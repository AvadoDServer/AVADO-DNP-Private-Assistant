"""Tests for the setup helper. Standard library only: `python3 -m unittest discover -s tests`."""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import avado_setup as s  # noqa: E402

GIB = 1024 ** 3
CATALOG = s.load_catalog(os.path.join(os.path.dirname(HERE), "models.json"))
PUBLIC = os.path.join(os.path.dirname(HERE), "public")


class FakeOllama:
    def __init__(self, installed=None, fail=None):
        self.models = list(installed or [])
        self.fail = fail  # error message for the next pull
        self.pulls = []
        self.deleted = []
        self.speed_calls = []
        self.is_up = True

    def up(self):
        return self.is_up

    def installed(self):
        return [{"tag": t, "size": 1000} for t in self.models]

    def delete(self, tag):
        self.deleted.append(tag)
        self.models.remove(tag)

    def pull(self, tag, progress, cancel):
        self.pulls.append(tag)
        if self.fail:
            msg, self.fail = self.fail, None
            raise s.PullError(msg)
        progress.feed({"status": "pulling manifest"})
        progress.feed({"status": "pulling x", "digest": "sha256:a", "total": 100, "completed": 50})
        progress.feed({"status": "pulling x", "digest": "sha256:a", "total": 100, "completed": 100})
        progress.feed({"status": "success"})
        self.models.append(tag)

    def speed_test(self, tag, think=False):
        self.speed_calls.append((tag, think))
        return {"tokens_per_second": 12.0, "load_seconds": 2.0, "at": 0, "model": tag}


class FakeWebUI:
    def __init__(self, admin_token="owner", onboarding=False):
        self.admin_token = admin_token
        self.onboard = onboarding
        self.defaults = []
        self.up = True

    def onboarding(self):
        return self.onboard if self.up else None

    def is_admin(self, token):
        return bool(token) and token == self.admin_token

    def set_default_model(self, token, tag):
        assert token == self.admin_token
        self.defaults.append(tag)
        return True


def tag_of(model_id):
    return s.find_model(CATALOG, model_id)["tag"]


class PlainWords(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(s.plain_size(274302030), "274 MB")
        self.assertEqual(s.plain_size(2497293316), "2.5 GB")
        self.assertEqual(s.plain_size(2e9), "2 GB")
        self.assertEqual(s.plain_size(18556698692), "19 GB")
        self.assertEqual(s.plain_size(None), "unknown")
        self.assertEqual(s.plain_size(3748e9), "3.7 TB")
        self.assertEqual(s.plain_size(2e12), "2 TB")

    def test_memory_is_named_as_sold(self):
        self.assertEqual(s.plain_ram(int(62.2 * GIB)), "64 GB")
        self.assertEqual(s.plain_ram(int(30.5 * GIB)), "32 GB")
        self.assertEqual(s.plain_ram(int(93 * GIB)), "96 GB")
        self.assertEqual(s.plain_ram(int(123 * GIB)), "128 GB")
        self.assertEqual(s.plain_ram(int(13.4 * GIB)), "16 GB")
        self.assertIsNone(s.plain_ram(None))

    def test_durations(self):
        self.assertEqual(s.plain_duration(30), "less than a minute")
        self.assertEqual(s.plain_duration(7 * 60), "about 7 minutes")
        self.assertEqual(s.plain_duration(42 * 60), "about 40 minutes")
        self.assertEqual(s.plain_duration(5 * 3600), "about 5 hours")
        self.assertEqual(s.plain_duration(4 * 86400), "about 4 days")

    def test_speeds(self):
        self.assertEqual(s.plain_speed(60e3), "60 kB per second")
        self.assertEqual(s.plain_speed(4.2e6), "4.2 MB per second")
        self.assertIsNone(s.plain_speed(0))

    def test_pull_errors(self):
        self.assertEqual(s.plain_pull_error("write: no space left on device"),
                         ("The box ran out of free space during the download.", False))
        self.assertTrue(s.plain_pull_error("dial tcp: lookup registry.ollama.ai: i/o timeout")[1])
        self.assertFalse(s.plain_pull_error("pull model manifest: file does not exist")[1])

    def test_speed_sentence(self):
        text = s.plain_speed_result({"tokens_per_second": 12.0, "load_seconds": 9})
        self.assertIn("9 words per second", text)
        self.assertIn("faster than most people read", text)
        self.assertIn("9 seconds", text)


class Choosing(unittest.TestCase):
    """Box sizes as Linux reports them, with plenty of disk."""

    def pick(self, ram_gib, free=3e12):
        m = s.recommend(CATALOG, ram_gib * GIB, free)
        return m and m["id"]

    def test_by_memory(self):
        self.assertEqual(self.pick(7.5), "tiny")     # 8 GB box
        self.assertEqual(self.pick(15.5), "tiny")    # 16 GB box
        self.assertEqual(self.pick(30.5), "small")   # 32 GB box
        self.assertEqual(self.pick(61.7), "medium")  # 64 GB box (the test box)
        self.assertEqual(self.pick(92.5), "big")     # 96 GB box
        self.assertEqual(self.pick(123), "large")    # 128 GB box

    def test_disk_limits_the_choice(self):
        # 64 GB box with 60 GB free: the medium model (19 GB + 50 GB kept free) does not fit, small does
        self.assertEqual(self.pick(61.7, free=60e9), "small")
        self.assertIsNone(self.pick(61.7, free=10e9))

    def test_fit_reasons(self):
        ok, why = s.check_fit(s.find_model(CATALOG, "large"), 30 * GIB, 3e12, CATALOG["reserve_bytes"])
        self.assertFalse(ok)
        self.assertEqual(why, "Needs a box with more memory.")
        ok, why = s.check_fit(s.find_model(CATALOG, "small"), 30 * GIB, 10e9, CATALOG["reserve_bytes"])
        self.assertFalse(ok)
        self.assertIn("free space", why)

    def test_catalog_is_sane(self):
        for m in CATALOG["models"]:
            self.assertIn(":", m["tag"])
            self.assertGreater(m["download_bytes"], 1e8)
            self.assertTrue(m["about"])
            if m["recommend_from_gib"] is not None:
                self.assertGreaterEqual(m["recommend_from_gib"], m["min_ram_gib"])

    def test_same_tag(self):
        self.assertTrue(s.same_tag("qwen3", "qwen3:latest"))
        self.assertFalse(s.same_tag("qwen3:4b", "qwen3:1.7b"))
        self.assertFalse(s.same_tag(None, "x"))


class ProgressMath(unittest.TestCase):
    def test_progress(self):
        t = [0.0]
        p = s.Progress(expected_total=1000, clock=lambda: t[0])
        p.feed({"status": "pulling manifest"})
        for i in range(1, 11):
            t[0] = i * 1.0
            p.feed({"digest": "a", "total": 1000, "completed": i * 50})
        sm = p.summary()
        self.assertEqual(sm["stage"], "downloading")
        self.assertEqual(sm["done"], 500)
        self.assertEqual(sm["percent"], 50)
        self.assertEqual(sm["speed_plain"], "1 kB per second")
        self.assertEqual(sm["eta_plain"], "less than a minute")
        p.feed({"status": "verifying sha256 digest"})
        self.assertEqual(p.summary()["stage"], "verifying")


class ManagerFlow(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.t = [0.0]

    def mk(self, ram_gib=61.7, free=3e12, installed=None, auto=True, fail=None, onboarding=False):
        self.ollama = FakeOllama(installed, fail)
        self.webui = FakeWebUI(onboarding=onboarding)
        return s.Manager(CATALOG, self.ollama, self.webui, state_dir=self.dir, auto_download=auto,
                         ram_bytes=ram_gib * GIB, free_bytes_fn=lambda: free, retry_seconds=1,
                         clock=lambda: self.t[0], warmup_seconds=0)

    def path(self, name):
        return os.path.join(self.dir, name)

    def run_steps(self, m, n=6):
        for _ in range(n):
            m.step()

    def test_first_start_picks_before_ollama_and_seeds_default(self):
        m = self.mk()
        self.assertEqual(m.state["target"], tag_of("medium"))
        with open(self.path("default-model")) as f:
            self.assertEqual(f.read().strip(), tag_of("medium"))
        self.assertTrue(os.path.exists(self.path("state.json")))
        self.assertFalse(os.path.exists(self.path("ready")))

    def test_download_then_ready_then_speed(self):
        m = self.mk()
        m.step()  # downloads the chat model
        self.assertEqual(m.state["active"], tag_of("medium"))
        self.assertEqual(m.state["phase"], "ready")
        self.assertTrue(os.path.exists(self.path("ready")))
        m.step()  # speed test
        self.assertEqual(self.ollama.speed_calls, [(tag_of("medium"), False)])
        self.assertEqual(self.ollama.pulls, [tag_of("medium")])
        st = m.status()
        self.assertTrue(st["ready"])
        self.assertIn("words per second", st["speed"])
        self.assertEqual(m.step(), 30)  # nothing left to do

    def test_speed_test_waits_until_the_box_has_settled(self):
        self.ollama = FakeOllama([tag_of("small")])
        self.webui = FakeWebUI()
        self.webui.up = False
        m = s.Manager(CATALOG, self.ollama, self.webui, state_dir=self.dir, auto_download=False,
                      ram_bytes=30 * GIB, free_bytes_fn=lambda: 3e12, clock=lambda: self.t[0], warmup_seconds=90)
        self.assertEqual(m.step(), 10)
        self.t[0] = 100
        self.assertEqual(m.step(), 10)  # Open WebUI still starting
        self.webui.up = True
        m.step()
        self.assertEqual(len(self.ollama.speed_calls), 1)

    def test_thinking_models_get_a_level_in_the_speed_test(self):
        m = self.mk(installed=[tag_of("thinker")], auto=False)
        m.step()
        m.step()
        self.assertEqual(self.ollama.speed_calls[0], (tag_of("thinker"), "low"))

    def test_restart_keeps_state_and_does_not_pick_again(self):
        m = self.mk()
        self.run_steps(m, 3)
        m2 = s.Manager(CATALOG, self.ollama, self.webui, state_dir=self.dir, ram_bytes=61.7 * GIB,
                       free_bytes_fn=lambda: 3e12, clock=lambda: self.t[0], warmup_seconds=0)
        self.assertIsNone(m2.state["target"])
        self.assertEqual(m2.state["active"], tag_of("medium"))

    def test_no_space(self):
        m = self.mk(free=5e9)
        self.assertEqual(m.state["phase"], "no-space")
        m.step()
        self.assertEqual(m.status()["phase"], "no-space")
        self.assertEqual(self.ollama.pulls, [])

    def test_network_error_retries_by_itself(self):
        m = self.mk(fail="dial tcp: i/o timeout")
        self.assertEqual(m.step(), 1)
        self.assertEqual(m.state["phase"], "retrying")
        self.assertEqual(m.state["problem"], "The box lost its connection to the model library.")
        m.step()
        self.assertEqual(m.state["phase"], "ready")

    def test_missing_model_stops_and_offers_try_again(self):
        m = self.mk(fail="pull model manifest: file does not exist")
        m.step()
        st = m.status()
        self.assertEqual(st["phase"], "error")
        self.assertEqual(st["failed"], "medium")
        self.assertIsNone(m.state["target"])

    def test_owner_actions(self):
        m = self.mk(installed=[tag_of("small")], auto=False)
        m.step()
        self.assertEqual(m.state["active"], tag_of("small"))
        self.assertEqual(m.remove("small"), "This is the model the assistant uses. Choose another one first.")
        self.assertEqual(m.choose("large"), "Needs a box with more memory.")
        self.assertEqual(m.choose("nope"), "That model is not on the list.")
        self.assertIsNone(m.choose("tiny"))
        m.step()
        self.assertEqual(m.state["active"], tag_of("tiny"))
        self.assertIsNone(m.remove("small"))
        self.assertEqual(self.ollama.deleted, [tag_of("small")])
        self.assertEqual(m.remove("small"), "That model is not on the box.")

    def test_stop_download(self):
        m = self.mk()
        self.assertIsNone(m.stop_download())
        self.assertIsNone(m.state["target"])
        self.assertEqual(m.stop_download(), "Nothing is downloading.")
        m.step()
        self.assertEqual(m.state["phase"], "choose")
        self.assertEqual(self.ollama.pulls, [])

    def test_model_added_elsewhere_is_used(self):
        m = self.mk(installed=["llama3.2:3b"], auto=False)
        m.step()
        st = m.status()
        self.assertEqual(st["active"], "llama3.2:3b")
        self.assertEqual([x["label"] for x in st["models"] if x["id"] == "llama3.2:3b"], ["Added by you"])

    def test_sync_with_open_webui_only_for_ready_model_and_rate_limited(self):
        m = self.mk()
        m.sync_webui("owner")
        self.assertEqual(self.webui.defaults, [])  # nothing downloaded yet
        self.run_steps(m, 3)
        m.sync_webui("owner")
        self.assertEqual(self.webui.defaults, [tag_of("medium")])
        m.sync_webui("owner")
        self.assertEqual(len(self.webui.defaults), 1)  # within a minute
        self.t[0] += 61
        m.sync_webui("owner")
        self.assertEqual(len(self.webui.defaults), 2)

    def test_ready_flag_survives_ollama_being_down(self):
        m = self.mk()
        m.step()
        self.assertTrue(os.path.exists(self.path("ready")))
        self.ollama.is_up = False
        m.step()
        self.assertTrue(os.path.exists(self.path("ready")))


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.ollama = FakeOllama([tag_of("small")])
        cls.webui = FakeWebUI()
        cls.m = s.Manager(CATALOG, cls.ollama, cls.webui, state_dir=cls.dir, auto_download=False,
                          ram_bytes=30 * GIB, free_bytes_fn=lambda: 3e12, warmup_seconds=0)
        cls.m.step()
        cls.server = s.ThreadingHTTPServer(("127.0.0.1", 0), s.make_handler(cls.m, PUBLIC))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.dir)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def test_status_is_open_but_read_only(self):
        r, data = self.req("GET", "/avado/api/status")
        self.assertEqual(r.status, 200)
        st = json.loads(data)
        self.assertFalse(st["can_change"])
        self.assertEqual(st["active"], "Qwen3 4B")
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertIn("frame-ancestors 'self' http://my.ava.do", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("Cache-Control"), "no-store")

    def test_owner_cookie_is_recognised(self):
        r, data = self.req("GET", "/avado/api/status", headers={"Cookie": "a=b; token=owner"})
        self.assertTrue(json.loads(data)["can_change"])

    def test_buttons_need_the_page_header(self):
        r, _ = self.req("POST", "/avado/api/choose", {"id": "tiny"}, {"Authorization": "Bearer owner"})
        self.assertEqual(r.status, 403)

    def test_buttons_need_the_owner(self):
        r, data = self.req("POST", "/avado/api/choose", {"id": "tiny"},
                           {"X-Avado-Request": "1", "Authorization": "Bearer someone"})
        self.assertEqual(r.status, 403)
        self.assertIn("owner", json.loads(data)["error"])

    def test_owner_can_press_buttons(self):
        r, data = self.req("POST", "/avado/api/choose", {"id": "nope"},
                           {"X-Avado-Request": "1", "Authorization": "Bearer owner", "Content-Type": "application/json"})
        self.assertEqual(r.status, 409)
        self.assertEqual(json.loads(data)["error"], "That model is not on the list.")
        r, data = self.req("POST", "/avado/api/unknown", {}, {"X-Avado-Request": "1", "Authorization": "Bearer owner"})
        self.assertEqual(r.status, 404)

    def test_no_buttons_before_the_first_account(self):
        self.webui.onboard = True
        try:
            r, data = self.req("POST", "/avado/api/cancel", {}, {"X-Avado-Request": "1", "Authorization": "Bearer owner"})
            self.assertEqual(r.status, 403)
            self.assertIn("Create your account", json.loads(data)["error"])
        finally:
            self.webui.onboard = False

    def test_static_files_only_from_the_public_folder(self):
        r, data = self.req("GET", "/avado/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"Private Assistant", data)
        for bad in ("/avado/../avado_setup.py", "/avado/%2e%2e/models.json", "/avado/.hidden.css",
                    "/avado/sub/x.css", "/avado/models.json", "/etc/passwd"):
            r, _ = self.req("GET", bad)
            self.assertEqual(r.status, 404, bad)
        r, _ = self.req("GET", "/avado")
        self.assertEqual(r.status, 302)

    def test_page_assets_exist(self):
        for name in ("index.html", "app.js", "style.css", "base.css", "theme.css", "favicon.png"):
            self.assertTrue(os.path.isfile(os.path.join(PUBLIC, name)), name)


class RequestToken(unittest.TestCase):
    def test_token_sources(self):
        self.assertEqual(s.request_token({"Authorization": "Bearer abc"}), "abc")
        self.assertEqual(s.request_token({"Cookie": "x=1; token=zz"}), "zz")
        self.assertIsNone(s.request_token({}))


class OllamaClient(unittest.TestCase):
    """The real client against a stub that answers like Ollama."""

    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler

        cls.generate_calls = []

        class Stub(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._json({"models": [{"name": "qwen3:1.7b", "size": 5}]})
                else:
                    self._json({"version": "0.40.13"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/generate":
                    cls.generate_calls.append(body)
                    n = len(cls.generate_calls)
                    # first run slower (and it loads the model), second faster
                    self._json({"eval_count": 96, "eval_duration": int((8e9 if n % 2 else 4e9)),
                                "load_duration": int(6e9 if n % 2 else 1e8)})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                lines = [{"status": "pulling manifest"},
                         {"status": "pulling a", "digest": "sha256:a", "total": 10, "completed": 10}]
                if body["model"] == "broken:1":
                    lines.append({"error": "pull model manifest: file does not exist"})
                else:
                    lines += [{"status": "verifying sha256 digest"}, {"status": "success"}]
                for line in lines:
                    self.wfile.write((json.dumps(line) + "\n").encode())

        cls.server = s.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.client = s.Ollama("http://127.0.0.1:%d" % cls.server.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_up_and_installed(self):
        self.assertTrue(self.client.up())
        self.assertEqual(self.client.installed(), [{"tag": "qwen3:1.7b", "size": 5}])
        self.assertFalse(s.Ollama("http://127.0.0.1:9").up())

    def test_pull_success(self):
        p = s.Progress(10)
        self.client.pull("qwen3:1.7b", p, threading.Event())
        self.assertEqual(p.summary()["stage"], "done")
        self.assertEqual(p.summary()["percent"], 100)

    def test_pull_error_event(self):
        with self.assertRaises(s.PullError) as e:
            self.client.pull("broken:1", s.Progress(10), threading.Event())
        self.assertIn("file does not exist", str(e.exception))

    def test_pull_cancelled(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(s.Cancelled):
            self.client.pull("qwen3:1.7b", s.Progress(10), ev)

    def test_speed_test_keeps_the_better_run(self):
        type(self).generate_calls = []
        r = self.client.speed_test("gpt-oss:20b", think="low")
        self.assertEqual(r["tokens_per_second"], 24.0)  # 96 tokens in 4 s, not 8 s
        self.assertEqual(r["load_seconds"], 6.0)        # the first run's load
        self.assertEqual(len(self.generate_calls), 2)
        self.assertEqual(self.generate_calls[0]["think"], "low")
        self.assertEqual(self.generate_calls[0]["options"], {"num_predict": 96})


if __name__ == "__main__":
    unittest.main()
