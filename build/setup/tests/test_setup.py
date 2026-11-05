"""Tests for the setup helper. Standard library only: `python3 -m unittest discover -s tests`."""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import avado_setup as s  # noqa: E402
import boxfacts as b  # noqa: E402

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
        self.in_memory = []
        self.unloaded = []
        self.is_up = True

    def up(self):
        return self.is_up

    def installed(self):
        return [{"tag": t, "size": 1000} for t in self.models]

    def loaded(self):
        return list(self.in_memory)

    def unload(self, tag):
        self.unloaded.append(tag)
        self.in_memory.remove(tag)

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

    def speed_test(self, tag, threads=None):
        self.speed_calls.append((tag, threads))
        return {"tokens_per_second": 12.0, "load_seconds": 2.0, "at": 0, "model": tag}


class FakeWebUI:
    def __init__(self, admin_token="owner", onboarding=False):
        self.admin_token = admin_token
        self.onboard = onboarding
        self.defaults = []
        self.named = []
        self.filters = 0
        self.up = True

    def onboarding(self):
        return self.onboard if self.up else None

    def is_admin(self, token):
        return bool(token) and token == self.admin_token

    def set_defaults(self, token, tag, threads):
        assert token == self.admin_token
        self.defaults.append((tag, threads))
        return True

    def name_model(self, token, model, threads):
        self.named.append(model["name"])
        return True

    def install_filter(self, token):
        self.filters += 1
        return ["created"]

    def set_tool_server(self, token, connection):
        self.tool_server = connection
        return True

    def set_model_tools(self, token, tag, on):
        self.model_tools = getattr(self, "model_tools", {})
        self.model_tools[tag] = on
        return True


class FakeMemory:
    """free memory now / lowest in 7 days, in GiB"""

    def __init__(self, now=60.0, low=40.0):
        self.now, self.low = now, low

    def free_now(self):
        return self.now

    def lowest_7d(self, refresh=False):
        return self.low

    def reading(self):
        both = [v for v in (self.now, self.low) if v is not None]
        return {"now": self.now, "lowest_7d": self.low, "free": min(both) if both else None,
                "source": "prometheus" if self.low is not None else "meminfo"}


class FakeFacts:
    def __init__(self):
        self.text = None
        self.text_at = None
        self.builds = 0

    def context(self, mem):
        self.text = self.build(mem)
        self.text_at = 0
        return self.text

    def build(self, mem):
        self.builds += 1
        return "facts %d" % self.builds


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
    """budget = min(free now, lowest in 7 days) - 4 - 0.5 - 1 GiB; plenty of disk."""

    def pick(self, free_gib, disk=3e12):
        m = s.recommend(CATALOG, free_gib, disk)
        return m and m["id"]

    def test_by_free_memory(self):
        self.assertIsNone(self.pick(7.4))              # below 7.5 GiB: nothing
        self.assertEqual(self.pick(7.5), "tiny")       # budget 2.0
        self.assertEqual(self.pick(9.6), "tiny")       # budget 4.1 < 4.2
        self.assertEqual(self.pick(9.8), "small")      # budget 4.3
        self.assertEqual(self.pick(12.1), "small")     # budget 6.6 < 6.7
        self.assertEqual(self.pick(12.3), "medium")    # budget 6.8
        self.assertEqual(self.pick(39.0), "medium")    # the test box at its busiest (41.8 GB)

    def test_on_request_models_are_never_suggested(self):
        for m in CATALOG["models"]:
            if not m["auto"]:
                self.assertNotEqual(self.pick(200), m["id"])

    def test_budget_and_need(self):
        self.assertAlmostEqual(s.budget_gib(CATALOG, 39.0), 33.5)
        self.assertIsNone(s.budget_gib(CATALOG, None))
        self.assertEqual(s.plain_gib(s.uses_gib(CATALOG, s.find_model(CATALOG, "medium"))), "8 GB")

    def test_unknown_memory_still_suggests_by_disk(self):
        self.assertEqual(self.pick(None), "medium")

    def test_disk_limits_the_choice(self):
        # 55 GB free: the medium model (6.6 GB + 50 GB kept free) does not fit, small (3.3 GB) does
        self.assertEqual(self.pick(39.0, disk=55e9), "small")
        self.assertIsNone(self.pick(39.0, disk=10e9))

    def test_fit_reasons(self):
        ok, why = s.check_fit(CATALOG, s.find_model(CATALOG, "large"), 15.0, 3e12)
        self.assertFalse(ok)
        self.assertEqual(why, "Needs about 24 GB of free memory next to your node.")
        ok, why = s.check_fit(CATALOG, s.find_model(CATALOG, "large"), 23.0, 3e12)
        self.assertTrue(ok)
        ok, why = s.check_fit(CATALOG, s.find_model(CATALOG, "small"), 30.0, 10e9)
        self.assertFalse(ok)
        self.assertIn("free space", why)

    def test_sizing_sentence(self):
        m = s.find_model(CATALOG, "medium")
        self.assertEqual(s.sizing_sentence(CATALOG, {"free": 39.0, "lowest_7d": 39.0}, m),
                         "Even at its busiest, your AVADO has about 42 GB free. The assistant will use about 8 GB.")
        self.assertEqual(s.sizing_sentence(CATALOG, {"free": 13.0, "lowest_7d": None}, s.find_model(CATALOG, "small")),
                         "Your AVADO has about 14 GB free right now. The assistant will use about 5 GB.")
        self.assertIn("does not download anything", s.sizing_sentence(CATALOG, {"free": 6.0, "lowest_7d": 6.0}, None))
        self.assertIsNone(s.sizing_sentence(CATALOG, {"free": None}, m))

    def test_catalog_is_sane(self):
        tags = [m["tag"] for m in CATALOG["models"]]
        for m in CATALOG["models"]:
            self.assertIn(":", m["tag"])
            self.assertGreater(m["download_bytes"], 1e8)
            self.assertGreater(m["ram_need_gib"], 0)
            self.assertTrue(m["about"])
            self.assertLessEqual(len(m["about"]), 140)
            self.assertNotIn(":", m["name"])  # friendly names, never the raw tag
        self.assertEqual(sorted(tags), sorted(["lfm3:1.7b", "qwen3.5:4b", "gemma4:e4b-it-qat", "qwen3.5:9b",
                                               "gemma4:26b-a4b-it-qat"]))
        for old in ("qwen3:", "gpt-oss", "qwen3-next"):
            self.assertFalse(any(t.startswith(old) for t in tags), old)

    def test_same_tag(self):
        self.assertTrue(s.same_tag("qwen3", "qwen3:latest"))
        self.assertFalse(s.same_tag("qwen3.5:4b", "qwen3.5:9b"))
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

    def test_resumed_download_counts_only_new_bytes_in_the_speed(self):
        # Seen on the test box: 551 MB were already there from a stopped download;
        # the page said "104 MB per second, less than a minute" at about 7 MB per second.
        t = [0.0]
        p = s.Progress(expected_total=2_500_000_000, clock=lambda: t[0])
        p.feed({"status": "pulling manifest"})
        t[0] = 1.0
        p.feed({"digest": "big", "total": 2_500_000_000, "completed": 551_000_000})
        for i in range(1, 7):
            t[0] = 1.0 + i
            p.feed({"digest": "big", "total": 2_500_000_000, "completed": 551_000_000 + i * 7_000_000})
        sm = p.summary()
        self.assertEqual(sm["done_plain"], "593 MB")
        self.assertEqual(sm["speed_plain"], "7.0 MB per second")
        self.assertEqual(sm["eta_plain"], "about 5 minutes")


class ManagerFlow(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.t = [0.0]

    def mk(self, now=60.0, low=40.0, disk=3e12, installed=None, fail=None, onboarding=False):
        self.ollama = FakeOllama(installed, fail)
        self.webui = FakeWebUI(onboarding=onboarding)
        self.mem = FakeMemory(now, low)
        self.facts = FakeFacts()
        return s.Manager(CATALOG, self.ollama, self.webui, state_dir=self.dir, memory=self.mem, facts=self.facts,
                         free_bytes_fn=lambda: disk, retry_seconds=1, clock=lambda: self.t[0], warmup_seconds=0,
                         threads=8)

    def path(self, name):
        return os.path.join(self.dir, name)

    def run_steps(self, m, n=6):
        for _ in range(n):
            m.step()

    def test_nothing_is_downloaded_until_the_owner_presses_the_button(self):
        m = self.mk()
        self.run_steps(m, 3)
        self.assertEqual(self.ollama.pulls, [])
        st = m.status()
        self.assertEqual(st["phase"], "choose")
        self.assertEqual(st["suggestion"], {"id": "medium", "name": "Qwen3.5 9B", "size_plain": "6.6 GB",
                                            "uses_plain": "8 GB"})
        self.assertEqual(st["memory_sentence"],
                         "Even at its busiest, your AVADO has about 43 GB free. The assistant will use about 8 GB.")
        self.assertFalse(os.path.exists(self.path("ready")))

    def test_button_then_download_then_ready_then_speed(self):
        m = self.mk()
        m.step()
        self.assertIsNone(m.choose("medium"))
        m.step()  # downloads
        self.assertEqual(m.state["active"], tag_of("medium"))
        self.assertEqual(m.state["phase"], "ready")
        self.assertTrue(os.path.exists(self.path("ready")))
        with open(self.path("default-model")) as f:
            self.assertEqual(f.read().strip(), tag_of("medium"))
        m.step()  # speed test, thinking off, half the threads
        self.assertEqual(self.ollama.speed_calls, [(tag_of("medium"), 8)])
        self.assertEqual(self.ollama.pulls, [tag_of("medium")])
        st = m.status()
        self.assertTrue(st["ready"])
        self.assertIn("words per second", st["speed"])
        self.assertEqual(m.step(), 30)  # nothing left to do

    def test_too_little_memory_downloads_nothing_and_says_so(self):
        m = self.mk(now=7.0, low=7.0)
        m.step()
        st = m.status()
        self.assertEqual(st["phase"], "no-memory")
        self.assertIsNone(st["suggestion"])
        self.assertIn("does not download anything", st["memory_sentence"])
        self.assertEqual(m.choose("tiny"), "Needs about 8 GB of free memory next to your node.")
        self.assertEqual(self.ollama.pulls, [])

    def test_too_little_disk_says_space_not_memory(self):
        m = self.mk(disk=20e9)
        m.step()
        st = m.status()
        self.assertEqual(st["phase"], "no-space")
        self.assertIn("will use about 8 GB", st["memory_sentence"])

    def test_the_lowest_point_of_the_week_decides(self):
        m = self.mk(now=60.0, low=11.0)
        self.assertEqual(m.status()["suggestion"]["id"], "small")

    def test_speed_test_waits_until_the_box_has_settled(self):
        self.ollama = FakeOllama([tag_of("small")])
        self.webui = FakeWebUI()
        self.webui.up = False
        m = s.Manager(CATALOG, self.ollama, self.webui, state_dir=self.dir, memory=FakeMemory(), facts=FakeFacts(),
                      free_bytes_fn=lambda: 3e12, clock=lambda: self.t[0], warmup_seconds=90)
        self.assertEqual(m.step(), 10)
        self.t[0] = 100
        self.assertEqual(m.step(), 10)  # Open WebUI still starting
        self.webui.up = True
        m.step()
        self.assertEqual(len(self.ollama.speed_calls), 1)

    def test_low_memory_pauses_and_unloads_then_resumes(self):
        m = self.mk(installed=[tag_of("medium")])
        m.step()
        self.ollama.in_memory = [tag_of("medium")]
        self.assertFalse(m.guard(tag_of("medium")))
        self.mem.now = 12.0  # needs 6.7 + 5.5 = 12.2
        self.assertTrue(m.guard(tag_of("medium")))
        self.assertEqual(self.ollama.unloaded, [tag_of("medium")])
        st = m.status()
        self.assertTrue(st["paused"])
        self.assertEqual(m.request_speed_test(), s.PAUSED_MESSAGE)
        self.mem.now = 12.5  # not yet: 1 GiB above the line
        self.assertTrue(m.guard())
        self.mem.now = 13.3
        self.assertFalse(m.guard())
        self.assertFalse(m.status()["paused"])

    def test_the_worker_checks_memory_every_minute(self):
        m = self.mk(installed=[tag_of("small")])
        m.step()
        self.ollama.in_memory = [tag_of("small")]
        self.mem.now = 5.0
        m.step()
        self.assertEqual(self.ollama.unloaded, [])  # checked less than a minute ago
        self.t[0] += 61
        m.step()
        self.assertEqual(self.ollama.unloaded, [tag_of("small")])
        self.assertEqual(self.ollama.speed_calls, [(tag_of("small"), 8)])  # before the drop; none while paused

    def test_restart_keeps_state(self):
        m = self.mk()
        m.choose("medium")
        self.run_steps(m, 3)
        m2 = s.Manager(CATALOG, self.ollama, self.webui, state_dir=self.dir, memory=FakeMemory(), facts=FakeFacts(),
                       free_bytes_fn=lambda: 3e12, clock=lambda: self.t[0], warmup_seconds=0)
        self.assertIsNone(m2.state["target"])
        self.assertEqual(m2.state["active"], tag_of("medium"))
        self.assertEqual(m2.internal_token, m.internal_token)
        self.assertEqual(os.stat(self.path("internal-token")).st_mode & 0o777, 0o600)

    def test_network_error_retries_by_itself(self):
        m = self.mk(fail="dial tcp: i/o timeout")
        m.choose("medium")
        self.assertEqual(m.step(), 1)
        self.assertEqual(m.state["phase"], "retrying")
        self.assertEqual(m.state["problem"], "The box lost its connection to the model library.")
        m.step()
        self.assertEqual(m.state["phase"], "ready")

    def test_missing_model_stops_and_offers_try_again(self):
        m = self.mk(fail="pull model manifest: file does not exist")
        m.choose("medium")
        m.step()
        st = m.status()
        self.assertEqual(st["phase"], "error")
        self.assertEqual(st["failed"], "medium")
        self.assertIsNone(m.state["target"])

    def test_owner_actions(self):
        m = self.mk(now=20, low=20, installed=[tag_of("small")])
        m.step()
        self.assertEqual(m.state["active"], tag_of("small"))
        self.assertEqual(m.remove("small"), "This is the model the assistant uses. Choose another one first.")
        self.assertEqual(m.choose("large"), "Needs about 24 GB of free memory next to your node.")
        self.assertEqual(m.choose("nope"), "That model is not on the list.")
        self.assertIsNone(m.choose("tiny"))
        m.step()
        self.assertEqual(m.state["active"], tag_of("tiny"))
        self.assertIsNone(m.remove("small"))
        self.assertEqual(self.ollama.deleted, [tag_of("small")])
        self.assertEqual(m.remove("small"), "That model is not on the box.")

    def test_stop_download(self):
        m = self.mk()
        m.choose("medium")
        self.assertIsNone(m.stop_download())
        self.assertIsNone(m.state["target"])
        self.assertEqual(m.stop_download(), "Nothing is downloading.")
        m.step()
        self.assertEqual(m.state["phase"], "choose")
        self.assertEqual(self.ollama.pulls, [])

    def test_side_loaded_model_is_used(self):
        m = self.mk(installed=[tag_of("gemma-small")])
        m.step()
        st = m.status()
        self.assertEqual(st["active"], "Gemma 4 E4B")
        self.assertTrue(st["ready"])
        self.assertIn("about 8 GB", st["memory_sentence"])

    def test_model_added_elsewhere_is_used(self):
        m = self.mk(installed=["llama3.2:3b"])
        m.step()
        st = m.status()
        self.assertEqual(st["active"], "llama3.2:3b")
        self.assertEqual([x["label"] for x in st["models"] if x["id"] == "llama3.2:3b"], ["Added by you"])
        self.assertIsNone(st["memory_sentence"])

    def test_sync_with_open_webui(self):
        m = self.mk(installed=[tag_of("small"), tag_of("medium")])
        m.sync_webui("owner")
        self.assertEqual(self.webui.defaults, [])  # Ollama not read yet
        m.step()
        m.sync_webui("owner")
        self.assertEqual(self.webui.defaults, [(tag_of("medium"), 8)])
        self.assertEqual(sorted(self.webui.named), ["Qwen3.5 4B", "Qwen3.5 9B"])
        self.assertEqual(self.webui.filters, 1)
        m.sync_webui("owner")
        self.assertEqual(len(self.webui.defaults), 1)  # within a minute
        self.t[0] += 61
        m.sync_webui("owner")
        self.assertEqual(len(self.webui.defaults), 2)
        self.assertEqual(len(self.webui.named), 2)  # each name only once

    def test_context_is_cached_and_rebuilt_in_the_background(self):
        m = self.mk()
        self.assertEqual(m.context(), "facts 1")
        self.assertEqual(m.context(), "facts 1")
        self.t[0] += 61
        self.assertEqual(m.context(), "facts 1")  # the old text while the new one is built
        for _ in range(50):
            if not m.context_building:
                break
            time.sleep(0.01)
        self.assertEqual(self.facts.text, "facts 2")

    def test_ready_flag_survives_ollama_being_down(self):
        m = self.mk(installed=[tag_of("small")])
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
        cls.m = s.Manager(CATALOG, cls.ollama, cls.webui, state_dir=cls.dir, memory=FakeMemory(20, 20),
                          facts=FakeFacts(), free_bytes_fn=lambda: 3e12, warmup_seconds=0)
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
        self.assertEqual(st["active"], "Qwen3.5 4B")
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

    def test_box_facts_only_with_the_internal_token(self):
        r, _ = self.req("GET", "/internal/context?model=x")
        self.assertEqual(r.status, 404)
        r, _ = self.req("GET", "/internal/context", headers={"X-Avado-Internal": "wrong"})
        self.assertEqual(r.status, 404)
        r, data = self.req("GET", "/internal/context?model=qwen3.5:4b", headers={"X-Avado-Internal": self.m.internal_token})
        self.assertEqual(r.status, 200)
        body = json.loads(data)
        self.assertFalse(body["paused"])
        self.assertTrue(body["system"].startswith("facts"))
        r, _ = self.req("GET", "/avado/../internal/context", headers={"X-Avado-Internal": "wrong"})
        self.assertEqual(r.status, 404)

    def test_paused_answer(self):
        self.m.memory.now = 5.0
        try:
            r, data = self.req("GET", "/internal/context", headers={"X-Avado-Internal": self.m.internal_token})
            body = json.loads(data)
            self.assertTrue(body["paused"])
            self.assertIn("Paused so your Ethereum node has enough memory", body["message"])
            self.assertNotIn("system", body)
        finally:
            self.m.memory.now = 20.0
            self.m.guard()

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
                elif self.path == "/api/ps":
                    self._json({"models": [{"name": "qwen3.5:4b", "size": 5}]})
                else:
                    self._json({"version": "0.40.13"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/generate" and body.get("keep_alive") == 0:
                    cls.generate_calls.append(body)
                    self._json({"done": True, "done_reason": "unload"})
                    return
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
        r = self.client.speed_test("qwen3.5:9b", threads=8)
        self.assertEqual(r["tokens_per_second"], 24.0)  # 96 tokens in 4 s, not 8 s
        self.assertEqual(r["load_seconds"], 6.0)        # the first run's load
        self.assertEqual(len(self.generate_calls), 2)
        self.assertIs(self.generate_calls[0]["think"], False)
        self.assertEqual(self.generate_calls[0]["options"], {"num_predict": 96, "num_thread": 8})

    def test_loaded_and_unload(self):
        type(self).generate_calls = []
        self.assertEqual(self.client.loaded(), ["qwen3.5:4b"])
        self.client.unload("qwen3.5:4b")
        self.assertEqual(self.generate_calls, [{"model": "qwen3.5:4b", "keep_alive": 0}])


class WebUISettingsTest(unittest.TestCase):
    """The admin API calls, against a stub of Open WebUI."""

    def setUp(self):
        from http.server import BaseHTTPRequestHandler

        self.posts = []
        self.cfg = {"DEFAULT_MODELS": "", "DEFAULT_PINNED_MODELS": None, "MODEL_ORDER_LIST": [],
                    "DEFAULT_MODEL_METADATA": {}, "DEFAULT_MODEL_PARAMS": {}}
        self.models = {}
        self.function = None
        test = self

        class Stub(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/api/v1/models/model?"):
                    mid = s.urllib.parse.parse_qs(self.path.split("?", 1)[1])["id"][0]
                    self._json(test.models.get(mid))
                elif self.path == "/api/v1/functions/id/avado_context":
                    if test.function is None:
                        self._json({"detail": "not found"}, 404)
                    else:
                        self._json(test.function)
                else:
                    self._json(test.cfg)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                test.posts.append((self.path, self.headers.get("Authorization"), body))
                if self.path == "/api/v1/models/create":
                    test.models[body["id"]] = body
                elif self.path == "/api/v1/functions/create":
                    test.function = dict(body, is_active=False, is_global=False)
                    body = test.function
                elif self.path.endswith("/toggle"):
                    test.function["is_active"] = not test.function["is_active"]
                elif self.path.endswith("/toggle/global"):
                    test.function["is_global"] = not test.function["is_global"]
                elif self.path.endswith("/update"):
                    test.function.update(body)
                    body = test.function
                else:
                    test.cfg = body
                self._json(body)

        self.server = s.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.webui = s.WebUI("http://127.0.0.1:%d" % self.server.server_address[1],
                             filter_path=os.path.join(os.path.dirname(HERE), "avado_context_filter.py"))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_sets_model_thinking_threads_and_turns_builtin_tools_off(self):
        self.assertTrue(self.webui.set_defaults("tok", "qwen3.5:9b", 8))
        path, auth, body = self.posts[-1]
        self.assertEqual(auth, "Bearer tok")
        self.assertEqual(body["DEFAULT_MODELS"], "qwen3.5:9b")
        self.assertEqual(body["DEFAULT_MODEL_METADATA"], {"capabilities": {"builtin_tools": False}})
        self.assertEqual(body["DEFAULT_MODEL_PARAMS"], {"think": False, "num_thread": 8})
        self.assertEqual(body["MODEL_ORDER_LIST"], [])
        # nothing left to change: no second write
        self.assertFalse(self.webui.set_defaults("tok", "qwen3.5:9b", 8))
        self.assertEqual(len(self.posts), 1)

    def test_keeps_the_owners_own_choice_and_other_settings(self):
        self.cfg["DEFAULT_MODELS"] = "qwen3.5:9b"
        self.cfg["DEFAULT_MODEL_METADATA"] = {"capabilities": {"builtin_tools": True, "vision": False}, "x": 1}
        self.cfg["DEFAULT_MODEL_PARAMS"] = {"think": True, "num_thread": 4, "temperature": 0.5}
        self.assertFalse(self.webui.set_defaults("tok", "qwen3.5:9b", 8))
        self.assertEqual(self.posts, [])
        self.cfg["DEFAULT_MODEL_METADATA"] = {"capabilities": {"vision": False}, "x": 1}
        self.assertTrue(self.webui.set_defaults("tok", "qwen3.5:9b", 8))
        self.assertEqual(self.posts[-1][2]["DEFAULT_MODEL_METADATA"],
                         {"capabilities": {"vision": False, "builtin_tools": False}, "x": 1})
        self.assertEqual(self.posts[-1][2]["DEFAULT_MODEL_PARAMS"], {"think": True, "num_thread": 4, "temperature": 0.5})

    def test_handles_a_missing_or_odd_metadata_value(self):
        self.cfg["DEFAULT_MODEL_METADATA"] = None
        self.cfg["DEFAULT_MODEL_PARAMS"] = None
        self.assertTrue(self.webui.set_defaults("tok", "a:b", 2))
        self.assertEqual(self.posts[-1][2]["DEFAULT_MODEL_METADATA"], {"capabilities": {"builtin_tools": False}})

    def test_friendly_model_name_for_everyone(self):
        m = s.find_model(CATALOG, "medium")
        self.assertTrue(self.webui.name_model("tok", m, 8))
        body = self.models["qwen3.5:9b"]
        self.assertEqual(body["name"], "Qwen3.5 9B")
        self.assertIsNone(body["base_model_id"])
        self.assertEqual(body["params"], {"think": False, "num_thread": 8})
        self.assertEqual(body["access_grants"], s.PUBLIC_READ)
        self.assertFalse(self.webui.name_model("tok", m, 8))  # already there

    def test_filter_installed_switched_on_and_kept_up_to_date(self):
        self.assertEqual(self.webui.install_filter("tok"), ["created", "on", "global"])
        self.assertIn("class Filter", self.function["content"])
        self.assertEqual(self.webui.install_filter("tok"), [])
        self.function["content"] = "old"
        self.assertEqual(self.webui.install_filter("tok"), ["updated"])


class BoxFacts(unittest.TestCase):
    def test_packages_never_keep_env_values(self):
        raw = [{"name": "rocketpool.avado.dnp.dappnode.eth", "version": "1.0.2", "state": "running",
                "envs": {"PASSWORD": "secret"}, "manifest": {"title": "Rocket Pool"}},
               {"name": "dappmanager.dnp.dappnode.eth", "version": "10.0.54", "isCORE": True, "state": "running"}]
        out = b.clean_packages(raw)
        self.assertNotIn("secret", json.dumps(out))
        self.assertEqual(out[0]["title"], "Rocket Pool")
        self.assertTrue(out[1]["core"])

    def test_only_read_only_calls(self):
        with self.assertRaises(b.WampError):
            b.dappmanager_call("removePackage", {"id": "x"})

    def test_wamp_call(self):
        sent = []

        class WS:
            def __init__(self, url, proto, timeout=10):
                self.replies = [json.dumps([2, 123, {}]),
                                json.dumps([50, 1, {}, [json.dumps({"success": True, "result": [1, 2]})]])]

            def send(self, text):
                sent.append(json.loads(text))

            def recv(self):
                return self.replies.pop(0)

            def close(self):
                pass

        self.assertEqual(b.dappmanager_call("listPackages", {}, ws_factory=WS), [1, 2])
        self.assertEqual(sent[0][1], "dappnode_admin")
        self.assertEqual(sent[1][3], "listPackages.dappmanager.dnp.dappnode.eth")
        self.assertTrue(sent[1][5]["dontLogError"])

    def test_findings(self):
        diag = {"containers": [
            {"id": "teku.avado.dnp.dappnode.eth", "state": "running",
             "processes": [{"name": "teku", "state": "FATAL"}]},
            {"id": "care.avado.dnp.dappnode.eth", "state": "exited"},
        ], "clients": [
            {"id": "geth.avado.dnp.dappnode.eth", "role": "execution", "api": {"syncing": {"synced": False}, "peers": 3}},
        ]}
        out = b.derive_findings(diag, [{"name": "teku.avado.dnp.dappnode.eth", "title": "Teku"}])
        self.assertEqual(out[0], ("critical", "Teku: the teku process is not running (FATAL)"))
        texts = [t for _, t in out]
        self.assertIn("care is exited", texts)
        self.assertIn("geth is syncing", texts)
        self.assertIn("geth has only 3 peers", texts)
        self.assertEqual(b.derive_findings(None, []), [])

    def test_context_text(self):
        class Prom:
            def query(self, q):
                return {
                    b.Q_RSS: [({"client": "teku"}, 6e9), ({"client": "node-exporter"}, 3e7)],
                    b.Q_HEAD: [({"client": "teku", "network": "mainnet"}, (1793825391 - b.MAINNET_GENESIS) // 12)],
                    b.Q_PEERS: [({"client": "teku"}, 80)],
                    b.Q_HIT: [({"client": "teku"}, 9.6)],
                    b.Q_MISS: [({"client": "teku"}, 0.4)],
                }.get(q, [])

            def one(self, q):
                return {b.Q_DISK_SIZE: 3.9e12, b.Q_DISK_FREE: 3.7e12, b.Q_DISK_SLOPE: -7000.0,
                        b.Q_CPU_BUSY: 0.015}.get(q)

        def call(proc, kwargs, timeout=30):
            if proc == "listPackages":
                return [{"name": "teku.avado.dnp.dappnode.eth", "version": "0.0.77", "state": "running",
                         "manifest": {"title": "Teku"}},
                        {"name": "dappmanager.dnp.dappnode.eth", "version": "10.0.54", "isCORE": True, "state": "running"}]
            return {"containers": [], "clients": []}

        f = b.Facts(prometheus=Prom(), call=call, clock=lambda: 0, wall=lambda: 1793825391,
                    cpu=lambda: "AMD Ryzen 9 8945HS", mem_total=lambda: 66755842048, threads=lambda: 16)
        text = f.context(59e9)
        self.assertTrue(text.startswith("You are the Private Assistant"))
        for want in ("processor AMD Ryzen 9 8945HS (16 threads), memory 64 GB, disk 3.9 TB",
                     "Installed apps: Teku 0.0.77 (running).", "AVADO system parts: dappmanager 10.0.54.",
                     "Free memory: 59 GB.", "Processor busy: 2%.", "Ethereum clients running (memory used): teku 6.0 GB.",
                     "teku (mainnet): head slot", "in sync, 80 peers.",
                     "attestations in the last hour (teku): 10 made, 0 missed.",
                     "Free disk: 3.7 TB of 3.9 TB (95% free); at the last 7 days' pace the disk would be full in more than 10 years.",
                     "Problems found: none.", "seed phrase", "docs.ava.do", "support@ava.do", "I don't know"):
            self.assertIn(want, text)
        # stable facts before the live numbers
        self.assertLess(text.index("Installed apps"), text.index("Free memory"))
        # small enough: about 500 tokens (about 4 characters per token)
        self.assertLess(len(text), 2600)
        self.assertIs(f.context(1), text)  # cached for a minute

    def test_old_dappmanager_and_no_monitoring(self):
        class Prom:
            def query(self, q):
                return None

            def one(self, q):
                return None

        def call(proc, kwargs, timeout=30):
            if proc == "getDiagnostics":
                raise b.WampError("wamp.error.no_such_procedure")
            return []

        text = b.Facts(prometheus=Prom(), call=call, cpu=lambda: None, mem_total=lambda: None,
                       threads=lambda: 4).build(None)
        self.assertIn("Ethereum clients: unknown", text)
        self.assertIn("Problems found: unknown", text)

    def test_runner_memory(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for pid, cmd, anon in (("10", "/usr/local/lib/ollama/llama-server\0--model\0x", 2048),
                               ("11", "python3\0-m\0uvicorn", 99999), ("12", "/usr/local/bin/ollama\0runner\0--model", 1024)):
            os.makedirs(os.path.join(d, pid))
            with open(os.path.join(d, pid, "cmdline"), "w") as f:
                f.write(cmd)
            with open(os.path.join(d, pid, "status"), "w") as f:
                f.write("Name: x\nRssAnon:\t  %d kB\n" % anon)
        self.assertEqual(b.own_runner_anon_bytes(d), 3072 * 1024)

    def test_days_text(self):
        self.assertEqual(b.days_text(None), "the disk is not filling up")
        self.assertEqual(b.days_text(20), "the disk would be full in about 20 days")
        self.assertEqual(b.days_text(400), "the disk would be full in about 13 months")


class MemoryTest(unittest.TestCase):
    def test_reading_and_test_file(self):
        class Prom:
            calls = 0

            def one(self, q):
                Prom.calls += 1
                return 40 * GIB

        t = [0.0]
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        f = os.path.join(d, "test-free-gib")
        m = s.Memory(prometheus=Prom(), available=lambda: 60 * GIB, runner=lambda: 1 * GIB, clock=lambda: t[0],
                     test_file=f)
        r = m.reading()
        self.assertEqual((r["now"], r["lowest_7d"], r["free"], r["source"]), (61.0, 40.0, 40.0, "prometheus"))
        m.reading()
        self.assertEqual(Prom.calls, 1)  # at most every 10 minutes
        t[0] = 601
        m.reading()
        self.assertEqual(Prom.calls, 2)
        with open(f, "w") as fh:
            fh.write("8\n")
        self.assertEqual(m.reading()["free"], 8.0)

    def test_without_monitoring(self):
        class Prom:
            def one(self, q):
                return None

        m = s.Memory(prometheus=Prom(), available=lambda: 20 * GIB, runner=lambda: 0, test_file=None)
        r = m.reading()
        self.assertEqual((r["free"], r["source"]), (20.0, "meminfo"))


class FilterSource(unittest.TestCase):
    """The Open WebUI filter, loaded the way Open WebUI loads it (plain exec), with a fake helper."""

    def load(self, answer):
        with open(os.path.join(os.path.dirname(HERE), "avado_context_filter.py")) as fh:
            src = fh.read()
        ns = {}
        exec(compile(src, "avado_context_filter.py", "exec"), ns)
        ns["_ask"] = lambda model: answer
        ns["Filter"].inlet.__globals__["_ask"] = ns["_ask"]
        return ns["Filter"]()

    def run_inlet(self, f, body):
        import asyncio
        return asyncio.run(f.inlet(body))

    def test_adds_the_facts_first(self):
        f = self.load({"paused": False, "system": "FACTS"})
        body = self.run_inlet(f, {"model": "qwen3.5:9b", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(body["messages"][0], {"role": "system", "content": "FACTS"})
        body = self.run_inlet(f, {"messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "x"}]})
        self.assertEqual(body["messages"][0]["content"], "FACTS\n\nBe brief.")

    def test_paused_stops_the_chat(self):
        f = self.load({"paused": True, "message": "Paused so your Ethereum node has enough memory."})
        with self.assertRaises(Exception) as e:
            self.run_inlet(f, {"messages": [{"role": "user", "content": "hi"}]})
        self.assertIn("Paused", str(e.exception))

    def test_helper_down_still_gives_the_rules(self):
        f = self.load(None)
        body = self.run_inlet(f, {"messages": [{"role": "user", "content": "hi"}]})
        self.assertIn("seed phrase", body["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
