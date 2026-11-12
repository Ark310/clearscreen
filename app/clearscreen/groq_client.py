# -*- coding: utf-8 -*-
"""Groq Cloud as the AI review's model (POST /match/main and /match/test): qwen/qwen3.8-27b, reasoning high,
strict JSON schema. Returns (answer JSON, info) like app/ai_review.ollama_chat.

Limits: a short per-minute limit (Groq asks to retry within a minute) is waited out; a longer
one - the free tier's daily allowance - raises GroqLimitReached and the request fails with
AI_LIMIT_REACHED (clients are never told the provider's name). There is no fallback to the local model.

The API key comes from the encrypted secrets store (clearscreen/config.py). It is never
logged or returned.
"""
import copy
import time
from dataclasses import dataclass

import httpx

from clearscreen.ai_review import AIProviderLimit, AIReview


class GroqLimitReached(AIProviderLimit):
    """Groq refused for a usage limit that will not clear within a minute."""


class GroqNotConfigured(RuntimeError):
    """No Groq API key is set on this server."""


@dataclass
class GroqConfig:
    model: str = "qwen/qwen3.8-27b"
    reasoning_effort: str = "high"
    price_in: float = 0.80               # $ per 1M input tokens (console.groq.com/docs/models, 2026-10)
    price_out: float = 4.00              # $ per 1M output tokens
    url: str = "https://api.groq.com/openai/v1/chat/completions"
    timeout_s: float = 120.0
    max_wait_s: float = 60.0             # longest per-minute limit worth waiting out
    max_attempts: int = 6


def groq_key(store) -> str:
    """The Groq API key from the encrypted secrets store (set in setup or the Admin app)."""
    key = store.get("groq_api_key")
    if not key:
        raise GroqNotConfigured("No Groq API key is set. Add it in the ClearScreen Admin app (Settings).")
    return key


def strict_schema(schema: dict) -> dict:
    """A JSON schema in Groq's strict form: every property required, objects closed, no defaults."""
    s = copy.deepcopy(schema)

    def fix(node):
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("title", None)
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for v in node.values():
                fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)
    fix(s)
    return s


def groq_chat(store, cfg: GroqConfig | None = None, post=None):
    """chat(system, user) -> (answer JSON, info) through Groq. `post` replaces httpx.post in tests."""
    cfg = cfg or GroqConfig()
    key = groq_key(store)
    schema = strict_schema(AIReview.model_json_schema())
    post = post or httpx.post

    def chat(system, user):
        waited, waits = 0.0, []
        for _ in range(cfg.max_attempts):
            started = time.time()
            r = post(cfg.url, timeout=cfg.timeout_s, headers={"Authorization": f"Bearer {key}"},
                     json={"model": cfg.model, "temperature": 0, "reasoning_effort": cfg.reasoning_effort,
                           "reasoning_format": "parsed",
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                           "response_format": {"type": "json_schema", "json_schema": {
                               "name": "ai_review", "strict": True, "schema": schema}}})
            wall = time.time() - started
            if r.status_code != 429:
                break
            wait = float(r.headers.get("retry-after") or 20)
            if wait > cfg.max_wait_s or "per day" in r.text.lower():
                raise GroqLimitReached(f"Groq limit reached (Groq says retry in {wait / 60:.0f} min)")
            slept = time.time()
            time.sleep(wait + 1)
            waits.append((slept, time.time()))
            waited += wait + 1
        else:
            raise GroqLimitReached("Groq limit reached (still limited after waiting)")
        if r.status_code != 200:
            raise RuntimeError(f"Groq {r.status_code}: {r.text[:300]}")
        data = r.json()
        if data["choices"][0].get("finish_reason") == "length":
            raise RuntimeError("Groq answer cut off at the output limit")
        u = data.get("usage") or {}
        return data["choices"][0]["message"]["content"], dict(
            model=cfg.model, total_s=round(wall, 2), waited_s=round(waited, 1), waits=waits,
            prompt_tokens=u.get("prompt_tokens") or 0, output_tokens=u.get("completion_tokens") or 0,
            cost_usd=((u.get("prompt_tokens") or 0) * cfg.price_in + (u.get("completion_tokens") or 0) * cfg.price_out) / 1e6)
    return chat
