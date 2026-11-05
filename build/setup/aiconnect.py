"""AVADO Private Assistant: live box checks through AI Connect (AVADO-DNP-MCP).

AI Connect is the box's MCP server (http://mcp.my.ava.do/mcp). With a key that may only look
("read", no restart or install permission), the assistant can call four of its tools while it answers:

* health_check     the Admin's own health check, plus processes down / out of memory / restarting
* list_apps        installed apps: version, running or not, auto-update, data size
* app_logs         the newest lines of one app's log, secrets hidden by AI Connect
* ethereum_status  per node app: syncing or in sync, how far behind, peers, validator count

Open WebUI never sees the key. It talks MCP to this helper on 127.0.0.1 (/internal/mcp, with the
helper's internal token as its bearer key; nginx never forwards /internal/), and the helper passes the
calls on to AI Connect with the owner's key. On the way it

* lists and allows only the four tools above (anything else is refused here, before AI Connect);
* asks for at most LOG_LINES log lines (a processor reads 100 log lines in about 20 seconds);
* shortens every tool answer to MAX_RESULT_CHARS (for logs it keeps the newest part).

A key that may also restart, install or update apps is refused when it is added: the assistant only
ever looks.

The key is kept in the data volume (aiconnect.json, readable by the app user only). Standard library only.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

AI_CONNECT_URL = os.environ.get("AVADO_AICONNECT_URL", "http://mcp.my.ava.do/mcp")
AI_CONNECT_PAGE = "http://mcp.my.ava.do"
ALLOWED_TOOLS = ("health_check", "list_apps", "app_logs", "ethereum_status")
# A tool that changes something: a key that offers one of these is not taken.
ACTION_TOOLS = ("restart_app", "install_app", "update_app")
LOG_LINES = 40
MAX_RESULT_CHARS = 6000
MAX_BODY = 256 * 1024
KEY_RE = re.compile(r"^avado_[A-Za-z0-9_-]{20,100}$")
SERVER_ID = "avado"
TOOL_ID = "server:mcp:" + SERVER_ID
PROTOCOL = "2025-06-18"


class AIConnectError(Exception):
    pass


def clean_key(raw):
    """The key as the owner pasted it (spaces, quotes, a whole "Bearer ..." line), or None."""
    k = str(raw or "").strip().strip("'\"")
    if k.lower().startswith("bearer "):
        k = k[7:].strip()
    # Also the whole address with the key inside (".../k/avado_.../mcp")
    m = re.search(r"avado_[A-Za-z0-9_-]{20,100}", k)
    return m.group(0) if m and KEY_RE.match(m.group(0)) else None


def post_mcp(url, key, message, timeout=30, opener=urllib.request.urlopen):
    """One MCP JSON-RPC message to AI Connect (Streamable HTTP, stateless). Returns (status, dict)."""
    body = json.dumps(message).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("MCP-Protocol-Version", PROTOCOL)
    try:
        with opener(req, timeout=timeout) as r:
            return r.status, parse_mcp_body(r.read(MAX_BODY * 4), r.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(MAX_BODY)
        except Exception:
            raw = b""
        try:
            return e.code, parse_mcp_body(raw, e.headers.get("Content-Type") or "")
        except Exception:
            return e.code, {}


def parse_mcp_body(raw, ctype):
    """A JSON body, or the last JSON-RPC message of a text/event-stream body."""
    if not raw:
        return {}
    if "text/event-stream" in ctype:
        last = {}
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.startswith("data:"):
                try:
                    last = json.loads(line[5:].strip())
                except ValueError:
                    pass
        return last
    return json.loads(raw)


def shorten(text, keep_end=False, limit=MAX_RESULT_CHARS):
    if not isinstance(text, str) or len(text) <= limit:
        return text
    note = "\n[shortened: %d of %d characters]" % (limit, len(text))
    if keep_end:
        return "[older lines left out]\n" + text[-limit:]
    return text[:limit] + note


def trim_result(tool, result):
    """Shortens the text parts of a tools/call result, in place. Returns it."""
    if not isinstance(result, dict):
        return result
    for part in result.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            part["text"] = shorten(part.get("text"), keep_end=(tool == "app_logs"))
    if "structuredContent" in result:
        # The same facts again as JSON: the text part is enough for the model
        del result["structuredContent"]
    return result


def filter_tools(result):
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        result["tools"] = [t for t in result["tools"] if isinstance(t, dict) and t.get("name") in ALLOWED_TOOLS]
        for t in result["tools"]:
            # The answer schema is not needed to call a tool, and it costs the model reading time
            t.pop("outputSchema", None)
    return result


def rpc_error(msg_id, code, text):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}


class AIConnect:
    """The owner's AI Connect key and the MCP pass-through."""

    def __init__(self, state_dir, url=AI_CONNECT_URL, post=post_mcp, clock=time.time):
        self.path = os.path.join(state_dir, "aiconnect.json")
        self.url = url
        self.post = post
        self.clock = clock
        self.lock = threading.Lock()
        self.data = self._load()
        self.last_error = None

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) and clean_key(d.get("key")) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    @property
    def connected(self):
        return bool(self.data.get("key"))

    def check(self, key):
        """Asks AI Connect which tools this key may use. Returns None or a sentence for the owner."""
        try:
            status, init = self.post(self.url, key, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                           "clientInfo": {"name": "avado-private-assistant", "version": "0.2"}}})
            if status in (401, 403):
                return "AI Connect did not accept this key. Make a new connection on the AI Connect page and copy its key."
            if status == 429:
                return "AI Connect is busy. Please try again in a few minutes."
            if status != 200 or "result" not in init:
                return "AI Connect did not answer as expected. Is it installed and running?"
            status, listed = self.post(self.url, key, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        except (OSError, ValueError) as e:
            sys.stderr.write("avado-setup: AI Connect check failed: %r\n" % e)
            return "AI Connect cannot be reached. Install it from the AVADO store, or start it, then try again."
        names = [t.get("name") for t in (listed.get("result") or {}).get("tools") or [] if isinstance(t, dict)]
        if any(n in ACTION_TOOLS for n in names):
            return ("This key may also restart or install apps. The assistant only needs to look: make a connection "
                    "without extra permissions on the AI Connect page and use its key.")
        if not any(n in ALLOWED_TOOLS for n in names):
            return "This key cannot read anything on the box. Make a new connection on the AI Connect page."
        return None

    def connect(self, raw_key):
        key = clean_key(raw_key)
        if not key:
            return "That does not look like an AI Connect key. It starts with avado_ and is shown once when you make a connection."
        problem = self.check(key)
        if problem:
            return problem
        with self.lock:
            self.data = {"key": key, "hint": key[:10], "at": int(self.clock())}
            self._save()
        self.last_error = None
        return None

    def disconnect(self):
        with self.lock:
            self.data = {}
            try:
                os.remove(self.path)
            except OSError:
                pass

    def status(self):
        return {"connected": self.connected, "hint": self.data.get("hint"), "page": AI_CONNECT_PAGE,
                "tools": list(ALLOWED_TOOLS), "problem": self.last_error}

    # -- the pass-through for Open WebUI's MCP client
    def handle(self, body_bytes):
        """One POST from Open WebUI. Returns (http status, response dict or None for 202)."""
        try:
            msg = json.loads(body_bytes or b"null")
        except ValueError:
            return 400, rpc_error(None, -32700, "Parse error")
        if isinstance(msg, list):
            return 400, rpc_error(None, -32600, "Batches are not supported")
        if not isinstance(msg, dict):
            return 400, rpc_error(None, -32600, "Invalid request")
        msg_id = msg.get("id")
        method = msg.get("method")
        key = self.data.get("key")
        if not key:
            return 200, rpc_error(msg_id, -32001, "Live box checks are off. The owner can turn them on on the assistant's setup page.")
        if method == "tools/call":
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            name = params.get("name")
            if name not in ALLOWED_TOOLS:
                return 200, rpc_error(msg_id, -32602, "The assistant may not use %s." % str(name)[:60])
            if name == "app_logs":
                args = dict(params.get("arguments") or {})
                try:
                    lines = int(args.get("lines") or LOG_LINES)
                except (TypeError, ValueError):
                    lines = LOG_LINES
                args["lines"] = max(1, min(lines, LOG_LINES))
                params = dict(params, arguments=args)
                msg = dict(msg, params=params)
        try:
            status, out = self.post(self.url, key, msg, timeout=60)
        except (OSError, ValueError) as e:
            self.last_error = "AI Connect cannot be reached right now."
            sys.stderr.write("avado-setup: AI Connect call failed: %r\n" % e)
            if msg_id is None:
                return 202, None
            return 200, rpc_error(msg_id, -32002, "AI Connect cannot be reached right now.")
        if status in (401, 403):
            self.last_error = "AI Connect no longer accepts the key. Connect again with a new key."
        elif status == 200:
            self.last_error = None
        if msg_id is None or status == 202:
            return 202, None
        if method == "tools/list":
            filter_tools(out.get("result"))
        elif method == "tools/call":
            trim_result(msg["params"].get("name"), out.get("result"))
        if status != 200 and "error" not in out:
            out = rpc_error(msg_id, -32003, "AI Connect answered %d." % status)
        return 200, out


def tool_server_connection(internal_token, enabled=True):
    """Open WebUI's tool server entry (Admin settings, External tools) for the pass-through."""
    return {
        "url": "http://127.0.0.1:8090/internal/mcp",
        "path": "",
        "type": "mcp",
        "auth_type": "bearer",
        "key": internal_token,
        "headers": None,
        "forward_cookies": False,
        "config": {"enable": bool(enabled), "function_name_filter_list": ",".join(ALLOWED_TOOLS),
                   # no grants: only the owner (Open WebUI admins) can use the box checks
                   "access_grants": []},
        "info": {"id": SERVER_ID, "name": "Your AVADO",
                 "description": "Live, read-only checks of this box through AI Connect."},
    }
