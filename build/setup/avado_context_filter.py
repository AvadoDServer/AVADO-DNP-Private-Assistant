"""
title: About this AVADO
author: AVADO
version: 0.1.0
description: Gives the assistant a short, read-only summary of this box (apps, memory, disk, sync, validators) and the AVADO safety rules. Also stops a chat from loading the model while the Ethereum node needs the memory.
"""

# Installed and switched on for every model by the AVADO setup helper (build/setup/avado_setup.py).
# The facts come from the helper on 127.0.0.1:8090 inside this container (never from the network);
# it rebuilds them at most once a minute, with the stable facts first so the model can reuse what
# it has already read.

import asyncio
import json
import os
import urllib.request

HELPER = os.environ.get("AVADO_HELPER_URL", "http://127.0.0.1:8090")
TOKEN_FILE = os.path.join(os.environ.get("AVADO_STATE_DIR", "/app/backend/data/avado"), "internal-token")

FALLBACK = (
    "You are the Private Assistant on this AVADO. Answer in the language the user writes in, in plain words. "
    "Facts about this box are not available right now: for questions about the box, say you don't know. "
    "Never ask for or accept a seed phrase, keystore or password. "
    "For exits, withdrawals or money questions, point to docs.ava.do and support@ava.do."
)


def _ask(model):
    try:
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
    except OSError:
        return None
    req = urllib.request.Request(HELPER + "/internal/context?model=" + urllib.request.quote(model or ""),
                                 headers={"X-Avado-Internal": token})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception:
        return None


class Filter:
    def __init__(self):
        self.priority = 0

    async def inlet(self, body: dict, __user__: dict = None) -> dict:
        info = await asyncio.to_thread(_ask, body.get("model"))
        if info and info.get("paused"):
            # Open WebUI shows this text in the chat instead of an answer
            raise Exception(info.get("message") or "Paused so your Ethereum node has enough memory.")
        text = (info or {}).get("system") or FALLBACK
        messages = body.get("messages") or []
        if messages and messages[0].get("role") == "system" and isinstance(messages[0].get("content"), str):
            if not messages[0]["content"].startswith(text[:80]):
                messages[0] = dict(messages[0], content=text + "\n\n" + messages[0]["content"])
        else:
            messages.insert(0, {"role": "system", "content": text})
        body["messages"] = messages
        return body
