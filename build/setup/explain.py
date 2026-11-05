"""AVADO Private Assistant: "Explain this" for the AVADO Admin.

POST /avado/api/explain (nginx forwards it here) takes ONE finding of the Admin's health check as a
small, structured payload and returns a plain-words explanation, written word by word as server-sent
events. The Admin (http://my.ava.do or https://my.ava.do) is the only web page allowed to call it.

    {"finding": {"id", "severity", "topic", "title", "why", "fix": {"label"}},
     "app": {"title", "version", "running"},
     "context": {"verdict", "processes": [{"name", "state", "lastError"}],
                 "disk": {"pct"}, "sync": {"syncing", "distance", "peers"}},
     "question": "optional, at most 300 characters"}

The helper builds the prompt itself (the page cannot turn it into a general chat), scrubs every string
again with the Admin's rules (health/diagnostics.js scrub(): long hex, password=..., Bearer tokens,
passwords in addresses, 12+ word phrases), answers one request at a time (others get 429 at once),
stops after 2 minutes and keeps an answer for an hour (the same finding opened twice is instant).

The model never acts and never invents a button: it is told to name only the finding's own fix
button, by its label. Standard library only.
"""

import hashlib
import json
import re
import threading
import time

ALLOWED_ORIGINS = ("http://my.ava.do", "https://my.ava.do")
MAX_BODY = 16 * 1024
MAX_SECONDS = 120
CACHE_SECONDS = 3600
CACHE_SIZE = 100
MAX_WORDS_TOKENS = 380

SCRUB_RULES = [
    (re.compile(r"\b[0-9a-fA-F]{64,}\b"), lambda m: m.group(0)[:6] + "…"),
    (re.compile(r"(password|passphrase|token|secret|mnemonic|seed|jwt|apikey|api_key|private[_-]?key)(\s*[=:]\s*)\S+",
                re.I), lambda m: m.group(1) + "=<redacted>"),
    (re.compile(r"Bearer\s+\S+"), lambda m: "Bearer <redacted>"),
    (re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.I), lambda m: m.group(1) + "<redacted>@"),
    (re.compile(r"\b(?:[a-z]+[ \t]+){11,}[a-z]+\b"), lambda m: "<12+ words redacted>"),
]

# Findings about money or a validator's safety end with a pointer to AVADO support.
MONEY_TOPICS = ("attestations",)
MONEY_WORDS = ("fee-recipient", "fee_recipient", "two-validator", "doppelganger", "slash", "withdraw", "exit",
               "missed", "validator")

SYSTEM = """You explain one finding of the AVADO Admin's health check to the owner of an AVADO box (a home server for Ethereum staking and other apps). The owner is not technical.
Rules:
- Write 3 to 6 short sentences in plain words, in the language of the question if there is one, else in English. No headings, no lists, no code.
- First say what it means, then why it matters, then what to do.
- Use only the facts below. If they do not say why it happened, say you do not know the cause. Never guess numbers, versions or causes.
- Never suggest commands, file paths, SSH, a terminal or settings outside the AVADO Admin. Never ask for keys, passwords or recovery words.
- If the facts name a fix button, tell the owner to press that button, by its exact name in quotes. Never name any other button."""

MONEY_RULE = "- This finding is about a validator or money: end with \"If you are unsure, ask AVADO support at support@ava.do.\""


def scrub(text):
    s = "" if text is None else str(text)
    for rx, fn in SCRUB_RULES:
        s = rx.sub(fn, s)
    return s


def clip(text, n):
    s = scrub(text).replace("\r", " ").strip()
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _s(d, key, n):
    v = d.get(key) if isinstance(d, dict) else None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    return clip(v, n) if isinstance(v, str) and v.strip() else None


def _num(d, key):
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def _bool(d, key):
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, bool) else None


class PayloadError(Exception):
    pass


def clean_payload(raw):
    """Only the fields the prompt uses, every string scrubbed and clipped. PayloadError when unusable."""
    if not isinstance(raw, dict):
        raise PayloadError("Send a JSON object.")
    f = raw.get("finding")
    if not isinstance(f, dict):
        raise PayloadError("The finding is missing.")
    finding = {
        "id": _s(f, "id", 80),
        "severity": _s(f, "severity", 20),
        "topic": _s(f, "topic", 40),
        "title": _s(f, "title", 200),
        "why": _s(f, "why", 800),
        "fix": _s(f.get("fix") if isinstance(f.get("fix"), dict) else {}, "label", 80),
    }
    if not finding["title"]:
        raise PayloadError("The finding has no title.")
    a = raw.get("app") if isinstance(raw.get("app"), dict) else {}
    app = {"title": _s(a, "title", 80), "version": _s(a, "version", 40), "running": _bool(a, "running")}
    c = raw.get("context") if isinstance(raw.get("context"), dict) else {}
    procs = []
    for p in (c.get("processes") if isinstance(c.get("processes"), list) else [])[:8]:
        if isinstance(p, dict) and _s(p, "name", 60):
            procs.append({"name": _s(p, "name", 60), "state": _s(p, "state", 20), "lastError": _s(p, "lastError", 300)})
    disk = c.get("disk") if isinstance(c.get("disk"), dict) else {}
    sync = c.get("sync") if isinstance(c.get("sync"), dict) else {}
    context = {
        "verdict": _s(c, "verdict", 60),
        "processes": procs,
        "disk_pct": _num(disk, "pct"),
        "syncing": _bool(sync, "syncing"),
        "distance": _num(sync, "distance"),
        "peers": _num(sync, "peers"),
    }
    q = raw.get("question")
    question = clip(q, 300) if isinstance(q, str) and q.strip() else None
    return {"finding": finding, "app": app, "context": context, "question": question}


def is_money(p):
    f = p["finding"]
    blob = " ".join(x for x in (f["id"], f["topic"], f["title"]) if x).lower()
    return (f["topic"] or "").lower() in MONEY_TOPICS or any(w in blob for w in MONEY_WORDS)


def facts_text(p):
    f, a, c = p["finding"], p["app"], p["context"]
    lines = ["Finding: %s" % f["title"]]
    if f["severity"]:
        lines.append("How serious: %s" % {"critical": "action required", "warning": "needs attention",
                                          "info": "for information"}.get(f["severity"], f["severity"]))
    if f["why"]:
        lines.append("What the Admin says: %s" % f["why"])
    lines.append("Fix button: %s" % ('"%s"' % f["fix"] if f["fix"] else "none"))
    if a["title"]:
        bits = [a["title"]]
        if a["version"]:
            bits.append("version %s" % a["version"])
        if a["running"] is not None:
            bits.append("running" if a["running"] else "not running")
        lines.append("App: %s" % ", ".join(bits))
    for pr in c["processes"]:
        t = "Process %s: %s" % (pr["name"], pr["state"] or "unknown")
        if pr["lastError"]:
            t += "; last error line: %s" % pr["lastError"]
        lines.append(t)
    if c["disk_pct"] is not None:
        lines.append("Disk: %d%% full" % round(c["disk_pct"]))
    if c["syncing"] is not None:
        s = "syncing" if c["syncing"] else "in sync"
        if c["distance"] is not None and c["syncing"]:
            s += ", %d behind" % c["distance"]
        lines.append("Node: %s" % s)
    if c["peers"] is not None:
        lines.append("Peers: %d" % c["peers"])
    if c["verdict"]:
        lines.append("Overall: %s" % c["verdict"])
    return "\n".join(lines)


def build_messages(p):
    system = SYSTEM + ("\n" + MONEY_RULE if is_money(p) else "")
    user = "Facts:\n" + facts_text(p) + "\n\n"
    user += ("The owner asks: %s" % p["question"]) if p["question"] else "Explain this finding to the owner."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def cache_key(p, model):
    f = p["finding"]
    raw = json.dumps([f["id"], f["title"], f["why"], f["fix"], p["question"], model], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def origin_allowed(origin):
    return origin in ALLOWED_ORIGINS


class Busy(Exception):
    pass


class Explainer:
    """Cache and the one-at-a-time rule. `chat(messages, deadline)` yields text pieces."""

    def __init__(self, chat, clock=time.monotonic, max_seconds=MAX_SECONDS):
        self.chat = chat
        self.clock = clock
        self.max_seconds = max_seconds
        self.lock = threading.Lock()
        self.cache = {}

    def cached(self, key):
        hit = self.cache.get(key)
        if hit and self.clock() - hit[1] < CACHE_SECONDS:
            return hit[0]
        self.cache.pop(key, None)
        return None

    def remember(self, key, text):
        if len(self.cache) >= CACHE_SIZE:
            oldest = min(self.cache, key=lambda k: self.cache[k][1])
            self.cache.pop(oldest, None)
        self.cache[key] = (text, self.clock())

    def run(self, payload, model):
        """Yields ("text", piece) events and ends with ("done", {"cached": bool, "stopped": bool}).
        Raises Busy at once when another explanation is being written."""
        key = cache_key(payload, model)
        hit = self.cached(key)
        if hit is not None:
            def replay():
                yield "text", hit
                yield "done", {"cached": True, "stopped": False}
            return replay()
        if not self.lock.acquire(blocking=False):
            raise Busy()
        return self._fresh(payload, key)

    def _fresh(self, payload, key):
        out = []
        stopped = False
        complete = False
        try:
            deadline = self.clock() + self.max_seconds
            for piece in self.chat(build_messages(payload), deadline):
                if piece:
                    out.append(piece)
                    yield "text", piece
                if self.clock() > deadline:
                    stopped = True
                    break
            complete = True
        finally:
            self.lock.release()
            text = "".join(out).strip()
            if complete and text and not stopped:
                self.remember(key, text)
        yield "done", {"cached": False, "stopped": stopped}
