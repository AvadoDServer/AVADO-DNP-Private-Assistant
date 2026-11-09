# AVADO Private Assistant

`assistant.avado.dnp.dappnode.eth` ("Private Assistant"): a private AI chat assistant that runs
entirely on the AVADO box. [Open WebUI](https://github.com/open-webui/open-webui) is the chat,
[Ollama](https://github.com/ollama/ollama) runs the model on the processor, and a small AVADO setup
helper suggests a model that fits next to the Ethereum node (by the box's real free memory), downloads
it when the owner presses the button, gives every chat a short read-only summary of the box, and pauses
the assistant when the node needs the memory.

Address: http://assistant.my.ava.do (the chat) and http://assistant.my.ava.do/avado/ (the setup page,
also the Admin's Setup tab). Until a model is on the box, `/` opens the setup page. The setup page is
three numbered steps with a live tick (create your account, get the AI model, start chatting) and one
button each; model choice, speed, family members and background facts sit under "More options".

## Models

`build/setup/models.json`, the newest open models on ollama.com/library (checked live 2026-11-04).
Every model runs with thinking off (`think:false`): all of them think by default in Ollama, which
costs about a minute per answer on a processor.

| Model | Ollama tag | Download | Memory while it answers | Suggested |
|---|---|---|---|---|
| LFM3 1.7B | `lfm3:1.7b` | 1.1 GB | 1.5 GiB | when little memory is free |
| Qwen3.5 4B | `qwen3.5:4b` | 3.3 GB | 4.2 GiB | the default |
| Gemma 4 E4B | `gemma4:e4b-it-qat` | 6.1 GB | 6.7 GiB | on request (quicker follow-up questions) |
| Qwen3.5 9B | `qwen3.5:9b` | 6.6 GB | 6.7 GiB | the default when enough memory is free |
| Gemma 4 26B A4B | `gemma4:26b-a4b-it-qat` | 15.6 GB | about 17 GiB | on request |

Memory = Ollama's model runner at an 8k context, KV cache included (measured; the 26B is an estimate).
Open WebUI's model picker and the label above every answer show the friendly name: the helper makes an
Open WebUI model entry per model (id = the Ollama tag, name = "Qwen3.5 9B", readable by every account).
LFM3 is under the LFM Open License v1.0 (free for commercial use below USD 10 M yearly revenue); the
others are Apache 2.0. The package downloads them from the Ollama library; it bundles none.

## Sizing: by real free memory, not by the memory fitted

```
free   = min(MemAvailable now, lowest MemAvailable in the last 7 days)
budget = free - 4 GiB (the node's margin) - 0.5 GiB (Open WebUI) - 1 GiB (the runner's prompt cache)
suggestion = the biggest "auto" model whose memory need fits the budget (and its download + 50 GB the disk)
```

- The 7-day minimum comes from the box's Prometheus (`min_over_time(node_memory_MemAvailable_bytes[7d])`),
  reached from the container at `http://prometheus.my.ava.do:9090/api/v1` (the AVADO network's name for
  the monitoring package), then the Admin's read-only proxy `http://my.ava.do/metrics-api`. Without the
  monitoring package, only `/proc/meminfo` counts. MemAvailable now has the assistant's own runner memory
  added back, so a loaded model does not count against itself.
- Below 7.5 GiB free nothing is suggested or downloaded, and the page says so.
- The setup page says it in words: "Even at its busiest, your AVADO has about 42 GB free. The assistant
  will use about 8 GB."
- Free memory is checked every minute (the 7-day minimum every 10 minutes) and before every chat. When
  it drops below the model's need + 5.5 GiB, the helper unloads the model (`keep_alive: 0`), chats answer
  "Paused so your Ethereum node has enough memory", and the assistant resumes by itself 1 GiB above that
  line.
- Box tests only: a number (GiB) in `/run/avado/test-free-gib` inside the container replaces the
  measured free memory. `/run` is not a volume, so the file is gone after an update or reinstall.

## First start

Nothing is downloaded by itself. The setup page has three steps: create your account, get the AI
model (it shows the suggestion, its download size and the memory it will use; the owner presses
"Download Qwen3.5 9B"), start chatting. A model already in the models volume (side-loaded, or added in
Open WebUI) is used as it is. The download shows, for example, "1.2 GB of 6.6 GB downloaded (8 MB per
second). Time left: about 10 minutes." A lost connection is retried every minute and the download goes
on where it stopped; a full disk or a model that left the library stops with a plain message and a
"Try again" button. Once the model is on the box, `/` opens the chat and the helper measures how fast it
writes on this box (two short answers, the better one counts).

The buttons work only for a signed-in Open WebUI admin (the helper checks the `token` cookie with Open
WebUI's `/api/v1/auths/`) and need the `X-Avado-Request: 1` header, so another web page cannot press
them. While the owner has the page open, the helper keeps Open WebUI's settings in step through its admin
API with the owner's own sign-in: default model, `DEFAULT_MODEL_PARAMS` (`think:false`, `num_thread`),
built-in tools off, the friendly model names and the "About this AVADO" filter.

## About this AVADO (the box's facts in every chat)

An Open WebUI filter function (`build/setup/avado_context_filter.py`, installed, switched on and global)
puts a short system message in front of every chat (about 400 to 500 tokens):

1. the rules: answer in the user's language in plain words; for box questions use only the facts and say
   "I don't know" otherwise; never ask for or accept a seed phrase, keystore, private key or password;
   for exits, withdrawals and money point to docs.ava.do and support@ava.do;
2. stable facts: processor, memory, disk; installed apps and versions (DAPPMANAGER `listPackages`, read
   at most every 10 minutes, environment values dropped);
3. live numbers last, rounded: free memory, processor use, the Ethereum clients' memory
   (`process_resident_memory_bytes` by client), sync head and peers (`beacon_head_slot`,
   `libp2p_peers` / `p2p_peer_count`, the Admin Home's queries), attestations made and missed in the last
   hour (`validator_monitor_prev_epoch_on_chain_attester_hit_total` / `miss_total`), free disk and when it
   would be full (`deriv(node_filesystem_avail_bytes[7d])`), "Problems found" from DAPPMANAGER
   `getDiagnostics` (10.0.53+, the Admin's "What we found" rules without logs; older DAPPMANAGERs: "unknown").

The facts are rebuilt at most once a minute (a chat never waits for a rebuild; it gets the previous text),
so the stable start of the prompt stays the same and Ollama can reuse it where the model allows (Gemma 4
does; Qwen3.5 re-reads the whole prompt every turn in Ollama 0.40.14, see the box test in the PR).

How the helper calls the DAPPMANAGER: like AI Connect (AVADO-DNP-MCP), WAMP on
`ws://wamp.my.ava.do:8080/ws`, realm `dappnode_admin`, anonymous, with a small standard-library WebSocket
client (`build/setup/boxfacts.py`). Only `listPackages` and `getDiagnostics` can be called. The filter
reads the facts from the helper's `/internal/context` on 127.0.0.1 with a random token kept in the data
volume (mode 600); nginx never forwards `/internal/`.

## Sign-in

- `WEBUI_AUTH=true`: nothing is usable without an account (`/api/models` answers 401).
- The first account made becomes the admin ("owner"), and Open WebUI then closes sign-up
  (`ENABLE_SIGNUP=false` as well); the owner adds other people in Open WebUI (Admin panel, Users).
- The setup page tells the owner to make the account first, so nobody else on the network can.
- The sign-in secret is kept in the data volume, so an update does not sign everybody out.

## Keeping the node first

AVADO's package format has no CPU or memory limits (the DAPPMANAGER does not pass them on), so:

- The sizing and pause rules above.
- Ollama runs with `nice 10` and the lowest best-effort disk priority, Open WebUI with `nice 5`; the model
  uses half the processor threads (`num_thread` = threads / 2, in `DEFAULT_MODEL_PARAMS`, in every model
  entry and in the speed test).
- Ollama has `oom_score_adj 1000`: if the box runs out of memory, Linux stops the model, not the node.
- `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_KEEP_ALIVE=10m`,
  `OLLAMA_CONTEXT_LENGTH=8192`, `LLAMA_ARG_CACHE_RAM=1024` (llama-server's prompt cache may otherwise grow
  to 8 GiB, unseen by Ollama's scheduler).
- The background extras that would make every chat slower on a processor are off: follow-up
  suggestions, tags, search and retrieval queries, autocomplete. Titles stay on.
- Open WebUI's built-in tools (search memories, notes, chats and knowledge; ask the user; time; from 0.13
  also "create a tool from chat") are off for every model (`DEFAULT_MODEL_METADATA` capabilities
  `builtin_tools: false`), and so are workspace (Python) tools and scheduled automations
  (`ENABLE_TOOLS=false`, `ENABLE_AUTOMATIONS=false`; `ENABLE_TOOLS` also removes "create a tool from
  chat"). With the built-in tools on, a one-line question becomes a 5,500-token prompt that a processor
  needs minutes to read. Functions stay on for the filter. Tool servers are on (`ENABLE_TOOL_SERVERS=true`)
  for the one entry the helper makes for live checks (below); at most 3 rounds of tool calls per answer
  (`CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS=3`).

## Live checks of the box (AI Connect, 0.2.0)

With [AI Connect](https://github.com/AvadoDServer/AVADO-DNP-MCP) on the box, the assistant can call
four of its read-only tools while it answers, so "Is my AVADO healthy?" is answered from a fresh check:

| Tool (as the model sees it) | AI Connect tool | What |
|---|---|---|
| `avado_health_check` | `health_check` | the Admin's own health rules, plus processes down / out of memory / restarting |
| `avado_list_apps` | `list_apps` | installed apps, version, running or not |
| `avado_ethereum_status` | `ethereum_status` | per node app: in sync or how far behind, peers, validator count |
| `avado_app_logs` | `app_logs` | the newest lines of one app's log, secrets hidden by AI Connect |

- **Turning it on** (setup page, More options, "Live checks of your box"; owner only): make a connection
  named "Private Assistant" on the AI Connect page with no extra permissions, paste its key, press
  Connect. A key that may restart, install or update apps is refused: the assistant only looks.
  AI Connect can also send the owner straight to `http://assistant.my.ava.do/avado/#aiconnect=<key>`;
  the page then has the key filled in and the owner presses Connect (the key in the `#` part never
  reaches a server log).
- **Open WebUI never sees the key.** The helper (`build/setup/aiconnect.py`) registers one MCP tool
  server in Open WebUI (`http://127.0.0.1:8090/internal/mcp`, bearer = the helper's internal token,
  owner-only access, `function_name_filter_list` = the four tools) and adds it to the model entries'
  `toolIds`, so it is on in every new chat. The helper passes each MCP message on to
  `http://mcp.my.ava.do/mcp` with the owner's key (kept in `aiconnect.json` in the data volume, mode
  600). On the way it lists and allows only the four tools, asks for at most 40 log lines, drops the
  duplicate `structuredContent` and shortens every answer to 6,000 characters (for logs it keeps the
  newest part), so one tool call adds at most about 1,500 tokens for the processor to read.
- Native function calling (Ollama `tools`) with the catalogue's Qwen3.5 4B / 9B, the best small models at
  tool calls (BFCL-V4, MCP-Atlas). The chat rules tell the model to call the matching tool before it
  answers a question about the box, and not to paste raw logs.
- Family members' accounts do not get the box checks (the tool server is owner-only).

## "Explain this" for the Admin (0.2.0)

`POST http://assistant.my.ava.do/avado/api/explain` explains ONE finding of the Admin's health check in
plain words, written word by word as server-sent events (`build/setup/explain.py`). For the AVADO Admin
only: CORS for `http(s)://my.ava.do`, the `X-Avado-Request: 1` header, JSON of at most 16 kB.

```
POST /avado/api/explain
Origin: http://my.ava.do
Content-Type: application/json
X-Avado-Request: 1

{"finding": {"id": "disk-high", "severity": "warning", "topic": "storage",
             "title": "Your disk is 91% full", "why": "...", "fix": {"label": "Free up space"}},
 "app": {"title": "Geth", "version": "10.0.84", "running": true},
 "context": {"verdict": "Needs attention", "disk": {"pct": 91},
             "processes": [{"name": "geth", "state": "RUNNING", "lastError": "..."}],
             "sync": {"syncing": false, "distance": 0, "peers": 47}},
 "question": "optional, at most 300 characters"}

200 text/event-stream
event: text   data: {"text": "Your disk is almost full. "}   (repeated)
event: done   data: {"cached": false, "stopped": false}
```

- Errors answer JSON `{"error": "..."}`: 409 no model yet, 503 paused for the node, 429 another
  explanation is being written (one at a time), 400/413/415 a bad request, 403 another page.
- The helper builds the prompt itself: 3 to 6 plain sentences (what it means, why it matters, what to
  do), only from the facts given, no commands, paths or settings outside the Admin, and only the
  finding's own fix button, by its label. Validator and money findings end with "If you are unsure, ask
  AVADO support at support@ava.do". Every string is scrubbed again with the Admin's rules
  (`health/diagnostics.js` `scrub()`).
- At most 2 minutes per answer; a finished answer is kept for an hour (same finding id, text, fix and
  question, same model), so opening it again is instant.
- `GET /avado/api/status` is readable by the Admin too (CORS) and has `"api": {"explain": true}`, so the
  Admin shows "Explain" only when the assistant is installed, has a model and is new enough.

## Doors for Home Assistant (0.2.0)

Everything above listens on 127.0.0.1 or behind the sign-in. `build/setup/doors.py` opens three doors on
the container's AVADO address for ONE named app, Home Assistant (`AVADO_APP_DOORS`, default
`homeassistant.my.ava.do`): a connection is accepted only from the address the box's name server gives
for that name (looked up again when an unknown address knocks, at most every 5 s, and every 60 s).

| Door | What | Who |
|---|---|---|
| `assistant.my.ava.do:11434` | Ollama's API, for Home Assistant's Ollama integration | Home Assistant, with a key |
| `assistant.my.ava.do:10300` | speech to text (Wyoming, faster-whisper) | Home Assistant, while voice is on |
| `assistant.my.ava.do:10200` | text to speech (Wyoming, Piper) | Home Assistant, while voice is on |

- The model door also wants a key, which Home Assistant gets once from `POST /avado/connect` (only it can
  call that). It lets through only `GET /api/version`, `/api/tags`, `/api/ps` and `POST /api/show`,
  `/api/chat`, `/api/generate`: nothing can be downloaded, copied, created or deleted. Every chat gets the
  assistant's own settings: thinking off, half the threads, the 8k context (so the model is never loaded
  twice), at most 10 minutes in memory. While the assistant is paused for the node, a chat answers 503
  "Paused so your Ethereum node has enough memory".
- `POST /avado/voice {"on": true, "language": "en"}` (with the key) starts or stops the speech servers;
  `GET /avado/status` tells Home Assistant the model and whether speech is ready.

## Voice for Home Assistant (0.2.0)

`build/setup/voice.py` runs two small Wyoming servers while Home Assistant has voice on, in their own
virtual environment (`/opt/avado/voice`, every version pinned in `build/voice/requirements.txt`):
**wyoming-faster-whisper 3.8.1** (faster-whisper 1.2.1, model `auto` = `base-int8` on this processor,
about 80 MB) and **wyoming-piper 2.5.2** (Piper 1.8.0; the voice follows Home Assistant's language:
`en_US-lessac-medium`, `de_DE-thorsten-medium`, `sl_SI-artur-medium`, ...; about 60 MB each). Both are
the newest on PyPI (2026-11-05). They run at nice 10, OOM score 1000, at most 4 threads, as the app
user; models are fetched once into `/models/voice` the first time voice is turned on (or side-loaded
there). Speech counts in the sizing: 0.7 GiB more is kept aside while voice is on (the pause line and
the suggestion both include it). Turning voice off stops both.

Home Assistant's side (the switch, the Assist pipeline) is in AVADO-DNP-HomeAssistant 0.2.0.

## Privacy

- Ollama, Open WebUI and the helper listen on 127.0.0.1 only; nginx on port 80 (and 443, see HTTPS
  below) is the only listener on the AVADO network for people. The doors for Home Assistant (above) answer only Home Assistant. Requests with any other host name (an IP address, a name a web page made up to
  reach the box) are redirected to `assistant.my.ava.do`. No host ports.
- No cloud: `OLLAMA_NO_CLOUD=1`, `ENABLE_OPENAI_API=false`, no direct connections, no community
  sharing, `OFFLINE_MODE=true` (no update checks, no downloads from Hugging Face).
- The box goes online only to download models from the Ollama library and package updates.
- Documents added to a chat are given to the model whole ("full context",
  `BYPASS_EMBEDDING_AND_RETRIEVAL=true`): the slim Open WebUI image keeps document search data only in
  an external PostgreSQL server (`VECTOR_DB=pgvector`), which the box does not have.

## Image

One container, built in `build/Dockerfile` from:

- `ghcr.io/open-webui/open-webui:v0.13.0-slim` (Debian 13), pinned by digest;
- the Ollama program and its processor libraries from `ollama/ollama:0.40.15`, pinned by digest. The
  upstream image is about 5 GB, nearly all graphics-card libraries (CUDA, MLX, Vulkan) no AVADO box can
  use; only about 70 MB is kept;
- nginx and tini from Debian.

One package rather than two (Ollama + Open WebUI with a dependency): the DAPPMANAGER makes one
container per package, a dependency would add a store release-order trap and a second install, and
keeping Ollama inside the container means its API (which has no sign-in) is never on the AVADO
network.

tini is PID 1; `build/avado-start` runs Ollama, the helper, Open WebUI and nginx, each restarted if it
stops, all but nginx's master as the unprivileged user `assistant` with no capabilities. The image is
about 1.2 GB (312 MB as the `.tar.xz` the store ships; 0.1.0 was 160 MB, the rest is the speech part). The helper's unit tests run during the image
build; a failing test fails the build.

Volumes: `assistant_models` (`/models`: Ollama's models and key) and `assistant_data`
(`/app/backend/data`: accounts, chats, settings, the helper's state).

Runs on today's boxes (Debian 11, Docker 20.10, docker-compose 1.25): no `devices`, not privileged, no
DAPPMANAGER change. No dependencies.

**HTTPS.** The page also answers on https://assistant.my.ava.do (port 443) with the box's
`*.my.ava.do` certificate, the one the Admin and the other AVADO apps with https use (the
box's DAPPMANAGER serves it at http://dappmanager.my.ava.do; iso.ava.do is the second source).
Chrome tries https first ("Always use secure connections"), so without it Chrome showed "This
site doesn't support a secure connection". The certificate is used only when it is for
`*.my.ava.do`, valid for at least another hour and matches its key; until then the page stays
http only and it is checked every 5 minutes, after that daily. http works as before, also in
the Admin's Setup tab. nginx serves it (`/run/avado/https/https.conf`, written by `build/avado-start` with `build/setup/avadotls.py`, the same file as in the AVADO Bitcoin packages); if nginx refuses the files, it stays http only. Open WebUI accepts both addresses (`CORS_ALLOW_ORIGIN`).

## Not in this version

- Graphics-chip acceleration (needs AVADO OS 2 and `devices` support in the DAPPMANAGER).
- Voice input in the browser. Voice works through Home Assistant (its apps and voice devices).
- One-click hand-over of the AI Connect key from AI Connect's page (the receiving end, `#aiconnect=`, is
  here; AI Connect's button is not).
- Document search with embeddings (needs a vector store the slim image can use locally).

## Development

```
cd build/setup && python3 -m unittest discover -s tests
docker buildx build --platform linux/amd64 --load -t assistant-test -f build/Dockerfile .
docker run --rm -p 127.0.0.1:8480:80 assistant-test   # nothing is downloaded until the button
```

`AVADO_CATALOG` points the helper at another model list (for a test with a tiny model).

## Licences

AVADO's own files in this repository: GPL-3.0 (see `LICENSE`). The image contains Open WebUI
(Copyright (c) 2023- Open WebUI Inc.; Open WebUI License, BSD-3-Clause based, with a branding clause:
the "Open WebUI" name and logo are kept unchanged), Ollama (Copyright (c) Ollama; MIT), and for speech
wyoming-faster-whisper, wyoming-piper and wyoming (MIT), Piper (`piper-tts`, GPL-3.0), faster-whisper
(MIT), CTranslate2 (MIT) and ONNX Runtime (MIT), installed unchanged from PyPI. Their
licence texts are in `build/licenses/` and in the image under `/usr/share/doc/avado-assistant/`. DAppNode's `DAppNodePackage-ollama-openwebui-generic` and `DAppNodePackage-ollama-generic`
were read for reference only (they have no licence file); no code or files were copied from them.
