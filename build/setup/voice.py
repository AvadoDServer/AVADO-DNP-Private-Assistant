"""AVADO Private Assistant: speech for Home Assistant (local speech-to-text and text-to-speech).

When Home Assistant turns on "Use my AVADO's assistant for voice", this helper starts two small
Wyoming servers next to the model (both on 127.0.0.1; doors.py lets only Home Assistant reach them):

* speech to text:  wyoming-faster-whisper (faster-whisper, model "auto" = base-int8 on this processor,
                   about 0.5 GB of memory), on 127.0.0.1:10301, reached at assistant.my.ava.do:10300
* text to speech:  wyoming-piper (Piper voices, about 0.2 GB of memory each), on 127.0.0.1:10201,
                   reached at assistant.my.ava.do:10200

Both run as the app user, at nice 10 with the lowest disk priority and OOM score 1000 (the node stays
first), with at most 4 processor threads for speech. Models and voices are kept in the models volume
(/models/voice); a model that is missing is downloaded once from Hugging Face the first time it is
needed (whisper about 80 MB, a voice about 60 MB), or side-loaded there. Turning voice off stops both
and frees their memory. Standard library only (the servers themselves live in /opt/avado/voice).
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

VOICE_BIN = os.environ.get("AVADO_VOICE_BIN", "/opt/avado/voice/bin")
VOICE_DIR = os.environ.get("AVADO_VOICE_DIR", "/models/voice")
STT_PORT = 10301
TTS_PORT = 10201
PUBLIC_STT_PORT = 10300
PUBLIC_TTS_PORT = 10200
# Memory both speech servers take together while in use: measured on the test box 0.49 GB (whisper base-int8)
# + 0.20 GB (Piper, one voice) resident, 2026-11-05
VOICE_GIB = 0.7

# The voice the pipeline uses per language (Piper's own "medium" voices, the quality Home Assistant
# suggests). Any other language: English (Piper has no Croatian voice, for example).
VOICES = {
    "en": "en_US-lessac-medium",
    "de": "de_DE-thorsten-medium",
    "sl": "sl_SI-artur-medium",
    "it": "it_IT-paola-medium",
    "fr": "fr_FR-siwis-medium",
    "es": "es_ES-sharvard-medium",
    "nl": "nl_NL-mls-medium",
    "pl": "pl_PL-gosia-medium",
    "cs": "cs_CZ-jirka-medium",
    "sk": "sk_SK-lili-medium",
    "hu": "hu_HU-anna-medium",
    "sr": "sr_RS-serbski_institut-medium",
}


def language_code(lang):
    """"en-GB" -> "en"; anything odd -> "en"."""
    lang = str(lang or "en").replace("_", "-").split("-")[0].lower()
    return lang if lang.isalpha() and 2 <= len(lang) <= 3 else "en"


def voice_for(lang):
    return VOICES.get(language_code(lang), VOICES["en"])


def port_open(port, host="127.0.0.1", timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def low_priority(cmd):
    """Run cmd at nice 10, idle-ish disk priority, OOM score 1000."""
    return ["sh", "-c", 'echo 1000 > /proc/self/oom_score_adj 2>/dev/null; exec nice -n 10 ionice -c2 -n7 "$@"',
            "voice"] + cmd


def threads():
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def stt_command(lang):
    d = os.path.join(VOICE_DIR, "whisper")
    return [os.path.join(VOICE_BIN, "wyoming-faster-whisper"), "--uri", "tcp://127.0.0.1:%d" % STT_PORT,
            "--model", "auto", "--language", language_code(lang), "--data-dir", d, "--download-dir", d,
            "--cpu-threads", str(threads()), "--stt-library", "faster-whisper"]


def tts_command(lang):
    d = os.path.join(VOICE_DIR, "piper")
    return [os.path.join(VOICE_BIN, "wyoming-piper"), "--uri", "tcp://127.0.0.1:%d" % TTS_PORT,
            "--voice", voice_for(lang), "--data-dir", d, "--download-dir", d]


class Service:
    """One speech server, started again (with a growing pause) when it stops, until stopped."""

    def __init__(self, name, port, popen=subprocess.Popen, clock=time.monotonic):
        self.name = name
        self.port = port
        self.popen = popen
        self.clock = clock
        self.proc = None
        self.cmd = None
        self.want = False
        self.pause = 2
        self.next_start = 0
        self.started_at = None
        self.failures = 0

    def ensure(self, cmd):
        """Called every few seconds by the supervisor."""
        if cmd != self.cmd and self.proc is not None:
            self.stop()
        self.cmd = cmd
        self.want = True
        if self.proc is not None and self.proc.poll() is None:
            if self.started_at is not None and self.clock() - self.started_at > 120:
                self.pause = 2
            return
        if self.proc is not None:
            code = self.proc.poll()
            sys.stderr.write("avado-setup: %s stopped (exit %s), starting it again in %ds\n" % (self.name, code, self.pause))
            self.proc = None
            self.failures += 1
            self.next_start = self.clock() + self.pause
            self.pause = min(self.pause * 2, 120)
            return
        if self.clock() < self.next_start:
            return
        env = dict(os.environ, HF_HOME=os.path.join(VOICE_DIR, "hf"), XDG_CACHE_HOME=os.path.join(VOICE_DIR, "cache"),
                   HOME=VOICE_DIR, ORT_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
        os.makedirs(cmd[cmd.index("--data-dir") + 1], exist_ok=True)
        self.proc = self.popen(low_priority(cmd), env=env, stdin=subprocess.DEVNULL)
        self.started_at = self.clock()
        sys.stderr.write("avado-setup: started %s\n" % self.name)

    def stop(self):
        self.want = False
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        self.pause = 2
        self.next_start = 0

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def ready(self, probe=port_open):
        return self.running() and probe(self.port)


class Voice:
    """Voice on or off (kept in the data volume), and the two servers."""

    def __init__(self, state_dir, popen=subprocess.Popen, clock=time.monotonic, probe=port_open):
        self.path = os.path.join(state_dir, "voice.json")
        self.stt = Service("speech to text", STT_PORT, popen=popen, clock=clock)
        self.tts = Service("text to speech", TTS_PORT, popen=popen, clock=clock)
        self.probe = probe
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    @property
    def on(self):
        return bool(self.data.get("on"))

    @property
    def language(self):
        return language_code(self.data.get("language"))

    def installed(self):
        return os.path.exists(os.path.join(VOICE_BIN, "wyoming-faster-whisper")) and \
            os.path.exists(os.path.join(VOICE_BIN, "wyoming-piper"))

    def set(self, on, language=None, by=None):
        with self.lock:
            self.data["on"] = bool(on)
            if language:
                self.data["language"] = language_code(language)
            if by:
                self.data["by"] = str(by)[:80]
            self.data["at"] = int(time.time())
            self._save()
        self.tick()

    def tick(self):
        """Start or stop the servers to match the setting. Called every few seconds."""
        with self.lock:
            if self.on and self.installed():
                self.stt.ensure(stt_command(self.language))
                self.tts.ensure(tts_command(self.language))
            else:
                if self.stt.proc is not None or self.stt.want:
                    self.stt.stop()
                if self.tts.proc is not None or self.tts.want:
                    self.tts.stop()

    def gib(self):
        """Memory to keep aside for speech (0 while voice is off)."""
        return VOICE_GIB if self.on else 0.0

    def status(self):
        stt, tts = self.stt.ready(self.probe), self.tts.ready(self.probe)
        return {"on": self.on, "ready": bool(self.on and stt and tts), "stt": stt, "tts": tts,
                "language": self.language, "voice": voice_for(self.language),
                "stt_port": PUBLIC_STT_PORT, "tts_port": PUBLIC_TTS_PORT, "installed": self.installed()}

    def run(self, stop_event=None, every=5):
        while not (stop_event and stop_event.is_set()):
            try:
                self.tick()
            except Exception as e:  # never let the supervisor die
                sys.stderr.write("avado-setup: voice supervisor error: %r\n" % e)
            time.sleep(every)
