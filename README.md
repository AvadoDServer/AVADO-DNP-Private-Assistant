# AVADO Private Assistant

`assistant.avado.dnp.dappnode.eth` ("Private Assistant"): a private AI chat assistant that runs
entirely on the AVADO box. [Open WebUI](https://github.com/open-webui/open-webui) is the chat,
[Ollama](https://github.com/ollama/ollama) runs the model on the processor, and a small AVADO setup
helper picks a model that suits the box, downloads it with progress in plain words and measures how
fast it is.

Address: http://assistant.my.ava.do (the chat) and http://assistant.my.ava.do/avado/ (the setup page,
also the Admin's Setup tab). Until a model is on the box, `/` opens the setup page. The setup page is
three numbered steps with a live tick (create your account, get the AI model, start chatting) and one
button each; model choice, speed, family members and background facts sit under "More options".

## What it does on first start

1. The helper reads the box's memory and free space and picks a model (`build/setup/models.json`):

   | Box memory (as sold) | Linux reports | Automatic choice | Download |
   |---|---|---|---|
   | up to 16 GB | < 20 GiB | Qwen3 1.7B | 1.4 GB |
   | 24-32 GB | 20-44 GiB | Qwen3 4B Instruct 2507 | 2.5 GB |
   | 48-64 GB | 44-80 GiB | Qwen3 30B-A3B Instruct 2507 (mixture of experts) | 19 GB |
   | 96 GB | 80-112 GiB | Qwen3-Next 80B-A3B Instruct (mixture of experts) | 50 GB |
   | 128 GB+ | 112+ GiB | gpt-oss 120B (mixture of experts) | 65 GB |

   gpt-oss 20B is offered on boxes from 40 GiB but never picked automatically (it always thinks
   before answering, which is slow on a processor). The memory thresholds leave 20-25 GB for an
   Ethereum node. A model is only offered when the download plus 50 GB stays free on the disk.
2. Open WebUI starts with that model as the default for new chats.
3. The model is downloaded through Ollama. The setup page shows, for example, "1.2 GB of 19 GB
   downloaded (8 MB per second). Time left: about 40 minutes." A lost connection is retried every
   minute and the download goes on where it stopped; a full disk or a model that left the library
   stops with a plain message and a "Try again" button.
4. Once the model is on the box, `/` opens the chat, and the helper measures how fast it writes on this
   box (two short answers, the better one counts) and shows it in words ("about 9 words per second:
   faster than most people read").

The owner can choose another model, stop a download or remove a model on the setup page. Those buttons
work only for a signed-in Open WebUI admin (the helper checks the `token` cookie with Open WebUI's
`/api/v1/auths/`) and need the `X-Avado-Request: 1` header, so another web page cannot press them.
The helper also keeps Open WebUI's default model in step with the chosen one (through Open WebUI's
admin API, with the owner's own sign-in, while the owner has the setup page open).

## Sign-in

- `WEBUI_AUTH=true`: nothing is usable without an account (`/api/models` answers 401).
- The first account made becomes the admin ("owner"), and Open WebUI then closes sign-up
  (`ENABLE_SIGNUP=false` as well); the owner adds other people in Open WebUI (Admin panel, Users).
- The setup page tells the owner to make the account first, so nobody else on the network can.
- The sign-in secret is kept in the data volume, so an update does not sign everybody out.

## Keeping the node first

AVADO's package format has no CPU or memory limits (the DAPPMANAGER does not pass them on), so:

- Ollama runs with `nice 15` and the lowest best-effort disk priority, Open WebUI with `nice 10`.
- Ollama has `oom_score_adj 1000`: if the box runs out of memory, Linux stops the model, not the node.
- One answer at a time (`OLLAMA_NUM_PARALLEL=1`) and one model in memory at a time
  (`OLLAMA_MAX_LOADED_MODELS=1`, so a chat with a second model cannot load two big models next to the
  node); the model is unloaded after 10 minutes without use, context 16k tokens.
- The background extras that would make every chat slower on a processor are off: follow-up
  suggestions, tags, search and retrieval queries, autocomplete. Titles stay on.
- Open WebUI's built-in tools (search memories, notes, chats and knowledge; ask the user; time) are off
  for every model (`DEFAULT_MODEL_METADATA` capabilities `builtin_tools: false`). With them on, a
  one-line question becomes a 5,500-token prompt; on the test box (Ryzen 9 8945HS, Qwen3 30B) the first
  question then took over 2 minutes, searched six empty tools and ended with a question back instead
  of an answer. Off: 22-31 prompt tokens, first words after 2-3 s (15 s when the model was unloaded),
  about 27 tokens per second. The image setting only seeds Open WebUI's first start, so the setup helper
  also sets it through Open WebUI's admin API when the owner opens the setup page, unless the owner has
  set the switch himself. Documents added to a chat still work.

## Privacy

- Ollama, Open WebUI and the helper listen on 127.0.0.1 only; nginx on port 80 is the only listener on
  the AVADO network. Requests with any other host name (an IP address, a name a web page made up to
  reach the box) are redirected to `assistant.my.ava.do`. No host ports.
- No cloud: `OLLAMA_NO_CLOUD=1`, `ENABLE_OPENAI_API=false`, no direct connections, no community
  sharing, `OFFLINE_MODE=true` (no update checks, no downloads from Hugging Face).
- The box goes online only to download models from the Ollama library and package updates.
- Documents added to a chat are given to the model whole ("full context",
  `BYPASS_EMBEDDING_AND_RETRIEVAL=true`): the slim Open WebUI image keeps document search data only in
  an external PostgreSQL server (`VECTOR_DB=pgvector`), which the box does not have.

## Image

One container, built in `build/Dockerfile` from:

- `ghcr.io/open-webui/open-webui:v0.12.5-slim` (Debian 13, Python 3.12), pinned by digest;
- the Ollama program and its processor libraries from `ollama/ollama:0.40.13`, pinned by digest. The
  upstream image is about 5 GB, nearly all graphics-card libraries (CUDA, MLX, Vulkan) no AVADO box can
  use; only about 70 MB is kept;
- nginx and tini from Debian.

One package rather than two (Ollama + Open WebUI with a dependency): the DAPPMANAGER makes one
container per package, a dependency would add a store release-order trap and a second install, and
keeping Ollama inside the container means its API (which has no sign-in) is never on the AVADO
network.

tini is PID 1; `build/avado-start` runs Ollama, the helper, Open WebUI and nginx, each restarted if it
stops, all but nginx's master as the unprivileged user `assistant` with no capabilities. The image is
about 690 MB (160 MB as the `.tar.xz` the store ships). The helper's unit tests run during the image
build; a failing test fails the build.

Volumes: `assistant_models` (`/models`: Ollama's models and key) and `assistant_data`
(`/app/backend/data`: accounts, chats, settings, the helper's state).

Runs on today's boxes (Debian 11, Docker 20.10, docker-compose 1.25): no `devices`, not privileged, no
DAPPMANAGER change. No dependencies.

## Not in this version

- Graphics-chip acceleration (needs AVADO OS 2 and `devices` support in the DAPPMANAGER).
- HTTPS (voice input in the browser needs it); the page is plain HTTP like most AVADO apps.
- Document search with embeddings (needs a vector store the slim image can use locally).

## Development

```
cd build/setup && python3 -m unittest discover -s tests
docker buildx build --platform linux/amd64 --load -t assistant-test -f build/Dockerfile .
docker run --rm -p 127.0.0.1:8480:80 -e AUTO_DOWNLOAD=no assistant-test   # no model download
```

`AVADO_CATALOG` points the helper at another model list (for a test with a tiny model).

## Licences

AVADO's own files in this repository: GPL-3.0 (see `LICENSE`). The image contains Open WebUI
(Copyright (c) 2023- Open WebUI Inc.; Open WebUI License, BSD-3-Clause based, with a branding clause:
the "Open WebUI" name and logo are kept unchanged) and Ollama (Copyright (c) Ollama; MIT). Their
licence texts are in `build/licenses/` and in the image under `/usr/share/doc/avado-assistant/`. DAppNode's `DAppNodePackage-ollama-openwebui-generic` and `DAppNodePackage-ollama-generic`
were read for reference only (they have no licence file); no code or files were copied from them.
