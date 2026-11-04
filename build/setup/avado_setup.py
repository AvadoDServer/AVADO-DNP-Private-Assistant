#!/usr/bin/env python3
"""AVADO Private Assistant: the setup helper.

Runs next to Ollama and Open WebUI in the package container (see
/usr/local/bin/avado-start). It

* measures how much memory the box can spare for the assistant (free memory now and its lowest
  point in the last 7 days, from the box's monitoring) and suggests the biggest model that fits;
* downloads a model through Ollama only when the owner presses the button on the setup page,
  with progress in plain words;
* watches the free memory (every minute, and before every chat): when the Ethereum node needs it,
  the model is unloaded and chats say "Paused so your Ethereum node has enough memory";
* gives every chat a short, read-only summary of the box ("About this AVADO", see boxfacts.py and
  avado_context_filter.py, an Open WebUI filter this helper installs);
* serves the setup page at /avado/ (nginx forwards it here) and a small API the page uses: status
  (open, read-only) and choose / cancel / remove / speed test (only for the assistant's owner: a
  signed-in Open WebUI admin);
* writes the "ready" flag that tells nginx to open the chat at / instead of the setup page;
* keeps Open WebUI's settings in step (default model, friendly model names, thinking off, half the
  processor threads, built-in tools off, the filter), through Open WebUI's admin API with the
  owner's own sign-in, whenever the owner opens this page;
* measures how fast the model writes on this box, once, after a download.

Standard library only (the image's Python), so it adds nothing to the image and cannot break when
Open WebUI's own Python packages change.
"""

import collections
import json
import os
import secrets
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boxfacts

HERE = os.path.dirname(os.path.abspath(__file__))

OLLAMA_URL = os.environ.get("AVADO_OLLAMA_URL", "http://127.0.0.1:11434")
WEBUI_URL = os.environ.get("AVADO_WEBUI_URL", "http://127.0.0.1:8080")
STATE_DIR = os.environ.get("AVADO_STATE_DIR", "/app/backend/data/avado")
MODELS_DIR = os.environ.get("OLLAMA_MODELS", "/models/models")
PUBLIC_DIR = os.environ.get("AVADO_PUBLIC_DIR", os.path.join(HERE, "public"))
CATALOG_PATH = os.environ.get("AVADO_CATALOG", os.path.join(HERE, "models.json"))
FILTER_PATH = os.environ.get("AVADO_FILTER", os.path.join(HERE, "avado_context_filter.py"))
LISTEN_HOST = os.environ.get("AVADO_SETUP_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("AVADO_SETUP_PORT", "8090"))
VERSION_FILE = os.environ.get("AVADO_VERSION_FILE", "/opt/avado/package-version")
RETRY_SECONDS = int(os.environ.get("AVADO_RETRY_SECONDS", "60"))
# For box tests only: a number in this file (GiB) replaces the measured free memory. The file lives
# in the container's own /run, so it is gone after any update or reinstall.
TEST_FREE_FILE = os.environ.get("AVADO_TEST_FREE_FILE", "/run/avado/test-free-gib")

GIB = 1024 ** 3
FILTER_ID = "avado_context"
PAUSED_MESSAGE = ("Paused so your Ethereum node has enough memory. The assistant starts again by itself "
                  "when enough memory is free.")


def half_threads():
    return max(1, (os.cpu_count() or 2) // 2)


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


def plain_gib(gib):
    """An amount of memory in GiB as a round number of GB ("about 14 GB" is what people read)."""
    gb = max(0.0, gib) * GIB / 1e9
    return "%d GB" % max(1, round(gb))


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


def read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def free_disk_bytes(path):
    p = path
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    try:
        return shutil.disk_usage(p or "/").free
    except OSError:
        return None


class Memory:
    """How much memory the box can spare, in GiB.

    free_now = MemAvailable + what the assistant's own model runner holds (so a loaded model does
    not count against itself). lowest_7d = the lowest MemAvailable in the last 7 days from the box's
    Prometheus (it catches the Ethereum clients' peaks: syncing, compaction, restarts), read at most
    every 10 minutes; None when the monitoring app is not installed (then only /proc/meminfo counts).
    """

    def __init__(self, prometheus=None, available=boxfacts.mem_available_bytes,
                 runner=boxfacts.own_runner_anon_bytes, clock=time.monotonic, test_file=TEST_FREE_FILE):
        self.prom = prometheus or boxfacts.Prometheus()
        self.available = available
        self.runner = runner
        self.clock = clock
        self.test_file = test_file
        self.low = None
        self.low_at = None

    def _test_value(self):
        t = read_file(self.test_file) if self.test_file else None
        try:
            return float(t) if t and t.strip() else None
        except ValueError:
            return None

    def free_now(self):
        t = self._test_value()
        if t is not None:
            return t
        a = self.available()
        if a is None:
            return None
        return (a + (self.runner() or 0)) / GIB

    def lowest_7d(self, refresh=False):
        t = self._test_value()
        if t is not None:
            return t
        now = self.clock()
        if refresh or self.low_at is None or now - self.low_at >= 600:
            v = self.prom.one(boxfacts.Q_MEM_MIN_7D)
            self.low = v / GIB if v else None
            self.low_at = now
        return self.low

    def reading(self):
        now = self.free_now()
        low = self.lowest_7d()
        both = [v for v in (now, low) if v is not None]
        return {"now": now, "lowest_7d": low, "free": min(both) if both else None,
                "source": "prometheus" if low is not None else "meminfo"}


# --------------------------------------------------------------------------
# Which model
# --------------------------------------------------------------------------


def load_catalog(path=CATALOG_PATH):
    with open(path) as f:
        data = json.load(f)
    models = sorted(data["models"], key=lambda m: (m["ram_need_gib"], m["download_bytes"]))
    sizing = {"margin_gib": 4, "webui_gib": 0.5, "cache_gib": 1, "min_free_gib": 7.5, "resume_extra_gib": 1}
    sizing.update(data.get("sizing") or {})
    return {"reserve_bytes": int(data.get("reserve_disk_gb", 50) * 1e9), "models": models, "sizing": sizing}


def overhead_gib(catalog):
    s = catalog["sizing"]
    return s["margin_gib"] + s["webui_gib"] + s["cache_gib"]


def budget_gib(catalog, free_gib):
    """Memory the model may use: free memory minus the margin for the node, Open WebUI and the
    model runner's prompt cache."""
    if free_gib is None:
        return None
    return free_gib - overhead_gib(catalog)


def uses_gib(catalog, model):
    """What the owner is told the assistant will use: the model plus Open WebUI."""
    return model["ram_need_gib"] + catalog["sizing"]["webui_gib"]


def check_fit(catalog, model, free_gib, free_bytes):
    """(fits?, reason in plain words when it does not)."""
    budget = budget_gib(catalog, free_gib)
    if budget is not None and (free_gib < catalog["sizing"]["min_free_gib"] or model["ram_need_gib"] > budget):
        return False, "Needs about %s of free memory next to your node." % plain_gib(
            model["ram_need_gib"] + overhead_gib(catalog))
    reserve = catalog["reserve_bytes"]
    if free_bytes is not None and free_bytes < model["download_bytes"] + reserve:
        need = model["download_bytes"] + reserve
        return False, "Needs %s of free space (the download plus %s kept free for your other apps)." % (
            plain_size(need), plain_size(reserve))
    return True, None


def recommend(catalog, free_gib, free_bytes):
    """The suggestion: the biggest model marked "auto" that fits the memory budget and the disk.
    None when the box has too little memory to spare (below min_free_gib nothing is suggested)."""
    if free_gib is not None and free_gib < catalog["sizing"]["min_free_gib"]:
        return None
    for m in reversed([m for m in catalog["models"] if m.get("auto")]):
        if check_fit(catalog, m, free_gib, free_bytes)[0]:
            return m
    return None


def find_model(catalog, key):
    for m in catalog["models"]:
        if key in (m["id"], m["tag"]) or same_tag(key, m["tag"]):
            return m
    return None


def same_tag(a, b):
    """Ollama reports "name:latest" for a tag pulled as "name"."""
    def norm(t):
        return t if ":" in t else t + ":latest"
    return bool(a and b) and norm(a) == norm(b)


def sizing_sentence(catalog, reading, model):
    """ "Even at its busiest, your AVADO has about 39 GB free. The assistant will use about 8 GB." """
    free = reading.get("free")
    if free is None:
        return None
    if reading.get("lowest_7d") is not None:
        first = "Even at its busiest, your AVADO has about %s free." % plain_gib(free)
    else:
        first = "Your AVADO has about %s free right now." % plain_gib(free)
    if model is None:
        return first + " The assistant needs about %s free, so it does not download anything for now." % plain_gib(
            catalog["sizing"]["min_free_gib"])
    return first + " The assistant will use about %s." % plain_gib(uses_gib(catalog, model))


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

    def loaded(self):
        """Tags of the models in memory right now."""
        data = http_json(self.base + "/api/ps", timeout=5)
        return [m.get("name") or m.get("model") for m in data.get("models", [])]

    def unload(self, tag):
        http_json(self.base + "/api/generate", data={"model": tag, "keep_alive": 0}, timeout=60)

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

    def speed_test(self, tag, threads=None):
        """Words per second on this box. Runs twice and keeps the better run: the
        first one also loads the model, and a busy moment on the box (a node
        catching up) should not decide the number on its own. Thinking off, like
        every chat."""
        best, load = None, None
        options = {"num_predict": 96}
        if threads:
            options["num_thread"] = threads
        for _ in range(2):
            r = http_json(self.base + "/api/generate", data={
                "model": tag,
                "prompt": "In three short sentences, explain why some people like to run their own computer at home.",
                "stream": False,
                "think": False,
                "keep_alive": "5m",
                "options": options,
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


PUBLIC_READ = [{"principal_type": "user", "principal_id": "*", "permission": "read"}]


class WebUI:
    def __init__(self, base=WEBUI_URL, filter_path=FILTER_PATH):
        self.base = base
        self.filter_path = filter_path
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

    def set_defaults(self, token, tag, threads):
        """Admin settings, Models: new chats start with `tag`; thinking off and half the processor
        threads for every model; Open WebUI's built-in tools off for every model. A value the owner
        has set himself is kept (only missing ones are added), except the default model, which
        follows the setup page.

        The built-in tools (search memories, notes, chats, knowledge; ask the user; time; and in
        0.13 "create a tool from chat") add about 5,500 tokens of instructions to every question. A
        processor needs one to two minutes to read them before the first word, and the model then
        often searches empty notes and asks a question back instead of answering (seen on the test
        box). The image's environment only seeds Open WebUI's very first start, so this also covers
        a box that started with an older image."""
        cfg = http_json(self.base + "/api/v1/configs/models", headers=self._auth(token), timeout=10)
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
        params = cfg.get("DEFAULT_MODEL_PARAMS")
        params = dict(params) if isinstance(params, dict) else {}
        for k, v in (("think", False), ("num_thread", threads)):
            if k not in params:
                params[k] = v
                cfg["DEFAULT_MODEL_PARAMS"] = params
                changed = True
        if changed:
            http_json(self.base + "/api/v1/configs/models", data=cfg, headers=self._auth(token), timeout=10)
        return changed

    def name_model(self, token, model, threads):
        """A model entry with the Ollama tag as its id: Open WebUI then shows the friendly name
        ("Qwen3.5 9B") in the picker and above every answer instead of "qwen3.5:9b". Everyone with
        an account may use it (without an entry, Open WebUI lets only admins use a model). Thinking
        off and half the threads also for Open WebUI's own background requests (chat titles).
        Returns True when it made the entry."""
        try:
            existing = http_json(self.base + "/api/v1/models/model?" + urllib.parse.urlencode({"id": model["tag"]}),
                                 headers=self._auth(token), timeout=10)
        except urllib.error.HTTPError as e:
            if e.code not in (401, 404):
                raise
            existing = None
        if existing:
            return False
        http_json(self.base + "/api/v1/models/create", headers=self._auth(token), timeout=10, data={
            "id": model["tag"],
            "base_model_id": None,
            "name": model["name"],
            "meta": {"description": model["about"], "capabilities": {"builtin_tools": False}},
            "params": {"think": False, "num_thread": threads},
            "access_grants": PUBLIC_READ,
            "is_active": True,
        })
        return True

    def install_filter(self, token):
        """The "About this AVADO" filter: installed, switched on, for every model. Its code is
        brought up to date when the package has a newer one. Returns what it did."""
        with open(self.filter_path) as f:
            content = f.read()
        form = {"id": FILTER_ID, "name": "About this AVADO", "content": content,
                "meta": {"description": "A short, read-only summary of this box for every chat, and the AVADO safety rules."}}
        try:
            fn = http_json(self.base + "/api/v1/functions/id/" + FILTER_ID, headers=self._auth(token), timeout=10)
        except urllib.error.HTTPError as e:
            if e.code not in (400, 401, 404):
                raise
            fn = None
        did = []
        if not fn:
            fn = http_json(self.base + "/api/v1/functions/create", data=form, headers=self._auth(token), timeout=30)
            did.append("created")
        elif fn.get("content") != content:
            fn = http_json(self.base + "/api/v1/functions/id/%s/update" % FILTER_ID, data=form,
                           headers=self._auth(token), timeout=30)
            did.append("updated")
        if not fn.get("is_active"):
            http_json(self.base + "/api/v1/functions/id/%s/toggle" % FILTER_ID, data={},
                      headers=self._auth(token), timeout=10)
            did.append("on")
        if not fn.get("is_global"):
            http_json(self.base + "/api/v1/functions/id/%s/toggle/global" % FILTER_ID, data={},
                      headers=self._auth(token), timeout=10)
            did.append("global")
        return did


# --------------------------------------------------------------------------
# State and the worker
# --------------------------------------------------------------------------


class Manager:
    def __init__(self, catalog, ollama, webui, state_dir=STATE_DIR, memory=None, facts=None,
                 free_bytes_fn=None, retry_seconds=RETRY_SECONDS, clock=time.monotonic,
                 warmup_seconds=90, threads=None):
        self.catalog = catalog
        self.ollama = ollama
        self.webui = webui
        self.state_dir = state_dir
        self.memory = memory or Memory()
        self.facts = facts or boxfacts.Facts()
        self.free_bytes_fn = free_bytes_fn or (lambda: free_disk_bytes(MODELS_DIR))
        self.retry_seconds = retry_seconds
        self.clock = clock
        self.threads = threads or half_threads()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.cancel = threading.Event()
        self.progress = None
        self.installed = []
        self.ollama_up = False
        self.want_speed_test = False
        self.speed_testing = False
        self.paused = False
        self.mem = {"now": None, "lowest_7d": None, "free": None, "source": None}
        self.mem_checked = None
        self.context_building = False
        # The speed test waits until the box has settled after a start (Open
        # WebUI's first start alone keeps the processor busy for a minute).
        self.started = clock()
        self.warmup_seconds = warmup_seconds
        self.webui_synced = None  # (active tag, time) of the last good sync with Open WebUI
        os.makedirs(state_dir, exist_ok=True)
        self.state = self._load()
        self.internal_token = self._internal_token()
        if not os.path.exists(self._path("state.json")):
            self._save()

    # -- persistence
    def _path(self, name):
        return os.path.join(self.state_dir, name)

    def _load(self):
        base = {"active": None, "target": None, "phase": "starting", "problem": None, "retry": False,
                "failed": None, "speed": None, "named": []}
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

    def _internal_token(self):
        """The filter in Open WebUI (same container, same user) shows this to read the box facts.
        nginx never forwards /internal/ here, and the file is readable by the app user only."""
        path = self._path("internal-token")
        token = (read_file(path) or "").strip()
        if len(token) < 32:
            token = secrets.token_urlsafe(32)
            fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token + "\n")
            os.replace(path + ".tmp", path)
        return token

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

    def need_gib(self, tag):
        m = find_model(self.catalog, tag) if tag else None
        if m:
            return m["ram_need_gib"]
        size = next((i.get("size") for i in self.installed if same_tag(i["tag"], tag)), None)
        return (size or 0) * 1.1 / GIB

    # -- memory
    def check_memory(self, refresh_low=False):
        """Reads the free memory and pauses or resumes the assistant. Returns the reading."""
        self.memory.lowest_7d(refresh=refresh_low)
        reading = self.memory.reading()
        self.mem = reading
        self.mem_checked = self.clock()
        active = self.state.get("active")
        now = reading["now"]
        if not active or now is None:
            if self.paused:
                self.paused = False
            return reading
        line = self.need_gib(active) + overhead_gib(self.catalog)
        if not self.paused and now < line:
            self.paused = True
            sys.stderr.write("avado-setup: only %.1f GiB free (needs %.1f): pausing the assistant\n" % (now, line))
            self._unload_all()
        elif self.paused and now >= line + self.catalog["sizing"]["resume_extra_gib"]:
            self.paused = False
            sys.stderr.write("avado-setup: %.1f GiB free again: the assistant can answer\n" % now)
        return reading

    def _unload_all(self):
        try:
            for tag in self.ollama.loaded():
                self.ollama.unload(tag)
        except Exception as e:
            sys.stderr.write("avado-setup: could not unload the model: %r\n" % e)

    def guard(self, model=None):
        """Before every chat (the filter asks): may the model be loaded now?"""
        self.check_memory()
        return self.paused

    # -- box facts for the chat
    def context(self):
        """The "About this AVADO" text. Built at most once a minute; while a rebuild runs, the last
        text is used, so a chat never waits for the box's slow calls (only the very first one)."""
        f = self.facts
        text = f.text
        if text is not None:
            if f.text_at is None or self.clock() - f.text_at >= 60:
                with self.lock:
                    start = not self.context_building
                    self.context_building = True
                if start:
                    threading.Thread(target=self._rebuild_context, daemon=True).start()
            return text
        return f.context(self._free_bytes_now())

    def _rebuild_context(self):
        try:
            self.facts.text = self.facts.build(self._free_bytes_now())
            self.facts.text_at = self.clock()
        except Exception as e:
            sys.stderr.write("avado-setup: could not read the box facts: %r\n" % e)
        finally:
            self.context_building = False

    def _free_bytes_now(self):
        """Free memory as the box reports it (for the chat facts; the sizing adds back the
        assistant's own runner, these do not)."""
        return boxfacts.mem_available_bytes()

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
            ok, why = check_fit(self.catalog, model, self.memory.reading()["free"], self.free_bytes_fn())
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
        if self.paused:
            return PAUSED_MESSAGE
        self.want_speed_test = True
        self.wake.set()
        return None

    def sync_webui(self, token):
        """Owner is on the page: Open WebUI's settings, model names and the filter. At most once a
        minute, and again after every change."""
        active = self.state.get("active")
        if not active or not self._on_box(active):
            return
        now = self.clock()
        last = self.webui_synced
        if last and same_tag(last[0], active) and now - last[1] < 60:
            return
        try:
            self.webui.set_defaults(token, active, self.threads)
            named = list(self.state.get("named") or [])
            for i in self.installed:
                m = find_model(self.catalog, i["tag"])
                # Only once per model: an entry the owner deletes himself stays deleted
                if m and m["tag"] not in named:
                    self.webui.name_model(token, m, self.threads)
                    named.append(m["tag"])
            if named != self.state.get("named"):
                self._set(named=named)
            self.webui.install_filter(token)
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
                # A model that is on the box (e.g. side-loaded or added in Open WebUI's admin
                # settings) is better than none.
                known = [m for m in chat if find_model(self.catalog, m["tag"])] or chat
                active = known[-1]["tag"]
            if active != self.state.get("active"):
                self._set(active=active)
                self.webui_synced = None
            self._write_ready()

    def step(self):
        """One round of the worker. Returns seconds to wait before the next."""
        self.refresh()
        if self.mem_checked is None or self.clock() - self.mem_checked >= 60:
            self.check_memory()
        if not self.ollama_up:
            if self.state.get("phase") != "starting":
                self._set(phase="starting")
            return 3
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
            if not self.paused and (self.want_speed_test or not (self.state.get("speed") or {}).get("model") == self.state["active"]):
                if not self._settled():
                    return 10
                self._speed_test()
                return 0
            return 30
        if self.state.get("phase") != "error":
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
        if self.guard(tag):
            return
        self.speed_testing = True
        try:
            result = self.ollama.speed_test(tag, threads=self.threads)
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
    def _memory_sentence(self, mem, active, rec):
        if active:
            m = find_model(self.catalog, active)
            if not m:
                return None
            return sizing_sentence(self.catalog, mem, m)
        return sizing_sentence(self.catalog, mem, rec)

    def status(self, can_change=False):
        with self.lock:
            st = dict(self.state)
            installed = list(self.installed)
            progress = self.progress.summary() if self.progress and st.get("target") else None
        free = self.free_bytes_fn()
        mem = self.mem if self.mem_checked is not None else self.check_memory()
        active, target = st.get("active"), st.get("target")
        rec = recommend(self.catalog, mem["free"], free)
        models = []
        for m in self.catalog["models"]:
            on_box = next((i for i in installed if same_tag(i["tag"], m["tag"])), None)
            ok, why = check_fit(self.catalog, m, mem["free"], free)
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
                "size_plain": plain_size(m["download_bytes"]), "uses_plain": plain_gib(uses_gib(self.catalog, m)),
                "state": state, "note": None if (ok or on_box) else why,
                "recommended": rec is m,
            })
        extra = [i for i in installed if not find_model(self.catalog, i["tag"])]
        for i in extra:
            models.append({"id": i["tag"], "name": i["tag"], "label": "Added by you", "about": "",
                           "size_plain": plain_size(i.get("size")), "uses_plain": None,
                           "state": "active" if same_tag(active, i["tag"]) else "installed",
                           "note": None, "recommended": False})
        name_of = lambda tag: (find_model(self.catalog, tag) or {"name": tag})["name"] if tag else None
        speed = st.get("speed") or {}
        phase = st.get("phase")
        rec_memory = recommend(self.catalog, mem["free"], None)  # memory alone
        if phase == "choose" and rec is None:
            if rec_memory is None and mem["free"] is not None:
                phase = "no-memory"
            elif rec_memory is not None:
                phase = "no-space"
        suggestion = None
        if rec is not None:
            suggestion = {"id": rec["id"], "name": rec["name"], "size_plain": plain_size(rec["download_bytes"]),
                          "uses_plain": plain_gib(uses_gib(self.catalog, rec))}
        return {
            "version": (read_file(VERSION_FILE) or "").strip() or None,
            "phase": phase,
            "paused": self.paused,
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
            "suggestion": suggestion,
            "memory_sentence": self._memory_sentence(mem, active, rec or rec_memory),
            "box": {
                "ram_plain": plain_ram(boxfacts.mem_total_bytes()),
                "free_plain": plain_size(free) if free is not None else None,
                "cpu": boxfacts.cpu_name(),
            },
            "models": models,
        }


# --------------------------------------------------------------------------
# Web server (behind nginx at /avado/; /internal/ only for the filter inside the container)
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

        def _internal(self, path):
            token = self.headers.get("X-Avado-Internal") or ""
            if not secrets.compare_digest(token.encode(), manager.internal_token.encode()):
                self._send(404, {"error": "Not found"})
                return
            if path == "/internal/context":
                model = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("model", [None])[0]
                paused = manager.guard(model)
                body = {"paused": paused, "message": PAUSED_MESSAGE if paused else None}
                if not paused:
                    try:
                        body["system"] = manager.context()
                    except Exception as e:
                        sys.stderr.write("avado-setup: could not read the box facts: %r\n" % e)
                        body["system"] = None
                self._send(200, body)
                return
            self._send(404, {"error": "Not found"})

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/internal/"):
                self._internal(path)
                return
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
    manager = Manager(catalog, Ollama(), WebUI())
    worker = threading.Thread(target=manager.run, name="worker", daemon=True)
    worker.start()
    # Read the box facts once now, so the first chat does not wait for them
    threading.Thread(target=manager._rebuild_context, name="facts", daemon=True).start()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), make_handler(manager))
    server.daemon_threads = True
    reading = manager.check_memory(refresh_low=True)
    rec = recommend(catalog, reading["free"], manager.free_bytes_fn())
    print("avado-setup: listening on %s:%d; free memory %s now, %s lowest in 7 days (%s); suggestion %s; "
          "%d threads for the model" % (
              LISTEN_HOST, LISTEN_PORT,
              "%.1f GiB" % reading["now"] if reading["now"] is not None else "unknown",
              "%.1f GiB" % reading["lowest_7d"] if reading["lowest_7d"] is not None else "unknown",
              reading["source"], rec["tag"] if rec else "none", manager.threads), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
