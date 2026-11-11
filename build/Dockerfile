# AVADO Private Assistant package image: Ollama (processor only) + Open WebUI +
# the AVADO setup helper, behind nginx, in one container.
#
# Build context is the repository root (docker-compose.yml: context ".",
# dockerfile "build/Dockerfile"), so the image can read the package version
# from dappnode_package.json. See /.dockerignore for what is sent.

# Upstream images, pinned by digest (the multi-arch index digest). The tag is
# only a label; the digest decides. Update tag and digest together: get the
# digest with `docker buildx imagetools inspect <image>:<tag>` ("Digest:" line).
# Checked 2026-11-04.
ARG OPENWEBUI_IMAGE=ghcr.io/open-webui/open-webui:v0.13.3-slim@sha256:8faf917243afee55e042b10ddc08ed6ae6fa9c62522c7e14d81e7cf2922325ae
ARG OLLAMA_IMAGE=ollama/ollama:0.40.16@sha256:910d0d8107b538fb0894e63163eb195de0cc7aefcbc666dc62d3b18f71124592

########## Ollama, processor only
# The upstream image is about 5 GB, nearly all of it graphics-card libraries
# (CUDA, MLX, Vulkan) that no AVADO box can use. Only the program and the
# processor libraries (one per processor generation; Ollama picks the best
# one at start) are kept: about 70 MB.
FROM --platform=linux/amd64 ${OLLAMA_IMAGE} AS ollama
RUN set -eux; \
    mkdir -p /out/bin /out/lib/ollama; \
    cp -a /usr/bin/ollama /out/bin/ollama; \
    find /usr/lib/ollama -maxdepth 1 \( -type f -o -type l \) -exec cp -a {} /out/lib/ollama/ \; ; \
    test -e /out/lib/ollama/libggml-cpu-x64.so; \
    test -e /out/lib/ollama/libggml-cpu-zen4.so; \
    test -e /out/lib/ollama/libggml-base.so.0; \
    ! test -e /out/lib/ollama/cuda_v12

########## Speech for Home Assistant (voice.py): Wyoming speech to text (faster-whisper) and text to
# speech (Piper), in their own virtual environment, every version pinned (build/voice/requirements.txt).
# No models inside: they are fetched into the models volume the first time voice is turned on.
FROM --platform=linux/amd64 ${OPENWEBUI_IMAGE} AS voice
COPY build/voice/requirements.txt /tmp/voice-requirements.txt
RUN set -eux; \
    python3 -m venv /opt/avado/voice; \
    /opt/avado/voice/bin/pip install --no-cache-dir --only-binary :all: --no-deps -r /tmp/voice-requirements.txt; \
    /opt/avado/voice/bin/pip check; \
    /opt/avado/voice/bin/wyoming-faster-whisper --help >/dev/null; \
    /opt/avado/voice/bin/wyoming-piper --help >/dev/null; \
    /opt/avado/voice/bin/pip uninstall -y pip; \
    find /opt/avado/voice -name __pycache__ -type d -prune -exec rm -rf {} +

########## Setup helper tests (a failing test fails the image build)
FROM --platform=linux/amd64 ${OPENWEBUI_IMAGE} AS test
COPY build/setup /opt/avado/setup
RUN cd /opt/avado/setup && python3 -I -m unittest discover -s tests -v 2>&1 | tail -n 5 \
    && python3 -I -m unittest discover -s tests >/dev/null 2>&1 \
    && rm -rf tests __pycache__

########## Runtime
FROM --platform=linux/amd64 ${OPENWEBUI_IMAGE}

# nginx: the only listener on the AVADO network. tini: PID 1, passes the stop
# signal on and collects finished processes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx tini \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled /etc/nginx/conf.d /var/www/html

COPY --from=ollama /out/bin/ollama /usr/local/bin/ollama
COPY --from=ollama /out/lib/ollama /usr/local/lib/ollama
COPY --from=test /opt/avado/setup /opt/avado/setup
COPY --from=voice /opt/avado/voice /opt/avado/voice
COPY build/nginx/nginx.conf /etc/nginx/nginx.conf
COPY build/nginx/avado-starting.html /opt/avado/nginx/avado-starting.html
COPY build/avado-start build/avado-ollama /usr/local/bin/
# Licence texts of the two upstream programs (their copyright notices travel with the image).
COPY build/licenses/ /usr/share/doc/avado-assistant/

# The unprivileged user everything but nginx's master process runs as. Folders
# that start a new volume are owned by it, so the volumes are too.
COPY dappnode_package.json /tmp/dappnode_package.json
RUN set -eux; \
    useradd --system --uid 10001 --user-group --home-dir /models --no-create-home --shell /usr/sbin/nologin assistant; \
    mkdir -p /models/models /models/voice /app/backend/data/avado; \
    chown -R assistant:assistant /models /app/backend/data /app/backend/open_webui/static; \
    python3 -c 'import json,re,sys; v=json.load(open("/tmp/dappnode_package.json"))["version"]; \
        sys.exit("bad package version") if not re.fullmatch(r"[0-9A-Za-z.+-]+", v or "") else None; \
        open("/opt/avado/package-version","w").write(v+"\n")'; \
    rm /tmp/dappnode_package.json; \
    chmod 755 /usr/local/bin/avado-start /usr/local/bin/avado-ollama; \
    mkdir -p /run/avado/https; : > /run/avado/https/https.conf; \
    nginx -t; \
    /usr/local/bin/ollama --version 2>&1 | grep -q "0.40.16"; \
    test -f /opt/avado/setup/public/index.html; \
    test -x /opt/avado/voice/bin/wyoming-faster-whisper; \
    ! test -e /opt/avado/setup/tests

# Ollama: on the box only (127.0.0.1), models in the models volume, never the
# cloud, one answer at a time, one model in memory at a time (a second big
# model next to the first could push the node out of memory), unloaded after
# 10 minutes without use, an 8k context (what the memory figures in models.json
# are measured at). LLAMA_ARG_CACHE_RAM caps the model runner's prompt cache
# (llama-server keeps earlier chats' state in memory, by default up to 8 GiB,
# which neither Ollama nor its scheduler counts) at 1 GiB; the setup helper
# reserves that 1 GiB when it sizes the model.
# Open WebUI: sign-in required; the first account becomes the owner and closes
# sign-up (the owner adds other people in Open WebUI's admin panel),
# no cloud AI connections, no update checks or sharing, and none of the extra
# background answers (follow-up questions, tags, search queries) that would
# make every chat slower on a processor. A document added to a chat is given
# to the model whole ("full context"): the slim image stores document search
# data only in an external PostgreSQL server, which the box does not have.
# Open WebUI's built-in tools (search memories, notes, chats; ask the user;
# time; and from 0.13 "create a tool from chat") are off for every model: they
# add about 5,500 tokens to every question, which a processor needs one to two
# minutes to read, and the model then tends to search empty notes and ask a
# question back instead of answering (test box). Workspace (Python) tools and
# scheduled automations are off as well (ENABLE_TOOLS also removes "create a
# tool from chat"); Functions stay on for the "About this AVADO" filter.
# Tool servers are on, for one entry made by the setup helper when the owner connects AI Connect:
# Open WebUI's MCP client then reaches AI Connect's four read-only box checks through the helper
# (127.0.0.1:8090/internal/mcp, see aiconnect.py). At most 3 rounds of tool calls per answer, so a
# small model cannot loop.
# DEFAULT_MODEL_PARAMS: thinking off (every model in the catalogue thinks by
# default in Ollama, which costs a minute per answer on a processor); avado-start
# adds num_thread = half the processor threads. Documents added to a chat still
# work. These only seed the first start; the setup helper keeps them so (unless
# the owner sets them himself).
ENV OLLAMA_HOST=127.0.0.1:11434 \
    OLLAMA_MODELS=/models/models \
    OLLAMA_NO_CLOUD=1 \
    OLLAMA_KEEP_ALIVE=10m \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_MAX_LOADED_MODELS=1 \
    OLLAMA_MAX_QUEUE=16 \
    OLLAMA_CONTEXT_LENGTH=8192 \
    LLAMA_ARG_CACHE_RAM=1024 \
    WEBUI_URL=http://assistant.my.ava.do \
    OLLAMA_BASE_URL=http://127.0.0.1:11434 \
    USE_OLLAMA_DOCKER=false \
    WEBUI_AUTH=true \
    ENABLE_SIGNUP=false \
    DEFAULT_USER_ROLE=pending \
    ENABLE_OPENAI_API=false \
    ENABLE_DIRECT_CONNECTIONS=false \
    ENABLE_VERSION_UPDATE_CHECK=false \
    OFFLINE_MODE=true \
    ENABLE_COMMUNITY_SHARING=false \
    ENABLE_FOLLOW_UP_GENERATION=false \
    ENABLE_TAGS_GENERATION=false \
    ENABLE_AUTOCOMPLETE_GENERATION=false \
    ENABLE_SEARCH_QUERY_GENERATION=false \
    ENABLE_RETRIEVAL_QUERY_GENERATION=false \
    ENABLE_CODE_INTERPRETER=false \
    ENABLE_EVALUATION_ARENA_MODELS=false \
    ENABLE_TOOLS=false \
    ENABLE_TOOL_SERVERS=true \
    CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS=3 \
    ENABLE_AUTOMATIONS=false \
    ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=false \
    BYPASS_EMBEDDING_AND_RETRIEVAL=true \
    DEFAULT_MODEL_METADATA='{"capabilities":{"builtin_tools":false}}' \
    DEFAULT_MODEL_PARAMS='{"think":false}' \
    CORS_ALLOW_ORIGIN="http://assistant.my.ava.do;https://assistant.my.ava.do" \
    AVADO_STATE_DIR=/app/backend/data/avado \
    AVADO_APP_DOORS=homeassistant.my.ava.do \
    AVADO_VERSION_FILE=/opt/avado/package-version

WORKDIR /app/backend
# 80: the chat and the setup page. 11434, 10300, 10200: the doors for Home Assistant only (doors.py).
EXPOSE 80 11434 10300 10200
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["/usr/local/bin/avado-start"]
