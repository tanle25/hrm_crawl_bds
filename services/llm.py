"""
Unified LLM client supporting multiple providers.

Providers (set via LLM_PROVIDER env):
  - ollama_cloud  — Ollama Cloud API  (default)
  - openrouter    — OpenRouter proxy  (fallback)

Usage:
    from services.llm import call_llm
    result = call_llm(messages)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("bds-api.llm")


def _env(path: Path | None = None) -> None:
    """Reload .env into os.environ. Safe to call multiple times."""
    from dotenv import load_dotenv
    p = path or (Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(p, override=True)


# ── Config helpers (always read fresh from os.environ) ───────────────────────

def LLM_PROVIDER() -> str:
    """Current LLM provider: 'ollama_cloud' or 'openrouter'."""
    return os.getenv("LLM_PROVIDER", "ollama_cloud").lower()


def OLLAMA_API_KEY() -> str:
    return os.getenv("OLLAMA_API_KEY", "")


def OLLAMA_BASE_URL() -> str:
    return os.getenv("OLLAMA_BASE_URL", "https://cloud.ollama.com").rstrip("/")


def OLLAMA_MODEL() -> str:
    return os.getenv("OLLAMA_MODEL", "gpt-oss:120b-cloud")


def OPENROUTER_API_KEY() -> str:
    return os.getenv("OPENROUTER_API_KEY", "")


def OPENROUTER_BASE_URL() -> str:
    return os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")


def OPENROUTER_MODEL() -> str:
    return os.getenv("OPENROUTER_MODEL", "qwen/qwen3.6-plus:free")


def ENRICHER_MODEL() -> str:
    """Model used specifically for background data enrichment tasks (free tier)."""
    return os.getenv("ENRICHER_MODEL", "qwen/qwen3.6-plus:free")


def CHAT_MODEL() -> str:
    """Model used specifically for the chatbot conversation (gpt-oss)."""
    return os.getenv("CHAT_MODEL", "gpt-oss:120b-cloud")


# ── Provider implementations ─────────────────────────────────────────────────

def _call_ollama_cloud(
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    timeout: int,
) -> str:
    """
    Call Ollama Cloud API via requests.
    Routes to the correct endpoint:
      - localhost → local Ollama (ollama binary)
      - openrouter → OpenRouter /v1/chat/completions (needs OPENROUTER_API_KEY)
      - cloud.ollama.com → Ollama Cloud /api/chat (needs OLLAMA_API_KEY)
    """
    base_url = OLLAMA_BASE_URL().rstrip("/")

    # Determine which key and endpoint to use
    if "openrouter" in base_url:
        # Use OpenRouter-compatible endpoint
        endpoint = f"{base_url}/chat/completions"
        api_key = OPENROUTER_API_KEY()
    elif "localhost" in base_url or "127.0.0.1" in base_url:
        endpoint = f"{base_url}/api/chat"
        api_key = OLLAMA_API_KEY()
    else:
        # cloud.ollama.com or other
        endpoint = f"{base_url}/api/chat"
        api_key = OLLAMA_API_KEY()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }

    max_retries, delay = 3, 2.0
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
            if not resp.ok:
                raise requests.HTTPError(
                    f"{resp.status_code} Client Error: {resp.text[:500]}",
                    response=resp,
                )
            return resp.json()["choices"][0]["message"]["content"]
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < max_retries:
                logger.warning(
                    "[ollama_cloud] Attempt %s/%s failed: %s — retry in %ss",
                    attempt, max_retries, exc, delay,
                )
                time.sleep(delay)
                delay *= 2
            else:
                logger.error("[ollama_cloud] All attempts failed: %s", exc)
    raise last_exc or RuntimeError("_call_ollama_cloud: no exception")


def _call_openrouter(
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    timeout: int,
) -> str:
    headers: dict[str, str] = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY()}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }

    max_retries, delay = 3, 2.0
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                f"{OPENROUTER_BASE_URL()}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            if not resp.ok:
                raise requests.HTTPError(
                    f"{resp.status_code} Client Error: {resp.text[:500]}",
                    response=resp,
                )
            return resp.json()["choices"][0]["message"]["content"]
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < max_retries:
                logger.warning(
                    "[openrouter] Attempt %s/%s failed: %s — retry in %ss",
                    attempt, max_retries, exc, delay,
                )
                time.sleep(delay)
                delay *= 2
            else:
                logger.error("[openrouter] All attempts failed: %s", exc)
    raise last_exc or RuntimeError("_call_openrouter: no exception")


# ── Public API ────────────────────────────────────────────────────────────────

def call_llm(
    messages: list[dict[str, str]],
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    timeout: int = 120,
    task: str | None = None,
) -> str:
    """
    Call an LLM with automatic cross-provider fallback.

    Tries primary provider; on persistent failure falls back to the other.

    Args:
        messages: Chat messages
        provider: Override provider ('ollama_cloud' | 'openrouter')
        model: Override model name
        temperature: Override temperature
        timeout: Request timeout in seconds
        task: 'enrich' → use ENRICHER_MODEL on openrouter (free tier)
              'chat'   → use CHAT_MODEL on ollama_cloud (primary)
              None     → use current LLM_PROVIDER default
    """
    # Resolve effective provider and model based on task
    if task == "enrich":
        # Enrichment always uses openrouter free tier
        return _call_openrouter(
            model or ENRICHER_MODEL(),
            messages,
            temperature if temperature is not None else 0.3,
            timeout,
        )
    elif task == "chat":
        # Chatbot uses LLM_PROVIDER setting with CHAT_MODEL
        effective_provider = provider or LLM_PROVIDER()
        effective_temp = temperature if temperature is not None else 0.7
        if effective_provider == "ollama_cloud":
            effective_model = model or CHAT_MODEL()
            try:
                return _call_ollama_cloud(effective_model, messages, effective_temp, timeout)
            except Exception as primary_exc:
                logger.warning("[llm] ollama_cloud failed (%s). Falling back to openrouter.", primary_exc)
                try:
                    return _call_openrouter(OPENROUTER_MODEL(), messages, effective_temp, timeout)
                except Exception:
                    raise RuntimeError(f"Both ollama_cloud and openrouter failed. Last: {primary_exc}") from primary_exc
        else:
            effective_model = model or CHAT_MODEL()  # use CHAT_MODEL on openrouter
            try:
                return _call_openrouter(effective_model, messages, effective_temp, timeout)
            except Exception as primary_exc:
                logger.warning("[llm] openrouter failed (%s). Falling back to ollama_cloud.", primary_exc)
                try:
                    return _call_ollama_cloud(OLLAMA_MODEL(), messages, effective_temp, timeout)
                except Exception:
                    raise RuntimeError(f"Both openrouter and ollama_cloud failed. Last: {primary_exc}") from primary_exc
    else:
        # Default behavior: use current LLM_PROVIDER
        effective_provider = provider or LLM_PROVIDER()
        effective_temp = temperature if temperature is not None else 0.7

        if effective_provider == "ollama_cloud":
            effective_model = model or OLLAMA_MODEL()
            try:
                return _call_ollama_cloud(effective_model, messages, effective_temp, timeout)
            except Exception as primary_exc:
                logger.warning(
                    "[llm] ollama_cloud failed (%s). Falling back to openrouter.",
                    primary_exc,
                )
                try:
                    return _call_openrouter(OPENROUTER_MODEL(), messages, effective_temp, timeout)
                except Exception:
                    raise RuntimeError(
                        f"Both ollama_cloud and openrouter failed. Last: {primary_exc}"
                    ) from primary_exc
        else:
            effective_model = model or OPENROUTER_MODEL()
            try:
                return _call_openrouter(effective_model, messages, effective_temp, timeout)
            except Exception as primary_exc:
                logger.warning(
                    "[llm] openrouter failed (%s). Falling back to ollama_cloud.",
                    primary_exc,
                )
                try:
                    return _call_ollama_cloud(OLLAMA_MODEL(), messages, effective_temp, timeout)
                except Exception:
                    raise RuntimeError(
                        f"Both openrouter and ollama_cloud failed. Last: {primary_exc}"
                    ) from primary_exc


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract first JSON object from LLM response text."""
    text = (text or "").strip()
    try:
        return dict(json.loads(text))
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        return dict(json.loads(match.group(0)))
