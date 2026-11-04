"""AVADO Private Assistant: what the assistant knows about its box.

Read-only. Three sources, the same ones the AVADO Admin and AI Connect (AVADO-DNP-MCP) use:

* Prometheus (the monitoring package, with node-exporter): free memory now and its lowest point in
  the last 7 days, processor use, disk, the Ethereum clients' memory, sync head, peers and
  attestations. The queries are the Admin Home's (DNP_ADMIN build/src/src/health/prometheus.js).
* The DAPPMANAGER over WAMP (realm dappnode_admin at ws://wamp.my.ava.do:8080/ws, anonymous, like
  the Admin, Care and AI Connect): listPackages for the installed apps and getDiagnostics
  (DAPPMANAGER 10.0.53+) for "What we found". Only these two procedures are ever called (see
  READ_ONLY_PROCEDURES); environment values that listPackages returns are dropped at once.
* /proc on the box (the container sees the host's /proc/meminfo and /proc/cpuinfo).

The "What we found" lines follow the Admin's deriveProblems (DNP_ADMIN health/diagnostics.js,
GPL-3.0, like this package) without the parts that need logs, the host run or the network probes.

Standard library only (no websocket package): a minimal WebSocket client is below.
"""

import base64
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request

GIB = 1024 ** 3

PROMETHEUS_BASES = [b.strip() for b in os.environ.get(
    "AVADO_PROMETHEUS_URLS",
    "http://prometheus.my.ava.do:9090/api/v1,http://my.ava.do/metrics-api").split(",") if b.strip()]
WAMP_URL = os.environ.get("AVADO_WAMP_URL", "ws://wamp.my.ava.do:8080/ws")
WAMP_REALM = "dappnode_admin"
SUFFIX = ".dappmanager.dnp.dappnode.eth"
READ_ONLY_PROCEDURES = frozenset(["listPackages", "getDiagnostics"])

# Admin Home queries (labels client and network come from the AVADO Prometheus scrape config)
DISK_SERIES = 'node_filesystem_avail_bytes{mountpoint=~"/|/host",fstype!~"tmpfs|overlay|vfat"}'
Q_MEM_MIN_7D = "min(min_over_time(node_memory_MemAvailable_bytes[7d]))"
Q_HEAD = "max by (client, network) (beacon_head_slot)"
Q_PEERS = 'max by (client, network) (libp2p_peers or p2p_peer_count{state="Connected"})'
Q_HIT = "sum by (client, network) (increase(validator_monitor_prev_epoch_on_chain_attester_hit_total[1h]))"
Q_MISS = "sum by (client, network) (increase(validator_monitor_prev_epoch_on_chain_attester_miss_total[1h]))"
Q_RSS = "max by (client) (process_resident_memory_bytes)"
Q_DISK_FREE = "max(%s)" % DISK_SERIES
Q_DISK_SIZE = 'max(node_filesystem_size_bytes{mountpoint=~"/|/host",fstype!~"tmpfs|overlay|vfat"})'
Q_DISK_SLOPE = "min(deriv(%s[7d]))" % DISK_SERIES
Q_CPU_BUSY = '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))'

# Not Ethereum clients: the monitoring itself
NOT_CLIENTS = {"node-exporter", "prometheus", "node", "grafana"}
MAINNET_GENESIS = 1606824023


# --------------------------------------------------------------------------
# /proc
# --------------------------------------------------------------------------


def read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def meminfo_value(text, key):
    for line in (text or "").splitlines():
        if line.startswith(key + ":"):
            parts = line.split()
            try:
                return int(parts[1]) * 1024
            except (IndexError, ValueError):
                return None
    return None


def mem_available_bytes():
    return meminfo_value(read_file("/proc/meminfo"), "MemAvailable")


def mem_total_bytes():
    return meminfo_value(read_file("/proc/meminfo"), "MemTotal")


def own_runner_anon_bytes(proc="/proc"):
    """Memory the assistant's own model runner holds that free memory no longer counts (its
    anonymous memory: KV cache, buffers; the model file itself sits in the page cache, which
    MemAvailable still counts). Added back, so a loaded model does not pause itself."""
    total = 0
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return 0
    for pid in pids:
        cmd = read_file(os.path.join(proc, pid, "cmdline")) or ""
        argv0 = os.path.basename(cmd.split("\0", 1)[0])
        if argv0 != "llama-server" and " runner" not in cmd.replace("\0", " "):
            continue
        anon = meminfo_value(read_file(os.path.join(proc, pid, "status")), "RssAnon")
        total += anon or 0
    return total


def cpu_name():
    for line in (read_file("/proc/cpuinfo") or "").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None


# --------------------------------------------------------------------------
# Prometheus
# --------------------------------------------------------------------------


class Prometheus:
    def __init__(self, bases=None, timeout=6):
        self.bases = list(bases or PROMETHEUS_BASES)
        self.timeout = timeout
        self.working = 0

    def query(self, q):
        """[(labels, value)] or None when no base answered."""
        order = [self.working] + [i for i in range(len(self.bases)) if i != self.working]
        for i in order:
            url = "%s/query?%s" % (self.bases[i], urllib.parse.urlencode({"query": q}))
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as r:
                    body = json.loads(r.read())
            except Exception:
                continue
            if body.get("status") != "success":
                continue
            self.working = i
            out = []
            for item in body.get("data", {}).get("result", []):
                try:
                    v = float(item["value"][1])
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                if v == v and v not in (float("inf"), float("-inf")):
                    out.append((item.get("metric") or {}, v))
            return out
        return None

    def one(self, q):
        r = self.query(q)
        return r[0][1] if r else None


# --------------------------------------------------------------------------
# DAPPMANAGER over WAMP (minimal WebSocket + WAMP v2 caller)
# --------------------------------------------------------------------------


class WampError(Exception):
    pass


class WebSocket:
    """Just enough of RFC 6455 for one short WAMP session: text frames, ping/pong, close."""

    def __init__(self, url, protocol, timeout=10):
        u = urllib.parse.urlparse(url)
        if u.scheme != "ws":
            raise WampError("only ws:// is supported")
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: %s\r\n\r\n") % (
            u.path or "/", u.hostname, u.port or 80, key, protocol)
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WampError("the router closed the connection")
            head += chunk
            if len(head) > 65536:
                raise WampError("bad handshake")
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise WampError("no websocket upgrade")

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WampError("connection closed")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def send(self, text):
        self._send_frame(0x1, text.encode())

    def recv(self):
        message = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            if n > 64 * 1024 * 1024:
                raise WampError("message too large")
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise WampError("the router closed the session")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if fin:
                return message.decode("utf-8", "replace")

    def close(self):
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def dappmanager_call(procedure, kwargs=None, url=WAMP_URL, timeout=30, ws_factory=WebSocket):
    """One read-only DAPPMANAGER call: its `result`, or WampError."""
    if procedure not in READ_ONLY_PROCEDURES:
        raise WampError("%s is not a read-only call" % procedure)
    ws = ws_factory(url, "wamp.2.json", timeout=timeout)
    try:
        ws.send(json.dumps([1, WAMP_REALM, {"roles": {"caller": {"features": {}}}, "agent": "avado-assistant"}]))
        msg = json.loads(ws.recv())
        if not msg or msg[0] != 2:
            raise WampError("the router did not welcome the session")
        ws.send(json.dumps([48, 1, {}, procedure + SUFFIX, [], dict(kwargs or {}, dontLogError=True)]))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(ws.recv())
            if msg[0] == 50 and msg[1] == 1:
                args = msg[3] if len(msg) > 3 else []
                env = args[0] if args else (msg[4] if len(msg) > 4 else None)
                if isinstance(env, str):
                    env = json.loads(env)
                if not isinstance(env, dict) or env.get("success") is not True:
                    raise WampError("%s failed: %s" % (procedure, (env or {}).get("message") if isinstance(env, dict) else "no answer"))
                return env.get("result")
            if msg[0] == 8 and msg[2] == 1:
                raise WampError("%s: %s" % (procedure, msg[4] if len(msg) > 4 else "error"))
        raise WampError("%s: no answer in time" % procedure)
    finally:
        ws.close()


def clean_packages(raw):
    """Only what the context uses. Environment values (secrets) never leave this function."""
    out = []
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str):
            continue
        manifest = p.get("manifest") if isinstance(p.get("manifest"), dict) else {}
        out.append({
            "name": p["name"],
            "title": manifest.get("title") if isinstance(manifest.get("title"), str) else None,
            "version": p.get("version") if isinstance(p.get("version"), str) else None,
            "state": p.get("state") if isinstance(p.get("state"), str) else None,
            "core": p.get("isCORE") is True or p.get("isCore") is True,
        })
    return sorted(out, key=lambda x: (x["core"], x["name"]))


def short_name(name):
    return (name or "").split(".")[0]


def title_of(name, packages):
    for p in packages or []:
        if p["name"] == name and p.get("title"):
            return p["title"]
    return short_name(name)


CHAIN_HINTS = ("geth", "nethermind", "erigon", "besu", "reth", "teku", "nimbus", "prysm", "lighthouse",
               "lodestar", "beacon", "eth2validator")


def is_chain_client(name):
    n = short_name(name).lower()
    return any(h in n for h in CHAIN_HINTS)


def derive_findings(diag, packages):
    """The Admin's "What we found", from getDiagnostics {containers, clients}: most severe first."""
    if not isinstance(diag, dict):
        return []
    out = []
    order = {"critical": 0, "warning": 1, "info": 2}
    containers = [c for c in diag.get("containers") or [] if isinstance(c, dict)]
    by_id = {c.get("id"): c for c in containers}
    failed = {"FATAL", "BACKOFF", "EXITED"}

    def down(c):
        if c.get("state") != "running":
            return []
        return [p for p in c.get("processes") or [] if isinstance(p, dict) and p.get("state") in failed]

    for c in containers:
        title = title_of(c.get("id"), packages)
        important = c.get("isCore") or is_chain_client(c.get("id"))
        for p in down(c):
            out.append(("critical" if is_chain_client(c.get("id")) else "warning",
                        "%s: the %s process is not running (%s)" % (title, p.get("name"), p.get("state"))))
        if c.get("state") in ("exited", "dead", "restarting"):
            out.append(("critical" if important else "warning", "%s is %s" % (title, c.get("state"))))
        elif isinstance(c.get("restartCount"), (int, float)) and c["restartCount"] >= 3 \
                and isinstance(c.get("uptimeSeconds"), (int, float)) and c["uptimeSeconds"] < 3600:
            out.append(("warning", "%s restarted %d times recently" % (title, c["restartCount"])))
        if c.get("oomKilled"):
            out.append(("warning", "%s was stopped because the box ran out of memory" % title))
    for cl in diag.get("clients") or []:
        if not isinstance(cl, dict) or not cl.get("id"):
            continue
        title = title_of(cl["id"], packages)
        api = cl.get("api") if isinstance(cl.get("api"), dict) else {}
        cont = by_id.get(cl["id"])
        running = cont is None or (cont.get("state") == "running" and not down(cont))
        if api.get("reachable") is False:
            if running:
                out.append(("critical", "%s does not answer" % title))
            continue
        s = api.get("syncing") if isinstance(api.get("syncing"), dict) else {}
        dist = s.get("syncDistance")
        if cl.get("role") == "beacon":
            if s.get("isSyncing") or (isinstance(dist, (int, float)) and dist > 100):
                out.append(("warning", "%s is syncing (%s slots behind)" % (title, dist if dist is not None else "?")))
            if s.get("elOffline"):
                out.append(("critical", "%s is not connected to the execution client" % title))
        if cl.get("role") == "execution" and s.get("synced") is False:
            out.append(("warning", "%s is syncing" % title))
        peers = api.get("peers")
        if isinstance(peers, (int, float)) and peers < 10:
            out.append(("warning", "%s has only %d peers" % (title, peers)))
        vals = cl.get("validators") if isinstance(cl.get("validators"), dict) else {}
        if cl.get("role") == "validator" and vals.get("count") == 0:
            out.append(("warning", "%s has no validator keys" % title))
    out.sort(key=lambda x: order.get(x[0], 3))
    return out


# --------------------------------------------------------------------------
# The context text
# --------------------------------------------------------------------------

RULES = """You are the Private Assistant on this AVADO, a home server for Ethereum staking and other apps. You run on the box itself, so chats stay private.
Rules:
- Answer in the language the user writes in, in plain, short words. Explain any technical word you use.
- For questions about this box, use only the facts below. If a fact is not there, say "I don't know" (in the user's language). Never guess or make up numbers, versions, causes or commands.
- Never ask for, and never accept, a seed phrase (recovery words), keystore file, private key or password. If someone offers one, tell them not to share it with anyone.
- For validator exits, withdrawals, rewards, or any question about money, do not give steps: point to docs.ava.do and support@ava.do.
- Other questions (writing, explaining, general knowledge) you may answer normally; say when you are not sure."""


def gb(n):
    """Bytes as rounded decimal GB / TB, like the Admin."""
    if n is None:
        return "unknown"
    g = n / 1e9
    if g >= 1000:
        return "%.1f TB" % (g / 1000)
    if g >= 10:
        return "%d GB" % round(g)
    return "%.1f GB" % g


def days_text(days):
    if days is None:
        return "the disk is not filling up"
    if days > 3650:
        return "the disk would be full in more than 10 years"
    if days > 730:
        return "the disk would be full in about %d years" % round(days / 365)
    if days > 60:
        return "the disk would be full in about %d months" % round(days / 30)
    return "the disk would be full in about %d days" % max(1, round(days))


class Facts:
    """Collects the facts. Packages change rarely (listPackages also runs `docker system df`):
    read at most every 10 minutes; everything else at most every 60 s, and only when asked."""

    def __init__(self, prometheus=None, call=dappmanager_call, clock=time.monotonic, wall=time.time,
                 cpu=cpu_name, mem_total=mem_total_bytes, threads=os.cpu_count):
        self.prom = prometheus or Prometheus()
        self.call = call
        self.clock = clock
        self.wall = wall
        self.cpu = cpu
        self.mem_total = mem_total
        self.threads = threads
        self.packages = None
        self.packages_at = None
        self.text = None
        self.text_at = None

    def _packages(self):
        now = self.clock()
        if self.packages is None or now - self.packages_at > 600:
            try:
                self.packages = clean_packages(self.call("listPackages", {}, timeout=90))
            except Exception:
                if self.packages is None:
                    self.packages = []
            self.packages_at = now
        return self.packages

    def _findings(self, packages):
        try:
            diag = self.call("getDiagnostics", {"host": False, "network": False, "clients": True, "logs": False},
                             timeout=30)
        except Exception:
            return None  # older DAPPMANAGER (before 10.0.53) or not reachable
        return derive_findings(diag, packages)

    def context(self, mem_free_now=None):
        now = self.clock()
        if self.text is not None and now - self.text_at < 60:
            return self.text
        self.text = self.build(mem_free_now)
        self.text_at = now
        return self.text

    def build(self, mem_free_now=None):
        packages = self._packages()
        p = self.prom
        lines = [RULES, "", "Facts about this AVADO (read-only):"]

        # Stable facts first (they rarely change, so the model can reuse what it already read)
        box = []
        if self.cpu():
            box.append("processor %s (%s threads)" % (self.cpu(), self.threads()))
        if self.mem_total():
            box.append("memory %s" % gb(self.mem_total()))
        size = p.one(Q_DISK_SIZE)
        if size:
            box.append("disk %s" % gb(size))
        lines.append("- Box: " + (", ".join(box) or "unknown") + ".")
        apps = [p_ for p_ in packages if not p_["core"]]
        core = [p_ for p_ in packages if p_["core"]]
        if packages:
            lines.append("- Installed apps: " + "; ".join(
                "%s %s (%s)" % (x["title"] or short_name(x["name"]), x["version"] or "?", x["state"] or "?")
                for x in apps) + "." if apps else "- Installed apps: none.")
            lines.append("- AVADO system parts: " + "; ".join(
                "%s %s" % (short_name(x["name"]), x["version"] or "?") for x in core) + ".")
        else:
            lines.append("- Installed apps: unknown (the box did not answer).")

        # Live numbers last, rounded
        lines.append("Now (rounded):")
        if mem_free_now:
            lines.append("- Free memory: %s." % gb(mem_free_now))
        busy = p.one(Q_CPU_BUSY)
        if busy is not None:
            lines.append("- Processor busy: %d%%." % round(max(0.0, busy) * 100))
        rss = p.query(Q_RSS)
        clients = [(m.get("client"), v) for m, v in (rss or []) if m.get("client") and m.get("client") not in NOT_CLIENTS]
        if rss is None:
            lines.append("- Ethereum clients: unknown (the monitoring app does not answer).")
        elif clients:
            lines.append("- Ethereum clients running (memory used): " + ", ".join(
                "%s %s" % (c, gb(v)) for c, v in sorted(clients)) + ".")
        else:
            lines.append("- Ethereum clients: none reports to the monitoring app (none running, or not set up).")
        heads = p.query(Q_HEAD) or []
        peers = {m.get("client"): v for m, v in p.query(Q_PEERS) or []}
        for m, v in heads:
            c, net = m.get("client") or "?", m.get("network") or "?"
            extra = ""
            if net == "mainnet":
                behind = int((self.wall() - MAINNET_GENESIS) // 12) - int(v)
                extra = ", in sync" if behind <= 2 else ", %d slots behind" % behind
            lines.append("- %s (%s): head slot %d%s%s." % (c, net, v, extra,
                         ", %d peers" % peers[c] if c in peers else ""))
        for c, v in peers.items():
            if c not in {m.get("client") for m, _ in heads}:
                lines.append("- %s: %d peers." % (c, v))
        hit = {m.get("client"): v for m, v in p.query(Q_HIT) or []}
        miss = {m.get("client"): v for m, v in p.query(Q_MISS) or []}
        if hit or miss:
            for c in sorted(set(hit) | set(miss)):
                lines.append("- Validator attestations in the last hour (%s): %d made, %d missed." % (
                    c, round(hit.get(c, 0)), round(miss.get(c, 0))))
        else:
            lines.append("- Validator attestations: no data (no validator client reports them).")
        free = p.one(Q_DISK_FREE)
        slope = p.one(Q_DISK_SLOPE)
        if free is not None:
            days = free / -slope / 86400 if slope is not None and slope < 0 else None
            lines.append("- Free disk: %s; at the last 7 days' pace %s." % (gb(free), days_text(days)))
        findings = self._findings(packages)
        if findings is None:
            lines.append("- Problems found: unknown (this box's system is too old to report them).")
        elif findings:
            lines.append("- Problems found: " + "; ".join(t for _, t in findings[:8]) + ".")
        else:
            lines.append("- Problems found: none.")
        lines.append("- Time: %s UTC." % time.strftime("%Y-%m-%d %H:%M", time.gmtime(self.wall())))
        return "\n".join(lines)
