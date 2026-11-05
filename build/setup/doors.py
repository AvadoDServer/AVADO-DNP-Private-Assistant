"""AVADO Private Assistant: doors for ONE named app on the box (Home Assistant).

Everything else in this package listens on 127.0.0.1 or behind the sign-in. These doors let exactly
one other app use the assistant, by name (AVADO_APP_DOORS, default homeassistant.my.ava.do):

* the model, Ollama's API, on assistant.my.ava.do:11434
* speech to text (Wyoming) on assistant.my.ava.do:10300, while voice is on
* text to speech (Wyoming) on assistant.my.ava.do:10200, while voice is on

A connection is accepted only when it comes from the address the box's own name server gives for that
app's name (looked up again when an unknown address knocks, at most every 5 seconds, and every 60
seconds anyway, since an app gets a new address when it is recreated). Everyone else gets nothing:
the connection is closed (TCP) or answered 403 (HTTP).

The model door also wants a key (Authorization: Bearer ...), which the app gets once from
POST /avado/connect, a call only the named app can make. It lets through only what a chat needs:
GET /api/version, /api/tags, /api/ps and POST /api/show, /api/chat, /api/generate. Nothing can be
downloaded, copied, created or deleted through it. Every chat gets the assistant's own settings
(thinking off, half the processor threads, the 8k context, at most 10 minutes in memory after the
last use), so a chat from the app never makes the model load twice or use the whole processor, and a
chat waits for nothing while the assistant is paused for the node ("Paused so your Ethereum node has
enough memory").
"""

import http.client
import ipaddress
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingTCPServer, BaseRequestHandler

import voice as speech

APP_NAMES = [n.strip() for n in os.environ.get("AVADO_APP_DOORS", "homeassistant.my.ava.do").split(",") if n.strip()]
OLLAMA_PORT = 11434
GET_PATHS = ("/api/version", "/api/tags", "/api/ps")
POST_PATHS = ("/api/show", "/api/chat", "/api/generate")
MAX_BODY = 2 * 1024 * 1024
KEEP_ALIVE_MAX = 600
# The AVADO network (DAppNode's dncore_network). It is not in a private range, so it is named here.
AVADO_NETWORK = ipaddress.ip_network(os.environ.get("AVADO_NETWORK", "172.33.0.0/16"))
CONTEXT = int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "8192"))


def own_address():
    """The container's address on the AVADO network (Docker puts it in /etc/hosts for the host name)."""
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def lookup(name):
    """The app's addresses on the box's own network. When the app is not installed, the box's name server
    passes the name on to the internet (*.my.ava.do has a public address there): such an address is
    never an app on this box, so only addresses on the AVADO network (or a private one) count."""
    try:
        ips = socket.gethostbyname_ex(name)[2]
    except OSError:
        return set()
    out = set()
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (a in AVADO_NETWORK or a.is_private) and not a.is_loopback:
            out.add(ip)
    return out


class Peers:
    """Which addresses belong to the named app(s), from the box's name server."""

    def __init__(self, names=None, resolve=lookup, clock=time.monotonic, refresh=60, retry=5):
        self.names = list(names if names is not None else APP_NAMES)
        self.resolve = resolve
        self.clock = clock
        self.refresh = refresh
        self.retry = retry
        self.ips = set()
        self.at = None
        self.lock = threading.Lock()

    def _update(self):
        ips = set()
        for n in self.names:
            ips |= self.resolve(n)
        self.ips = ips
        self.at = self.clock()

    def allowed(self, ip):
        with self.lock:
            now = self.clock()
            if self.at is None or now - self.at >= self.refresh:
                self._update()
            elif ip not in self.ips and now - self.at >= self.retry:
                self._update()
            return ip in self.ips

    def name_of(self, ip):
        return self.names[0] if ip in self.ips and self.names else None


class Door:
    """The key the app uses, kept in the data volume (mode 600)."""

    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "app-door.json")
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("key"), str) and len(d["key"]) >= 32:
                return d
        except (OSError, ValueError):
            pass
        return {}

    def key(self):
        if not self.data.get("key"):
            self.data = {"key": secrets.token_urlsafe(32), "at": int(time.time())}
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        return self.data["key"]

    def check(self, header):
        h = header or ""
        if not h.lower().startswith("bearer "):
            return False
        return bool(self.data.get("key")) and secrets.compare_digest(h[7:].strip().encode(), self.data["key"].encode())


def shape_request(path, body, threads):
    """The assistant's own settings on a chat from the app. Returns the new body (dict)."""
    if path in ("/api/chat", "/api/generate"):
        body["think"] = False
        opts = dict(body.get("options") or {}) if isinstance(body.get("options"), dict) else {}
        opts["num_thread"] = threads
        opts["num_ctx"] = CONTEXT
        body["options"] = opts
        ka = body.get("keep_alive")
        secs = None
        if isinstance(ka, (int, float)) and not isinstance(ka, bool):
            secs = ka
        elif isinstance(ka, str):
            t = ka.strip().lower()
            try:
                secs = float(t[:-1]) * {"s": 1, "m": 60, "h": 3600}[t[-1]] if t and t[-1] in "smh" else float(t)
            except (ValueError, KeyError):
                secs = None
        if secs is None or secs < 0 or secs > KEEP_ALIVE_MAX:
            body["keep_alive"] = "10m"
    return body


def make_ollama_handler(manager, peers, door, ollama_host="127.0.0.1", ollama_port=OLLAMA_PORT):
    class Handler(BaseHTTPRequestHandler):
        server_version = "avado-assistant"
        sys_version = ""
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):
            pass

        def _json(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _peer_ok(self):
            if peers.allowed(self.client_address[0]):
                return True
            self._json(403, {"error": "Only the app this door is for may use it."})
            return False

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n > MAX_BODY:
                return None
            raw = self.rfile.read(n) if n else b"{}"
            try:
                b = json.loads(raw or b"{}")
            except ValueError:
                return None
            return b if isinstance(b, dict) else None

        def do_GET(self):
            if not self._peer_ok():
                return
            path = self.path.split("?", 1)[0]
            if path == "/avado/status":
                if not door.check(self.headers.get("Authorization")):
                    self._json(401, {"error": "Connect first (POST /avado/connect)."})
                    return
                self._json(200, manager.app_status())
                return
            if path not in GET_PATHS:
                self._json(403, {"error": "Not allowed through this door."})
                return
            if not door.check(self.headers.get("Authorization")):
                self._json(401, {"error": "unauthorized"})
                return
            self._pass("GET", path, None)

        def do_POST(self):
            if not self._peer_ok():
                return
            path = self.path.split("?", 1)[0]
            body = self._body()
            if body is None:
                self._json(400, {"error": "Send a JSON body of at most 2 MB."})
                return
            if path == "/avado/connect":
                self._json(200, manager.app_connect(peers.name_of(self.client_address[0]), door.key()))
                return
            if not door.check(self.headers.get("Authorization")):
                self._json(401, {"error": "unauthorized"})
                return
            if path == "/avado/voice":
                self._json(200, manager.app_voice(bool(body.get("on")), body.get("language"),
                                                  peers.name_of(self.client_address[0])))
                return
            if path not in POST_PATHS:
                self._json(403, {"error": "Not allowed through this door."})
                return
            if path in ("/api/chat", "/api/generate"):
                if manager.guard(body.get("model")):
                    self._json(503, {"error": manager.paused_message()})
                    return
            body = shape_request(path, body, manager.threads)
            self._pass("POST", path, json.dumps(body).encode())

        def do_DELETE(self):
            if self._peer_ok():
                self._json(403, {"error": "Not allowed through this door."})

        do_PUT = do_DELETE
        do_HEAD = do_DELETE

        def _pass(self, method, path, data):
            conn = http.client.HTTPConnection(ollama_host, ollama_port, timeout=900)
            try:
                headers = {"Content-Type": "application/json"} if data is not None else {}
                conn.request(method, path, body=data, headers=headers)
                r = conn.getresponse()
                self.send_response(r.status)
                self.send_header("Content-Type", r.getheader("Content-Type") or "application/json")
                self.end_headers()
                while True:
                    chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError as e:
                sys.stderr.write("avado-setup: model door: %r\n" % e)
                try:
                    self._json(502, {"error": "The assistant's model is not running right now."})
                except OSError:
                    pass
            finally:
                conn.close()

    return Handler


def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def make_tcp_handler(peers, target_port, is_open):
    class Handler(BaseRequestHandler):
        def handle(self):
            if not peers.allowed(self.client_address[0]) or not is_open():
                return
            try:
                up = socket.create_connection(("127.0.0.1", target_port), timeout=10)
            except OSError:
                return
            up.settimeout(None)
            self.request.settimeout(None)
            t = threading.Thread(target=pump, args=(up, self.request), daemon=True)
            t.start()
            pump(self.request, up)
            t.join(timeout=600)
            up.close()

    return Handler


class TCPServer(ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class HTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start(manager, voice, peers=None, door=None, address=None):
    """Opens the three doors on the container's AVADO address. Returns the servers (or [] if there is no
    such address, e.g. in a test without a network)."""
    address = address or own_address()
    if not address:
        sys.stderr.write("avado-setup: no AVADO network address; the doors for other apps stay closed\n")
        return []
    peers = peers or Peers()
    door = door or Door(manager.state_dir)
    servers = []
    plan = [
        (HTTPServer, OLLAMA_PORT, make_ollama_handler(manager, peers, door)),
        (TCPServer, speech.PUBLIC_STT_PORT, make_tcp_handler(peers, speech.STT_PORT, lambda: voice.on)),
        (TCPServer, speech.PUBLIC_TTS_PORT, make_tcp_handler(peers, speech.TTS_PORT, lambda: voice.on)),
    ]
    for cls, port, handler in plan:
        try:
            srv = cls((address, port), handler)
        except OSError as e:
            sys.stderr.write("avado-setup: could not open the door on %s:%d: %r\n" % (address, port, e))
            continue
        threading.Thread(target=srv.serve_forever, name="door-%d" % port, daemon=True).start()
        servers.append(srv)
    sys.stderr.write("avado-setup: doors for %s open on %s (model %d, speech 10300/10200)\n" % (
        ", ".join(peers.names), address, OLLAMA_PORT))
    return servers
