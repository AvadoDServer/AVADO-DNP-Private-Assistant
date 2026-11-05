"""Tests for 0.2.0: live box checks (AI Connect), "Explain this", the doors for Home Assistant, voice."""

import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import aiconnect as ac  # noqa: E402
import avado_setup as s  # noqa: E402
import doors  # noqa: E402
import explain as ex  # noqa: E402
import voice as vo  # noqa: E402
from test_setup import CATALOG, PUBLIC, FakeFacts, FakeMemory, FakeOllama, FakeWebUI, tag_of  # noqa: E402

KEY = "avado_" + "A" * 43


def fake_post(tools, status=200, calls=None):
    """An AI Connect stand-in: post(url, key, message) -> (status, dict)."""

    def post(url, key, msg, timeout=30):
        if calls is not None:
            calls.append((key, msg))
        m = msg.get("method")
        if m == "initialize":
            return status, {"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": ac.PROTOCOL}}
        if m == "tools/list":
            return status, {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
                {"name": n, "description": "d", "inputSchema": {"type": "object"}, "outputSchema": {"x": 1}}
                for n in tools]}}
        if m == "tools/call":
            text = "line\n" * 4000 if msg["params"]["name"] == "app_logs" else "x" * 9000
            return status, {"jsonrpc": "2.0", "id": msg["id"], "result": {
                "content": [{"type": "text", "text": text}], "structuredContent": {"big": True}}}
        return 202, {}

    return post


class AIConnectKeys(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_key_shapes(self):
        self.assertEqual(ac.clean_key("  %s \n" % KEY), KEY)
        self.assertEqual(ac.clean_key("Bearer " + KEY), KEY)
        self.assertEqual(ac.clean_key("https://box.tail.ts.net/k/%s/mcp" % KEY), KEY)
        self.assertIsNone(ac.clean_key("hello"))
        self.assertIsNone(ac.clean_key("avado_short"))

    def test_a_read_only_key_is_kept_with_mode_600(self):
        a = ac.AIConnect(self.dir, post=fake_post(["box_overview", "health_check", "list_apps", "app_logs"]))
        self.assertIsNone(a.connect(KEY))
        self.assertTrue(a.connected)
        st = os.stat(os.path.join(self.dir, "aiconnect.json"))
        self.assertEqual(st.st_mode & 0o777, 0o600)
        self.assertTrue(ac.AIConnect(self.dir).connected)  # survives a restart
        self.assertNotIn(KEY, json.dumps(a.status()))       # the page never sees the key
        a.disconnect()
        self.assertFalse(a.connected)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "aiconnect.json")))

    def test_a_key_that_may_act_is_refused(self):
        a = ac.AIConnect(self.dir, post=fake_post(["health_check", "restart_app"]))
        self.assertIn("only needs to look", a.connect(KEY))
        self.assertFalse(a.connected)

    def test_wrong_key_and_no_ai_connect(self):
        self.assertIn("did not accept", ac.AIConnect(self.dir, post=fake_post([], status=401)).connect(KEY))

        def down(*a, **k):
            raise OSError("no route")
        self.assertIn("cannot be reached", ac.AIConnect(self.dir, post=down).connect(KEY))
        self.assertIn("does not look like", ac.AIConnect(self.dir).connect("nope"))


class AIConnectPassThrough(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.calls = []
        self.a = ac.AIConnect(self.dir, post=fake_post(
            ["box_overview", "health_check", "list_apps", "app_logs", "ethereum_status", "store_apps"], calls=self.calls))
        self.a.connect(KEY)
        self.calls.clear()

    def call(self, msg):
        return self.a.handle(json.dumps(msg).encode())

    def test_only_four_tools_are_listed(self):
        code, out = self.call({"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}})
        self.assertEqual(code, 200)
        names = [t["name"] for t in out["result"]["tools"]]
        self.assertEqual(sorted(names), sorted(ac.ALLOWED_TOOLS))
        self.assertTrue(all("outputSchema" not in t for t in out["result"]["tools"]))
        self.assertEqual(self.calls[0][0], KEY)  # the owner's key goes to AI Connect

    def test_other_tools_are_refused_here(self):
        code, out = self.call({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                               "params": {"name": "store_apps", "arguments": {}}})
        self.assertIn("error", out)
        self.assertEqual(self.calls, [])

    def test_logs_are_capped_and_answers_shortened(self):
        code, out = self.call({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                               "params": {"name": "app_logs", "arguments": {"app": "geth", "lines": 500}}})
        self.assertEqual(self.calls[0][1]["params"]["arguments"]["lines"], ac.LOG_LINES)
        text = out["result"]["content"][0]["text"]
        self.assertLessEqual(len(text), ac.MAX_RESULT_CHARS + 40)
        self.assertTrue(text.startswith("[older lines left out]"))
        self.assertNotIn("structuredContent", out["result"])
        code, out = self.call({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                               "params": {"name": "health_check", "arguments": {}}})
        self.assertIn("[shortened", out["result"]["content"][0]["text"])

    def test_notifications_get_202(self):
        self.assertEqual(self.call({"jsonrpc": "2.0", "method": "notifications/initialized"}), (202, None))

    def test_off_without_a_key(self):
        self.a.disconnect()
        code, out = self.call({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
        self.assertIn("off", out["error"]["message"])

    def test_open_webui_entry(self):
        c = ac.tool_server_connection("internal")
        self.assertEqual(c["type"], "mcp")
        self.assertEqual(c["key"], "internal")
        self.assertTrue(c["url"].startswith("http://127.0.0.1:8090/internal/"))
        self.assertEqual(c["config"]["access_grants"], [])  # the owner only
        self.assertEqual(c["config"]["function_name_filter_list"].split(","), list(ac.ALLOWED_TOOLS))


FINDING = {
    "finding": {"id": "disk-high", "severity": "warning", "topic": "storage", "title": "Your disk is 91% full",
                "why": "When it is full, your node stops. password=hunter2 " + "ab" * 40,
                "fix": {"kind": "action", "label": "Free up space"}},
    "app": {"title": "Geth", "version": "10.0.84", "running": True},
    "context": {"verdict": "Needs attention", "disk": {"pct": 91.4},
                "processes": [{"name": "geth", "state": "RUNNING", "lastError": "Bearer abc.def"}],
                "sync": {"syncing": False, "peers": 47}},
}


class Explain(unittest.TestCase):
    def test_payload_is_scrubbed_and_clipped(self):
        p = ex.clean_payload(json.loads(json.dumps(FINDING)))
        self.assertNotIn("hunter2", p["finding"]["why"])
        self.assertNotIn("ab" * 40, p["finding"]["why"])
        self.assertEqual(p["context"]["processes"][0]["lastError"], "Bearer <redacted>")
        self.assertEqual(p["finding"]["fix"], "Free up space")
        text = ex.facts_text(p)
        self.assertIn('Fix button: "Free up space"', text)
        self.assertIn("Disk: 91% full", text)

    def test_twelve_words_are_hidden(self):
        words = " ".join(["abandon"] * 11 + ["about"])
        self.assertNotIn(words, ex.scrub("my words " + words))
        self.assertIn("<12+ words redacted>", ex.scrub("my words " + words))

    def test_bad_payloads(self):
        for bad in (None, [], {}, {"finding": {}}, {"finding": {"title": "  "}}):
            with self.assertRaises(ex.PayloadError):
                ex.clean_payload(bad)

    def test_money_findings_point_to_support(self):
        p = ex.clean_payload({"finding": {"id": "fee-recipient-missing", "topic": "setup", "title": "No fee recipient"}})
        self.assertIn("support@ava.do", ex.build_messages(p)[0]["content"])
        p = ex.clean_payload(FINDING)
        self.assertNotIn("support@ava.do", ex.build_messages(p)[0]["content"])

    def test_question_is_used(self):
        p = ex.clean_payload(dict(FINDING, question="Ali moram kaj narediti?"))
        self.assertIn("The owner asks: Ali moram kaj narediti?", ex.build_messages(p)[1]["content"])

    def test_one_at_a_time_and_cache(self):
        gate = threading.Event()
        calls = []

        def chat(messages, deadline):
            calls.append(messages)
            gate.wait(2)
            yield "It is "
            yield "nearly full."

        e = ex.Explainer(chat)
        p = ex.clean_payload(FINDING)
        first = e.run(p, "qwen3.5:4b")
        self.assertEqual(next(first), ("text", "It is "))   # now writing
        with self.assertRaises(ex.Busy):
            e.run(p, "qwen3.5:4b")
        gate.set()
        rest = list(first)
        self.assertEqual(rest[-1], ("done", {"cached": False, "stopped": False}))
        again = list(e.run(p, "qwen3.5:4b"))
        self.assertEqual(again, [("text", "It is nearly full."), ("done", {"cached": True, "stopped": False})])
        self.assertEqual(len(calls), 1)
        list(e.run(p, "qwen3.5:9b"))   # another model: asked again
        self.assertEqual(len(calls), 2)

    def test_two_minute_cap(self):
        t = [0.0]

        def chat(messages, deadline):
            for i in range(10):
                t[0] += 30
                yield "w%d " % i

        e = ex.Explainer(chat, clock=lambda: t[0])
        out = list(e.run(ex.clean_payload(FINDING), "m"))
        self.assertEqual(out[-1], ("done", {"cached": False, "stopped": True}))
        self.assertLessEqual(len(out), 6)
        self.assertFalse(e.cache)  # a cut-off answer is not kept

    def test_origins(self):
        self.assertTrue(ex.origin_allowed("http://my.ava.do"))
        self.assertTrue(ex.origin_allowed("https://my.ava.do"))
        for o in ("http://evil.example", "http://my.ava.do.evil.example", None, "null"):
            self.assertFalse(ex.origin_allowed(o))


class FakeVoice:
    def __init__(self):
        self.on = False
        self.calls = []

    def gib(self):
        return vo.VOICE_GIB if self.on else 0.0

    def installed(self):
        return True

    def set(self, on, language=None, by=None):
        self.on = on
        self.calls.append((on, language, by))

    def status(self):
        return {"on": self.on, "ready": self.on, "stt_port": 10300, "tts_port": 10200}


class FakeTools:
    def __init__(self, connected=False):
        self.connected = connected

    def status(self):
        return {"connected": self.connected}

    def connect(self, key):
        self.connected = True

    def disconnect(self):
        self.connected = False


def manager(dir_, installed=("qwen3.5:4b",), free=20.0, ollama=None):
    m = s.Manager(CATALOG, ollama or FakeOllama(list(installed)), FakeWebUI(), state_dir=dir_,
                  memory=FakeMemory(free, free), facts=FakeFacts(), free_bytes_fn=lambda: 3e12, warmup_seconds=10 ** 9,
                  threads=8, tools=FakeTools(), voice=FakeVoice())
    m.step()
    return m


class SpeechMemory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_voice_counts_in_the_pause_line(self):
        m = manager(self.dir, free=10.0)   # qwen3.5:4b needs 4.2 + 5.5 = 9.7
        m.check_memory()
        self.assertFalse(m.paused)
        m.voice.on = True                  # + 0.6 = 10.3
        m.check_memory()
        self.assertTrue(m.paused)

    def test_voice_counts_in_the_suggestion(self):
        self.assertEqual(s.recommend(CATALOG, 12.3, 3e12)["id"], "medium")
        self.assertEqual(s.recommend(CATALOG, 12.3, 3e12, extra_gib=0.6)["id"], "small")
        self.assertIsNone(s.recommend(CATALOG, 7.8, 3e12, extra_gib=0.6))

    def test_app_calls(self):
        m = manager(self.dir)
        st = m.app_connect("homeassistant.my.ava.do", "k" * 40)
        self.assertEqual(st["key"], "k" * 40)
        self.assertEqual(st["model"], "qwen3.5:4b")
        self.assertEqual(st["model_name"], "Qwen3.5 4B")
        st = m.app_voice(True, "sl", "homeassistant.my.ava.do")
        self.assertTrue(st["voice"]["on"])
        self.assertEqual(m.voice.calls[-1], (True, "sl", "homeassistant.my.ava.do"))
        self.assertEqual(m.status()["voice"]["apps"], ["homeassistant.my.ava.do"])


class Doors(unittest.TestCase):
    def test_peers_by_name(self):
        t = [0.0]
        table = {"homeassistant.my.ava.do": {"172.33.0.9"}}
        p = doors.Peers(["homeassistant.my.ava.do"], resolve=lambda n: set(table.get(n, ())), clock=lambda: t[0])
        self.assertTrue(p.allowed("172.33.0.9"))
        self.assertFalse(p.allowed("172.33.0.10"))
        table["homeassistant.my.ava.do"] = {"172.33.0.10"}   # Home Assistant was recreated
        self.assertFalse(p.allowed("172.33.0.10"))           # looked up again only after 5 s
        t[0] = 6
        self.assertTrue(p.allowed("172.33.0.10"))
        self.assertFalse(p.allowed("172.33.0.9"))

    def test_chat_gets_the_assistants_settings(self):
        b = doors.shape_request("/api/chat", {"model": "m", "keep_alive": -1, "think": True,
                                              "options": {"num_ctx": 32768, "num_thread": 16}}, 8)
        self.assertEqual(b["keep_alive"], "10m")
        self.assertFalse(b["think"])
        self.assertEqual(b["options"]["num_thread"], 8)
        self.assertEqual(b["options"]["num_ctx"], doors.CONTEXT)
        self.assertEqual(doors.shape_request("/api/chat", {"keep_alive": "5m"}, 8)["keep_alive"], "5m")
        self.assertEqual(doors.shape_request("/api/chat", {"keep_alive": "2h"}, 8)["keep_alive"], "10m")
        self.assertEqual(doors.shape_request("/api/show", {"model": "m"}, 8), {"model": "m"})


class OllamaStub(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"models": [{"name": "qwen3.5:4b", "model": "qwen3.5:4b"}]}).encode())

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        OllamaStub.seen.append((self.path, json.loads(self.rfile.read(n))))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for w in ("Hel", "lo"):
            self.wfile.write((json.dumps({"message": {"content": w}, "done": False}) + "\n").encode())
            self.wfile.flush()
        self.wfile.write((json.dumps({"done": True}) + "\n").encode())


class DoorHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.stub = ThreadingHTTPServer(("127.0.0.1", 0), OllamaStub)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()
        cls.m = manager(cls.dir)
        cls.allowed = {"127.0.0.1"}
        cls.peers = doors.Peers(["homeassistant.my.ava.do"], resolve=lambda n: set(cls.allowed))
        cls.door = doors.Door(cls.dir)
        cls.srv = doors.HTTPServer(("127.0.0.1", 0), doors.make_ollama_handler(
            cls.m, cls.peers, cls.door, ollama_port=cls.stub.server_address[1]))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for x in (cls.srv, cls.stub):
            x.shutdown()
            x.server_close()
        shutil.rmtree(cls.dir)

    def req(self, method, path, body=None, key=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        h = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = "Bearer " + key
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_connect_then_chat(self):
        code, data = self.req("POST", "/avado/connect", {})
        self.assertEqual(code, 200)
        info = json.loads(data)
        key = info["key"]
        self.assertEqual(info["model"], "qwen3.5:4b")
        self.assertEqual(oct(os.stat(os.path.join(self.dir, "app-door.json")).st_mode & 0o777), "0o600")
        self.assertEqual(self.req("GET", "/api/tags")[0], 401)           # no key
        self.assertEqual(self.req("GET", "/api/tags", key="wrong" * 8)[0], 401)
        code, data = self.req("GET", "/api/tags", key=key)
        self.assertEqual(code, 200)
        code, data = self.req("POST", "/api/chat", {"model": "qwen3.5:4b", "messages": [], "keep_alive": -1}, key=key)
        self.assertEqual(code, 200)
        self.assertIn(b"Hel", data)
        path, sent = OllamaStub.seen[-1]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(sent["options"]["num_thread"], 8)
        self.assertEqual(sent["keep_alive"], "10m")
        self.assertFalse(sent["think"])

    def test_nothing_else_goes_through(self):
        key = json.loads(self.req("POST", "/avado/connect", {})[1])["key"]
        for path in ("/api/pull", "/api/delete", "/api/create", "/api/copy", "/api/push", "/api/blobs/x"):
            self.assertEqual(self.req("POST", path, {"model": "x"}, key=key)[0], 403, path)
        self.assertEqual(self.req("DELETE", "/api/delete", {"model": "x"}, key=key)[0], 403)

    def test_other_apps_are_turned_away(self):
        self.allowed.clear()
        try:
            self.peers.at = None
            self.assertEqual(self.req("POST", "/avado/connect", {})[0], 403)
            self.assertEqual(self.req("GET", "/api/tags")[0], 403)
        finally:
            self.allowed.add("127.0.0.1")
            self.peers.at = None

    def test_voice_switch(self):
        key = json.loads(self.req("POST", "/avado/connect", {})[1])["key"]
        self.assertEqual(self.req("POST", "/avado/voice", {"on": True})[0], 401)
        code, data = self.req("POST", "/avado/voice", {"on": True, "language": "de"}, key=key)
        self.assertTrue(json.loads(data)["voice"]["on"])
        self.req("POST", "/avado/voice", {"on": False}, key=key)
        self.assertFalse(self.m.voice.on)

    def test_paused_answers_503(self):
        key = json.loads(self.req("POST", "/avado/connect", {})[1])["key"]
        self.m.memory.now = 1.0
        try:
            code, data = self.req("POST", "/api/chat", {"model": "qwen3.5:4b", "messages": []}, key=key)
            self.assertEqual(code, 503)
            self.assertIn("Paused", json.loads(data)["error"])
        finally:
            self.m.memory.now = 20.0
            self.m.check_memory()


class TcpDoor(unittest.TestCase):
    def test_only_the_app_and_only_while_on(self):
        echo = socket.socket()
        echo.bind(("127.0.0.1", 0))
        echo.listen(4)
        port = echo.getsockname()[1]

        def serve():
            while True:
                try:
                    c, _ = echo.accept()
                except OSError:
                    return
                c.sendall(c.recv(100).upper())
                c.close()
        threading.Thread(target=serve, daemon=True).start()
        state = {"on": True, "ips": {"127.0.0.1"}}
        peers = doors.Peers(["ha"], resolve=lambda n: set(state["ips"]))
        srv = doors.TCPServer(("127.0.0.1", 0), doors.make_tcp_handler(peers, port, lambda: state["on"]))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(echo.close)

        def talk():
            c = socket.create_connection(srv.server_address, timeout=3)
            try:
                c.sendall(b"hi")
                return c.recv(100)
            except ConnectionResetError:
                return b""   # turned away: closed with or without a reset
            finally:
                c.close()
        self.assertEqual(talk(), b"HI")
        state["on"] = False
        self.assertEqual(talk(), b"")
        state["on"], state["ips"], peers.at = True, set(), None
        self.assertEqual(talk(), b"")


class FakeProc:
    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


class VoiceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.procs = []
        self.t = [0.0]
        old = vo.VOICE_BIN, vo.VOICE_DIR
        bin_ = os.path.join(self.dir, "bin")
        os.makedirs(bin_)
        for n in ("wyoming-faster-whisper", "wyoming-piper"):
            open(os.path.join(bin_, n), "w").close()
        vo.VOICE_BIN, vo.VOICE_DIR = bin_, os.path.join(self.dir, "voice")
        self.addCleanup(lambda: setattr(vo, "VOICE_BIN", old[0]) or setattr(vo, "VOICE_DIR", old[1]))

        def popen(cmd, **kw):
            p = FakeProc(cmd, **kw)
            self.procs.append(p)
            return p
        self.v = vo.Voice(self.dir, popen=popen, clock=lambda: self.t[0], probe=lambda port: True)

    def test_off_by_default_then_on_then_off(self):
        self.v.tick()
        self.assertEqual(self.procs, [])
        self.v.set(True, "sl-SI", by="homeassistant.my.ava.do")
        self.assertEqual(len(self.procs), 2)
        stt, tts = self.procs
        self.assertIn("--language", stt.cmd)
        self.assertEqual(stt.cmd[stt.cmd.index("--language") + 1], "sl")
        self.assertEqual(tts.cmd[tts.cmd.index("--voice") + 1], "sl_SI-artur-medium")
        self.assertIn("nice -n 10", stt.cmd[2])
        self.assertTrue(self.v.status()["ready"])
        self.assertTrue(vo.Voice(self.dir).on)   # kept in the volume
        self.v.set(False)
        self.assertTrue(stt.terminated and tts.terminated)
        self.assertFalse(self.v.status()["on"])
        self.assertEqual(self.v.gib(), 0.0)

    def test_restarted_with_a_growing_pause(self):
        self.v.set(True, "en")
        self.procs[0].code = 1          # whisper stopped
        self.v.tick()                   # noticed: wait 2 s
        self.assertEqual(len(self.procs), 2)
        self.t[0] = 3
        self.v.tick()
        self.assertEqual(len(self.procs), 3)
        self.procs[2].code = 1
        self.v.tick()
        self.t[0] = 6
        self.v.tick()
        self.assertEqual(len(self.procs), 3)  # now 4 s
        self.t[0] = 8
        self.v.tick()
        self.assertEqual(len(self.procs), 4)

    def test_language_change_restarts_with_the_new_voice(self):
        self.v.set(True, "en")
        self.v.set(True, "de")
        self.assertTrue(self.procs[0].terminated)
        self.assertEqual(self.procs[-1].cmd[self.procs[-1].cmd.index("--voice") + 1], "de_DE-thorsten-medium")

    def test_voices(self):
        self.assertEqual(vo.voice_for("en-GB"), "en_US-lessac-medium")
        self.assertEqual(vo.voice_for("hr"), "en_US-lessac-medium")
        self.assertEqual(vo.language_code("../x"), "en")


class HttpV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.m = manager(cls.dir)
        cls.chats = []

        def chat(messages, deadline):
            cls.chats.append(messages)
            yield "Your disk "
            yield "is nearly full."
        cls.m.explainer = ex.Explainer(chat)
        cls.server = s.ThreadingHTTPServer(("127.0.0.1", 0), s.make_handler(cls.m, PUBLIC))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.dir)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=data, headers=headers or {})
        r = c.getresponse()
        out = r.read()
        c.close()
        return r, out

    ADMIN = {"Origin": "http://my.ava.do", "X-Avado-Request": "1", "Content-Type": "application/json"}

    def test_preflight_only_for_the_admin(self):
        r, _ = self.req("OPTIONS", "/avado/api/explain", headers={"Origin": "http://my.ava.do"})
        self.assertEqual(r.status, 204)
        self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "http://my.ava.do")
        self.assertIn("X-Avado-Request", r.getheader("Access-Control-Allow-Headers"))
        r, _ = self.req("OPTIONS", "/avado/api/explain", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("OPTIONS", "/avado/api/choose", headers={"Origin": "http://my.ava.do"})
        self.assertEqual(r.status, 403)

    def test_status_is_readable_by_the_admin(self):
        r, data = self.req("GET", "/avado/api/status", headers={"Origin": "https://my.ava.do"})
        self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "https://my.ava.do")
        st = json.loads(data)
        self.assertTrue(st["api"]["explain"])
        self.assertIn("voice", st)
        self.assertIn("tools", st)
        r, _ = self.req("GET", "/avado/api/status", headers={"Origin": "http://evil.example"})
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

    def test_explain_streams_and_caches(self):
        r, data = self.req("POST", "/avado/api/explain", FINDING, self.ADMIN)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "http://my.ava.do")
        events = [line[6:] for line in data.decode().splitlines() if line.startswith("data: ")]
        self.assertEqual(json.loads(events[0]), {"text": "Your disk "})
        self.assertEqual(json.loads(events[-1]), {"cached": False, "stopped": False})
        n = len(self.chats)
        r, data = self.req("POST", "/avado/api/explain", FINDING, self.ADMIN)
        self.assertIn('"cached": true', data.decode())
        self.assertEqual(len(self.chats), n)
        self.assertNotIn("hunter2", json.dumps(self.chats[-1]))

    def test_explain_refuses_other_pages_and_bad_bodies(self):
        r, _ = self.req("POST", "/avado/api/explain", FINDING, dict(self.ADMIN, Origin="http://evil.example"))
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/avado/api/explain", FINDING, {"Origin": "http://my.ava.do", "Content-Type": "application/json"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("POST", "/avado/api/explain", {"finding": {}}, self.ADMIN)
        self.assertEqual(r.status, 400)
        big = dict(FINDING, question="x" * 20000)
        r, _ = self.req("POST", "/avado/api/explain", big, self.ADMIN)
        self.assertEqual(r.status, 413)

    def test_explain_without_a_model(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        m = manager(d, installed=())
        self.assertEqual(m.explain_check()[0], 409)

    def test_internal_mcp_needs_the_internal_token(self):
        r, _ = self.req("POST", "/internal/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status, 401)

    def test_tools_buttons_are_owner_only(self):
        r, _ = self.req("POST", "/avado/api/tools/connect", {"key": KEY}, {"X-Avado-Request": "1"})
        self.assertEqual(r.status, 403)
        r, data = self.req("POST", "/avado/api/tools/connect", {"key": KEY},
                           {"X-Avado-Request": "1", "Authorization": "Bearer owner", "Content-Type": "application/json"})
        self.assertEqual(r.status, 200)
        self.assertTrue(self.m.tools.connected)
        self.assertEqual(self.m.webui.tool_server["type"], "mcp")
        self.assertTrue(self.m.webui.model_tools)


class FactsRule(unittest.TestCase):
    def test_tools_rule_only_when_connected(self):
        import boxfacts as b

        class P:
            def one(self, q):
                return None

            def query(self, q):
                return None
        f = b.Facts(prometheus=P(), call=lambda *a, **k: [], cpu=lambda: None, mem_total=lambda: None)
        self.assertNotIn("avado_health_check", f.build())
        f.tools = True
        self.assertIn("avado_health_check", f.build())


class LookupTest(unittest.TestCase):
    def test_only_private_addresses_count(self):
        real = doors.socket.gethostbyname_ex
        try:
            doors.socket.gethostbyname_ex = lambda n: (n, [], ["193.77.62.65", "172.33.0.9", "127.0.0.1"])
            self.assertEqual(doors.lookup("homeassistant.my.ava.do"), {"172.33.0.9"})
        finally:
            doors.socket.gethostbyname_ex = real


if __name__ == "__main__":
    unittest.main()
