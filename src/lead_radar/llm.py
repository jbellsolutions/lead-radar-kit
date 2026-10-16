"""Minimal OpenRouter chat client for the three model call sites (expander, Stage 2, openers).

Model names come only from config (models.*). Token usage is returned for run metering.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from .config import Settings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class ModelNotConfigured(RuntimeError):
    pass


@dataclass
class Completion:
    text: str
    model: str
    tokens_in: int
    tokens_out: int


def model_for(settings: Settings, role: str) -> str:
    name = settings.get(f"models.{role}")
    if not name:
        raise ModelNotConfigured(f"models.{role} is not set in config/radar.yaml")
    return name


async def chat(settings: Settings, role: str, system: str, user: str, *, json_mode: bool = True,
               max_tokens: int = 2000, transport: httpx.AsyncBaseTransport | None = None,
               model: str | None = None) -> Completion:
    model = model or model_for(settings, role)
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {settings.require('OPENROUTER_API_KEY')}", "X-Title": "lead-radar"}
    async with httpx.AsyncClient(timeout=120, transport=transport) as http:
        resp = await http.post(OPENROUTER_URL, json=body, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"openrouter {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    usage = data.get("usage") or {}
    return Completion(text=data["choices"][0]["message"]["content"] or "", model=data.get("model", model),
                      tokens_in=int(usage.get("prompt_tokens", 0)), tokens_out=int(usage.get("completion_tokens", 0)))


def parse_json(text: str) -> dict:
    """Parse a JSON object, tolerating ```json fences."""
    t = text.strip()
    if m := re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S):
        t = m.group(1)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in model output")
    return json.loads(t[start:end + 1])
