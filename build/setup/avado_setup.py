#!/usr/bin/env python3
"""AVADO Private Assistant: the setup helper.

Runs next to Ollama and Open WebUI in the package container (see
/usr/local/bin/avado-start). It

* picks a model that suits the box (memory and free disk space) on first start
  and downloads it through Ollama, with progress in plain words;
* serves the setup page at /avado/ (nginx forwards it here) and a small API the
  page uses: status (open, read-only) and choose / cancel / remove / speed test
  (only for the assistant's owner: a signed-in Open WebUI admin);
* writes the "ready" flag that tells nginx to open the chat at / instead of
  the setup page;
* keeps Open WebUI's default model in step with the chosen one. Open WebUI keeps
  its settings in its own database (the environment only seeds the first start),
  so this goes through Open WebUI's admin API with the owner's own sign-in,
  whenever the owner opens this page;
* measures how fast the model writes on this box, once, after a download.

Standard library only (the image's Python 3.12), so it adds nothing to the
image and cannot break when Open WebUI's own Python packages change.
"""

import collections
import json
import os
import shutil
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

OLLAMA_URL = os.environ.get("AVADO_OLLAMA_URL", "http://127.0.0.1:11434")
WEBUI_URL = os.environ.get("AVADO_WEBUI_URL", "http://127.0.0.1:8080")
STATE_DIR = os.environ.get("AVADO_STATE_DIR", "/app/backend/data/avado")
MODELS_DIR = os.environ.get("OLLAMA_MODELS", "/models/models")
PUBLIC_DIR = os.environ.get("AVADO_PUBLIC_DIR", os.path.join(HERE, "public"))
CATALOG_PATH = os.environ.get("AVADO_CATALOG", os.path.join(HERE, "models.json"))
LISTEN_HOST = os.environ.get("AVADO_SETUP_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("AVADO_SETUP_PORT", "8090"))
VERSION_FILE = os.environ.get("AVADO_VERSION_FILE", "/opt/avado/package-version")
RETRY_SECONDS = int(os.environ.get("AVADO_RETRY_SECONDS", "60"))


def env_yes(name, default="yes"):
    return os.environ.get(name, default).strip().lower() in ("1", "yes", "true", "on")


GIB = 1024 ** 3

# --------------------------------------------------------------------------
# Plain words
# --------------------------------------------------------------------------


def plain_size(n):
    """Bytes as people read them on a store page: 274 MB, 2.5 GB, 65 GB."""
    if n is None:
        return "unknown"
    if n < 1e9:
        return "%d MB" % max(1, round(n / 1e6))
    gb = n / 1e9
    if gb < 10:
        return ("%.1f GB" % gb).replace(".0 GB", " GB")
    if gb < 1000:
        return "%d GB" % round(gb)
    return ("%.1f TB" % (gb / 1000)).replace(".0 TB", " TB")


# Memory sizes boxes are sold with. Linux reports a little less than the
# memory fitted (a 64 GB box shows about 62 GiB), so the page names the size
# the owner bought, not a number that matches nothing on the invoice.
RAM_SIZES_GB = (4, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512)


def plain_ram(n):
    if not n:
        return None
    gib = n / 1024 ** 3
    for size in RAM_SIZES_GB:
        if gib <= size:
            return "%d GB" % size
    return "%d GB" % round(gib)


def plain_duration(seconds):
    if seconds is None:
        return None
    s = max(0, float(seconds))
    if s < 60:
        return "less than a minute"
    minutes = s / 60
    if minutes < 2:
        return "about a minute"
    if minutes < 10:
        return "about %d minutes" % round(minutes)
    if minutes < 60:
        return "about %d minutes" % (5 * round(minutes / 5))
    hours = minutes / 60
    if hours < 1.5:
        return "about an hour"
    if hours < 36:
        return "about %d hours" % round(hours)
    return "about %d days" % round(hours / 24)


def plain_speed(bytes_per_second):
    if not bytes_per_second:
        return None
    mb = bytes_per_second / 1e6
    if mb < 1:
        return "%d kB per second" % max(1, round(bytes_per_second / 1e3))
    if mb < 10:
        return "%.1f MB per second" % mb
    return "%d MB per second" % round(mb)


def plain_pull_error(message):
    """Ollama's pull error -> (sentence for the owner, try again by itself?)."""
    m = (message or "").lower()
    if "no space left" in m or "disk full" in m:
        return ("The box ran out of free space during the download.", False)
    if "file does not exist" in m or "not found" in m or "manifest unknown" in m:
        return ("This model is no longer in the model library. Please choose another one.", False)
    if "max retries exceeded" in m or "timeout" in m or "timed out" in m or "connection" in m \
            or "eof" in m or "tls" in m or "dial tcp" in m or "lookup" in m or "network" in m \
            or "stream ended" in m or "reset" in m or "unreachable" in m:
        return ("The box lost its connection to the model library.", True)
    if "digest mismatch" in m or "verif" in m:
        return ("A part of the download was damaged. It is downloaded again.", True)
    return ("The download stopped unexpectedly.", True)


# --------------------------------------------------------------------------
# The box: memory, disk, processor
# --------------------------------------------------------------------------


def parse_meminfo(text):
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            value = int(parts[1])
            unit = parts[2].lower() if len(parts) > 2 else "kb"
            return value * 1024 if unit == "kb" else value
    return None


def parse_cgroup_limit(text):
    t = (text or "").strip()
    if not t or t == "max":
        return None
    try:
        v = int(t)
    except ValueError:
        return None
    # cgroup v1 reports "no limit" as a huge number
    return v if v < (1 << 60) else None


def read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def box_ram_bytes():
    total = parse_meminfo(read_file("/proc/meminfo") or "")
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        limit = parse_cgroup_limit(read_file(p))
        if limit and (total is None or limit < total):
            total = limit
    return total


def box_cpu():
    text = read_file("/proc/cpuinfo") or ""
    name = None
    for line in text.splitlines():
        if line.startswith("model name"):
            name = line.split(":", 1)[1].strip()
            break
    return {"name": name, "threads": os.cpu_count()}


def free_disk_bytes(path):
    p = path
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    try:
        return shutil.disk_usage(p or "/").free
    except OSError:
        return None


# --------------------------------------------------------------------------
# Which model
# --------------------------------------------------------------------------


def load_catalog(path=CATALOG_PATH):
    with open(path) as f:
        data = json.load(f)
    models = sorted(data["models"], key=lambda m: m["download_bytes"])
    return {"reserve_bytes": int(data.get("reserve_disk_gb", 50) * 1e9), "models": models}


def check_fit(model, ram_bytes, free_bytes, reserve_bytes):
    """(fits?, reason in plain words when it does not)."""
    ram_gib = (ram_bytes or 0) / GIB
    if ram_gib < model["min_ram_gib"]:
        return False, "Needs a box with more memory."
    if free_bytes is not None and free_bytes < model["download_bytes"] + reserve_bytes:
        need = model["download_bytes"] + reserve_bytes
        return False, "Needs %s of free space (the download plus %s kept free for your other apps)." % (
            plain_size(need), plain_size(reserve_bytes))
    return True, None


def recommend(catalog, ram_bytes, free_bytes):
    """The automatic choice: the biggest model meant for this box size that
    also fits the free disk; else the biggest smaller one that fits; else None."""
    ram_gib = (ram_bytes or 0) / GIB
    reserve = catalog["reserve_bytes"]
    meant = [m for m in catalog["models"]
             if m.get("recommend_from_gib") is not None and ram_gib >= m["recommend_from_gib"]]
    for m in reversed(meant):
        if check_fit(m, ram_bytes, free_bytes, reserve)[0]:
            return m
    for m in reversed(catalog["models"]):
        if check_fit(m, ram_bytes, free_bytes, reserve)[0]:
            return m
    return None


def find_model(catalog, key):
    for m in catalog["models"]:
        if key in (m["id"], m["tag"]):
            return m
    return None


def same_tag(a, b):
    """Ollama reports "name:latest" for a tag pulled as "name"."""
    def norm(t):
        return t if ":" in t else t + ":latest"
    return bool(a and b) and norm(a) == norm(b)


# --------------------------------------------------------------------------
# Download progress
# --------------------------------------------------------------------------


class Progress:
    """Turns Ollama's pull events into done/total bytes, speed and time left."""

    WINDOW = 60.0

    def __init__(self, expected_total=None, clock=time.monotonic):
        self.expected = expected_total or 0
        self.layers = {}
        self.stage = "starting"
        self.clock = clock
        self.samples = collections.deque()
        # Bytes already on the box when a part was first seen (a download that
        # goes on where it stopped): not counted in the speed.
        self.resumed = 0

    def feed(self, event):
        status = (event.get("status") or "").lower()
        digest = event.get("digest")
        if digest and event.get("total"):
            completed = int(event.get("completed") or 0)
            if digest not in self.layers and completed:
                self.resumed += completed
                self.samples.clear()
            self.layers[digest] = (int(event["total"]), completed)
            self.stage = "downloading"
        elif status.startswith("pulling manifest"):
            self.stage = "starting"
        elif status.startswith("verifying"):
            self.stage = "verifying"
        elif status.startswith("writing") or status.startswith("removing"):
            self.stage = "finishing"
        elif status == "success":
            self.stage = "done"
        self.sample()

    def done(self):
        return sum(c for _, c in self.layers.values())

    def total(self):
        return max(sum(t for t, _ in self.layers.values()), self.expected)

    def sample(self):
        now = self.clock()
        self.samples.append((now, self.done() - self.resumed))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.WINDOW:
            self.samples.popleft()

    def speed(self):
        if len(self.samples) < 2:
            return None
        (t0, d0), (t1, d1) = self.samples[0], self.samples[-1]
        if t1 - t0 < 3 or d1 <= d0:
            return None
        return (d1 - d0) / (t1 - t0)

    def eta(self):
        s = self.speed()
        if not s:
            return None
        return max(0, self.total() - self.done()) / s

    def summary(self):
        total, done = self.total(), self.done()
        return {
            "stage": self.stage,
            "done": done,
            "total": total,
            "percent": int(done * 100 / total) if total else 0,
            "done_plain": plain_size(done),
            "total_plain": plain_size(total),
            "speed_plain": plain_speed(self.speed()),
            "eta_plain": plain_duration(self.eta()),
        }


class PullError(Exception):
    pass


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# Talking to Ollama and Open WebUI (both on 127.0.0.1 inside the container)
# --------------------------------------------------------------------------


def http_json(url, data=None, method=None, headers=None, timeout=10):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method or ("POST" if body else "GET"))
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


class Ollama:
    def __init__(self, base=OLLAMA_URL):
        self.base = base

    def up(self):
        try:
            http_json(self.base + "/api/version", timeout=3)
            return True
        except Exception:
            return False

    def installed(self):
        """[{tag, size}] of the models on the box."""
        data = http_json(self.base + "/api/tags", timeout=10)
        return [{"tag": m.get("name") or m.get("model"), "size": m.get("size")} for m in data.get("models", [])]

    def delete(self, tag):
        http_json(self.base + "/api/delete", data={"model": tag}, method="DELETE", timeout=60)

    def pull(self, tag, progress, cancel):
        req = urllib.request.Request(
            self.base + "/api/pull", data=json.dumps({"model": tag, "stream": True}).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                for line in resp:
                    if cancel.is_set():
                        raise Cancelled()
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("error"):
                        raise PullError(str(event["error"]))
                    progress.feed(event)
                    if (event.get("status") or "").lower() == "success":
                        return
        except (urllib.error.URLError, OSError) as e:
            if cancel.is_set():
                raise Cancelled()
            raise PullError("connection: %s" % e)
        if cancel.is_set():
            raise Cancelled()
        raise PullError("stream ended before the download finished")

    def speed_test(self, tag, think=False):
        """Words per second on this box. Runs twice and keeps the better run: the
        first one also loads the model, and a busy moment on the box (a node
        catching up) should not decide the number on its own."""
        best, load = None, None
        for _ in range(2):
            r = http_json(self.base + "/api/generate", data={
                "model": tag,
                "prompt": "In three short sentences, explain why some people like to run their own computer at home.",
                "stream": False,
                # gpt-oss always thinks and takes a level, the others can skip it
                "think": think,
                "keep_alive": "5m",
                "options": {"num_predict": 96},
            }, timeout=900)
            count, dur = r.get("eval_count") or 0, r.get("eval_duration") or 0
            if not count or not dur:
                raise PullError("no speed numbers in the answer")
            tps = count / (dur / 1e9)
            best = tps if best is None else max(best, tps)
            if load is None:
                load = (r.get("load_duration") or 0) / 1e9
        return {
            "tokens_per_second": round(best, 1),
            "load_seconds": round(load, 1),
            "at": int(time.time()),
            "model": tag,
        }


def plain_speed_result(result):
    if not result:
        return None
    words = result["tokens_per_second"] * 0.75
    if words >= 6:
        feel = "faster than most people read"
    elif words >= 3:
        feel = "about as fast as most people read"
    else:
        feel = "slower than most people read, so longer answers take a while"
    text = "On this box it writes about %s words per second: %s." % (
        ("%.0f" % words) if words >= 3 else ("%.1f" % words), feel)
    if result.get("load_seconds", 0) >= 5:
        text += " After a pause, the first answer starts about %d seconds later while the model loads." % round(
            result["load_seconds"])
    return text


class WebUI:
    def __init__(self, base=WEBUI_URL):
        self.base = base
        self._cache = {}

    def onboarding(self):
        """True while nobody has made an account yet; None if Open WebUI is not up."""
        try:
            return bool(http_json(self.base + "/api/config", timeout=5).get("onboarding"))
        except Exception:
            return None

    def is_admin(self, token):
        """Is this Open WebUI sign-in token an admin's? Cached for a minute."""
        if not token or len(token) > 4096:
            return False
        now = time.monotonic()
        hit = self._cache.get(token)
        if hit and now - hit[1] < 60:
            return hit[0]
        try:
            user = http_json(self.base + "/api/v1/auths/", headers={"Authorization": "Bearer " + token}, timeout=5)
            ok = user.get("role") == "admin"
        except Exception:
            ok = False
        if len(self._cache) > 64:
            self._cache.clear()
        self._cache[token] = (ok, now)
        return ok

    def _auth(self, token):
        return {"Authorization": "Bearer " + token}

    def default_model(self, token):
        cfg = http_json(self.base + "/api/v1/configs/models", headers=self._auth(token), timeout=10)
        return cfg

    def set_default_model(self, token, tag):
        """Make `tag` the model a new chat starts with (Admin settings, Models),
        and keep Open WebUI's built-in tools off for every model unless the owner
        has set that switch himself.

        The built-in tools (search memories, notes, chats, knowledge; ask the user;
        time) add about 5,500 tokens of instructions to every question. A processor
        needs one to two minutes to read them before the first word, and the model
        then often searches empty notes and asks a question back instead of
        answering (seen on the test box with Qwen3 30B). The image's
        DEFAULT_MODEL_METADATA only seeds Open WebUI's very first start, so this
        also covers a box that started with an older image."""
        cfg = self.default_model(token)
        changed = False
        if (cfg.get("DEFAULT_MODELS") or "") != tag:
            cfg["DEFAULT_MODELS"] = tag
            changed = True
        meta = cfg.get("DEFAULT_MODEL_METADATA")
        meta = dict(meta) if isinstance(meta, dict) else {}
        caps = meta.get("capabilities")
        caps = dict(caps) if isinstance(caps, dict) else {}
        if "builtin_tools" not in caps:
            caps["builtin_tools"] = False
            meta["capabilities"] = caps
            cfg["DEFAULT_MODEL_METADATA"] = meta
            changed = True
        if not changed:
            return False
        http_json(self.base + "/api/v1/configs/models", data=cfg, headers=self._auth(token), timeout=10)
        return True


# --------------------------------------------------------------------------
# State and the worker
# --------------------------------------------------------------------------


class Manager:
    def __init__(self, catalog, ollama, webui, state_dir=STATE_DIR, auto_download=True,
                 ram_bytes=None, free_bytes_fn=None, retry_seconds=RETRY_SECONDS, clock=time.monotonic,
                 warmup_seconds=90):
        self.catalog = catalog
        self.ollama = ollama
        self.webui = webui
        self.state_dir = state_dir
        self.auto_download = auto_download
        self.ram_bytes = ram_bytes if ram_bytes is not None else box_ram_bytes()
        self.free_bytes_fn = free_bytes_fn or (lambda: free_disk_bytes(MODELS_DIR))
        self.retry_seconds = retry_seconds
        self.clock = clock
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.cancel = threading.Event()
        self.progress = None
        self.installed = []
        self.ollama_up = False
        self.want_speed_test = False
        self.speed_testing = False
        # The speed test waits until the box has settled after a start (Open
        # WebUI's first start alone keeps the processor busy for a minute).
        self.started = clock()
        self.warmup_seconds = warmup_seconds
        self.webui_synced = None  # (active tag, time) of the last good sync with Open WebUI
        os.makedirs(state_dir, exist_ok=True)
        self.state = self._load()
        # The first choice needs only memory and disk, not Ollama, so it is made
        # before Open WebUI starts for the first time and can seed its default.
        self.auto_pick()

    # -- persistence
    def _path(self, name):
        return os.path.join(self.state_dir, name)

    def _load(self):
        base = {"active": None, "target": None, "auto_done": False, "phase": "starting",
                "problem": None, "retry": False, "failed": None, "speed": None}
        try:
            with open(self._path("state.json")) as f:
                base.update(json.load(f))
        except (OSError, ValueError):
            pass
        return base

    def _save(self):
        tmp = self._path("state.json.tmp")
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self._path("state.json"))

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)
            self._save()

    def _write_default(self, tag):
        """The model Open WebUI is seeded with on its very first start."""
        with open(self._path("default-model.tmp"), "w") as f:
            f.write(tag + "\n")
        os.replace(self._path("default-model.tmp"), self._path("default-model"))

    def _write_ready(self):
        active = self.state.get("active")
        flag = self._path("ready")
        if active and any(same_tag(m["tag"], active) for m in self.installed):
            self._write_default(active)
            if not os.path.exists(flag):
                open(flag, "w").close()
        elif os.path.exists(flag) and self.ollama_up:
            os.remove(flag)

    def _on_box(self, tag):
        return any(same_tag(m["tag"], tag) for m in self.installed)

    def auto_pick(self):
        """First start: choose the model for this box and queue its download."""
        with self.lock:
            if self.state.get("active") or self.state.get("target") or self.state.get("auto_done") \
                    or not self.auto_download:
                if not os.path.exists(self._path("state.json")):
                    self._save()
                return
            pick = recommend(self.catalog, self.ram_bytes, self.free_bytes_fn())
            if pick:
                self._set(target=pick["tag"], auto_done=True)
                self._write_default(pick["tag"])
            else:
                self._set(phase="no-space", auto_done=True,
                          problem="There is not enough free space on the box for even the smallest model.")

    # -- owner actions (called from the web server)
    def choose(self, key):
        model = find_model(self.catalog, key)
        if not model:
            return "That model is not on the list."
        with self.lock:
            if self._on_box(model["tag"]):
                self._set(active=model["tag"], target=None, phase="ready", problem=None)
                self._write_ready()
                self.webui_synced = None
                return None
            ok, why = check_fit(model, self.ram_bytes, self.free_bytes_fn(), self.catalog["reserve_bytes"])
            if not ok:
                return why
            if self.state.get("target"):
                self.cancel.set()
            self._set(target=model["tag"], problem=None, retry=False, failed=None)
        self.wake.set()
        return None

    def stop_download(self):
        with self.lock:
            if not self.state.get("target"):
                return "Nothing is downloading."
            self.cancel.set()
            self._set(target=None, problem=None, retry=False,
                      phase="ready" if self.state.get("active") else "choose")
        self.wake.set()
        return None

    def remove(self, key):
        model = find_model(self.catalog, key)
        tag = model["tag"] if model else key
        with self.lock:
            if not self._on_box(tag):
                return "That model is not on the box."
            if same_tag(self.state.get("active"), tag):
                return "This is the model the assistant uses. Choose another one first."
        try:
            self.ollama.delete(tag)
        except Exception:
            return "The model could not be removed. Please try again."
        self.refresh()
        return None

    def request_speed_test(self):
        if not self.state.get("active"):
            return "There is no model yet."
        self.want_speed_test = True
        self.wake.set()
        return None

    def sync_webui(self, token):
        """Owner is on the page: make Open WebUI start new chats with the chosen
        model. At most once a minute, and again after every change."""
        active = self.state.get("active")
        if not active or not self._on_box(active):
            return
        now = self.clock()
        last = self.webui_synced
        if last and same_tag(last[0], active) and now - last[1] < 60:
            return
        try:
            self.webui.set_default_model(token, active)
            self.webui_synced = (active, now)
        except Exception as e:
            sys.stderr.write("avado-setup: could not update the assistant's settings: %r\n" % e)

    # -- worker
    def refresh(self):
        self.ollama_up = self.ollama.up()
        if self.ollama_up:
            try:
                self.installed = self.ollama.installed()
            except Exception:
                pass
        with self.lock:
            active = self.state.get("active")
            if active and self.ollama_up and not self._on_box(active):
                active = None
            chat = self.installed
            if not active and chat:
                # A model that is on the box (e.g. from an earlier version or added
                # in Open WebUI's admin settings) is better than none.
                known = [m for m in chat if find_model(self.catalog, m["tag"])] or chat
                active = known[-1]["tag"]
            if active != self.state.get("active"):
                self._set(active=active)
                self.webui_synced = None
            self._write_ready()

    def step(self):
        """One round of the worker. Returns seconds to wait before the next."""
        self.refresh()
        if not self.ollama_up:
            if self.state.get("phase") not in ("starting", "no-space"):
                self._set(phase="starting")
            return 3
        self.auto_pick()
        with self.lock:
            target = self.state.get("target")
        if target and self._on_box(target):
            self._finish(target)
            return 0
        if target:
            return self._download(target)
        if self.state.get("active"):
            if self.state.get("phase") != "ready":
                self._set(phase="ready", problem=None)
            if self.want_speed_test or not (self.state.get("speed") or {}).get("model") == self.state["active"]:
                if not self._settled():
                    return 10
                self._speed_test()
                return 0
            return 30
        if self.state.get("phase") not in ("no-space", "error"):
            self._set(phase="choose")
        return 30

    def _download(self, tag):
        model = find_model(self.catalog, tag) or {"download_bytes": 0}
        self.cancel.clear()
        self.progress = Progress(model["download_bytes"])
        self._set(phase="downloading", problem=None, retry=False)
        try:
            self.ollama.pull(tag, self.progress, self.cancel)
        except Cancelled:
            self.progress = None
            return 0
        except PullError as e:
            text, retry = plain_pull_error(str(e))
            sys.stderr.write("avado-setup: download of %s stopped: %s\n" % (tag, e))
            with self.lock:
                if not same_tag(self.state.get("target"), tag):
                    return 0
                if retry:
                    self._set(phase="retrying", problem=text, retry=True)
                else:
                    # Do not try again by itself; the page offers "Try again".
                    self._set(phase="error", problem=text, retry=False, target=None, failed=tag)
            if not retry:
                self.progress = None
            return self.retry_seconds if retry else 0
        self.refresh()
        self._finish(tag)
        return 0

    def _finish(self, tag):
        with self.lock:
            self._set(active=tag, target=None, phase="ready", problem=None, retry=False, failed=None)
            self.progress = None
            self.webui_synced = None
            self._write_ready()
        self.want_speed_test = True

    def _settled(self):
        return self.clock() - self.started >= self.warmup_seconds and self.webui.onboarding() is not None

    def _speed_test(self):
        self.want_speed_test = False
        tag = self.state.get("active")
        model = find_model(self.catalog, tag) or {}
        self.speed_testing = True
        try:
            result = self.ollama.speed_test(tag, think=model.get("think", False))
            self._set(speed=result)
        except Exception as e:
            sys.stderr.write("avado-setup: speed test failed: %s\n" % e)
            self._set(speed={"model": tag, "failed": True, "at": int(time.time())})
        finally:
            self.speed_testing = False

    def run(self):
        while True:
            try:
                wait = self.step()
            except Exception as e:  # never let the worker die
                sys.stderr.write("avado-setup: worker error: %r\n" % e)
                wait = 10
            if wait:
                self.wake.wait(wait)
            self.wake.clear()

    # -- what the page shows
    def status(self, can_change=False):
        with self.lock:
            st = dict(self.state)
            installed = list(self.installed)
            progress = self.progress.summary() if self.progress and st.get("target") else None
        free = self.free_bytes_fn()
        ram = self.ram_bytes
        active, target = st.get("active"), st.get("target")
        models = []
        rec = recommend(self.catalog, ram, None) if ram else None
        for m in self.catalog["models"]:
            on_box = next((i for i in installed if same_tag(i["tag"], m["tag"])), None)
            ok, why = check_fit(m, ram, free, self.catalog["reserve_bytes"])
            if same_tag(active, m["tag"]):
                state = "active"
            elif same_tag(target, m["tag"]):
                state = "downloading"
            elif on_box:
                state = "installed"
            else:
                state = "available" if ok else "unavailable"
            models.append({
                "id": m["id"], "name": m["name"], "label": m["label"], "about": m["about"],
                "size_plain": plain_size(m["download_bytes"]), "state": state,
                "note": None if (ok or on_box) else why,
                "recommended": rec is m,
            })
        extra = [i for i in installed if not find_model(self.catalog, i["tag"])]
        for i in extra:
            models.append({"id": i["tag"], "name": i["tag"], "label": "Added by you", "about": "",
                           "size_plain": plain_size(i.get("size")),
                           "state": "active" if same_tag(active, i["tag"]) else "installed",
                           "note": None, "recommended": False})
        name_of = lambda tag: (find_model(self.catalog, tag) or {"name": tag})["name"] if tag else None
        speed = st.get("speed") or {}
        return {
            "version": (read_file(VERSION_FILE) or "").strip() or None,
            "phase": st.get("phase"),
            "problem": st.get("problem"),
            "retry": st.get("retry"),
            "failed": (find_model(self.catalog, st["failed"]) or {}).get("id") if st.get("failed") else None,
            "active": name_of(active),
            "target": name_of(target),
            "progress": progress,
            "ready": bool(active) and any(same_tag(i["tag"], active) for i in installed),
            "onboarding": self.webui.onboarding(),
            "can_change": can_change,
            "speed": None if speed.get("failed") or not same_tag(speed.get("model"), active)
            else plain_speed_result(speed),
            "speed_testing": self.speed_testing,
            "box": {
                "ram_plain": plain_ram(ram),
                "free_plain": plain_size(free) if free is not None else None,
                "cpu": box_cpu().get("name"),
            },
            "models": models,
        }


# --------------------------------------------------------------------------
# Web server (behind nginx at /avado/)
# --------------------------------------------------------------------------

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; "
       # only the AVADO Admin may show this page (its Setup tab)
       "frame-ancestors 'self' http://my.ava.do https://my.ava.do")


def static_path(url_path, public_dir=PUBLIC_DIR):
    """/avado/<name> -> a file directly in public_dir, or None. No sub folders, no tricks."""
    rest = url_path[len("/avado/"):] if url_path.startswith("/avado/") else None
    if rest is None:
        return None
    rest = rest or "index.html"
    if "/" in rest or "\\" in rest or rest.startswith(".") or os.path.splitext(rest)[1] not in STATIC_TYPES:
        return None
    path = os.path.join(public_dir, rest)
    return path if os.path.isfile(path) else None


def request_token(headers):
    auth = headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for part in (headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "token":
            return v
    return None


def make_handler(manager, public_dir=PUBLIC_DIR):
    class Handler(BaseHTTPRequestHandler):
        server_version = "avado-setup"
        sys_version = ""

        def log_message(self, fmt, *args):  # quiet; nginx has the access log off too
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(data)

        def _owner(self):
            return manager.webui.is_admin(request_token(self.headers))

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/avado":
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/avado/")
                self.end_headers()
                return
            if path == "/avado/api/status":
                owner = self._owner()
                if owner:
                    manager.sync_webui(request_token(self.headers))
                self._send(200, manager.status(can_change=owner))
                return
            f = static_path(path, public_dir)
            if not f:
                self._send(404, {"error": "Not found"})
                return
            with open(f, "rb") as fh:
                self._send(200, fh.read(), STATIC_TYPES[os.path.splitext(f)[1]])

        def do_HEAD(self):
            path = self.path.split("?", 1)[0]
            f = static_path(path, public_dir)
            self.send_response(200 if f else 404)
            self.send_header("Content-Type", STATIC_TYPES[os.path.splitext(f)[1]] if f else "text/plain")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            # A custom header cannot be sent by a form or link on another site,
            # so another web page cannot press these buttons for the owner.
            if self.headers.get("X-Avado-Request") != "1":
                self._send(403, {"error": "Please use the buttons on this page."})
                return
            if manager.webui.onboarding():
                self._send(403, {"error": "Create your account first: press \"Create your account\" on this page."})
                return
            if not self._owner():
                self._send(403, {"error": "Only the assistant's owner can change this. Please sign in to the assistant first."})
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 4096)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    body = {}
            except ValueError:
                body = {}
            key = str(body.get("id") or "")[:200]
            actions = {
                "/avado/api/choose": lambda: manager.choose(key),
                "/avado/api/cancel": manager.stop_download,
                "/avado/api/remove": lambda: manager.remove(key),
                "/avado/api/speedtest": manager.request_speed_test,
            }
            if path not in actions:
                self._send(404, {"error": "Not found"})
                return
            problem = actions[path]()
            if problem:
                self._send(409, {"error": problem})
            else:
                manager.sync_webui(request_token(self.headers))
                self._send(200, manager.status(can_change=True))

    return Handler


def main():
    catalog = load_catalog()
    manager = Manager(catalog, Ollama(), WebUI(), auto_download=env_yes("AUTO_DOWNLOAD"))
    worker = threading.Thread(target=manager.run, name="worker", daemon=True)
    worker.start()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), make_handler(manager))
    server.daemon_threads = True
    ram = manager.ram_bytes
    print("avado-setup: listening on %s:%d; memory %s, automatic download %s" % (
        LISTEN_HOST, LISTEN_PORT, plain_ram(ram) or "unknown",
        "on" if manager.auto_download else "off"), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
