import hashlib
import logging
import math
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests


DEFAULT_EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "huggingface_local")
DEFAULT_EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "AITeamVN/Vietnamese_Embedding")
DEFAULT_EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))
DEFAULT_EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
DEFAULT_EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENROUTER_API_KEY")


def normalize_embedding_text(text: str) -> str:
    normalized = text.strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def vector_to_sql_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


def _normalize_vector(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        return values
    return [value / norm for value in values]


def local_hash_embedding(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    tokens = re.findall(r"\w+", normalize_embedding_text(text))
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
        index = int.from_bytes(digest[:8], "big") % dimensions
        sign = 1.0 if digest[8] % 2 == 0 else -1.0
        weight = 1.0 + (digest[9] / 255.0)
        vector[index] += sign * weight
    return _normalize_vector(vector)


def openai_compatible_embedding(
    text: str,
    model: str,
    dimensions: int,
    base_url: str,
    api_key: str,
) -> list[float]:
    payload: dict[str, Any] = {
        "model": model,
        "input": text,
    }
    if dimensions:
        payload["dimensions"] = dimensions
    max_retries = 3
    retry_delay = 2.0
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                f"{base_url.rstrip('/')}/embeddings",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,
            )
            if not response.ok:
                raise requests.HTTPError(
                    f"{response.status_code} Client Error: {response.text[:500]}",
                    response=response,
                )
            data = response.json()["data"][0]["embedding"]
            if dimensions and len(data) != dimensions:
                raise ValueError(f"Embedding dimensions mismatch: expected {dimensions}, got {len(data)}")
            return [float(item) for item in data]
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < max_retries:
                logging.warning(
                    "[vectorizer] Attempt %s/%s failed (embedding): %s — retrying in %ss",
                    attempt, max_retries, exc, retry_delay
                )
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                logging.error("[vectorizer] All %s embedding attempts failed: %s", max_retries, exc)
    raise last_exc or RuntimeError("openai_compatible_embedding failed with no exception")


# Known-good SHA256 hashes for approved model configs.
# Format: model_name -> sha256 of its config.json (fetched from HuggingFace Hub).
# Add entries here when you first trust a new model, after verifying its config.
# Set env  VERIFY_MODEL_HASH=0  to disable (not recommended).
MODEL_CONFIG_HASHES: dict[str, str] = {
    # SHA256 of config.json fetched from HuggingFace Hub — verified 2026-04-02
    "AITeamVN/Vietnamese_Embedding": "d2cbae385b5acc4cbd816fdca3b624849212d8bf184d59731251c7314789af90",
}

_ALLOW_UNVERIFIED = os.getenv("VERIFY_MODEL_HASH", "1") != "0"


def _load_sentence_transformer(model: str):
    from sentence_transformers import SentenceTransformer
    import hashlib

    # Prevent MPS OOM — fall back to CPU if MPS is tight
    import os as _os
    if not _os.environ.get("PYTORCH_MPS_HIGH_WATERMARK_RATIO"):
        _os.environ["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] = "0.0"

    if _ALLOW_UNVERIFIED and model in MODEL_CONFIG_HASHES:
        expected_hash = MODEL_CONFIG_HASHES[model]
        try:
            from huggingface_hub import hf_hub_download
            config_path = hf_hub_download(
                repo_id=model, filename="config.json", local_files_only=False
            )
            actual_hash = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                raise SecurityError(
                    f"Model config hash mismatch for '{model}': "
                    f"expected {expected_hash}, got {actual_hash}. "
                    f"Model may have been tampered with. Aborting."
                )
            logging.info("[vectorizer] Model config hash verified for %s", model)
        except SecurityError:
            raise
        except Exception as exc:
            logging.warning(
                "[vectorizer] Could not verify model hash for '%s': %s — proceeding anyway",
                model, exc,
            )

    return SentenceTransformer(model, device="cpu")


class SecurityError(Exception):
    """Raised when a model config integrity check fails."""


def huggingface_local_embedding(text: str, model: str, dimensions: int) -> list[float]:
    return huggingface_local_batch_embedding([text], model, dimensions)[0]


def huggingface_local_batch_embedding(texts: list[str], model: str, dimensions: int) -> list[list[float]]:
    """Batch embedding — much faster than per-item calls."""
    encoder = _load_sentence_transformer(model)
    vectors = encoder.encode(texts, batch_size=8, normalize_embeddings=True, show_progress_bar=False)
    results: list[list[float]] = []
    for vector in vectors:
        values = [float(item) for item in vector.tolist()]
        if dimensions and len(values) != dimensions:
            raise ValueError(f"Embedding dimensions mismatch: expected {dimensions}, got {len(values)}")
        results.append(values)
    return results


def embed_text(
    text: str,
    provider: str = DEFAULT_EMBEDDING_PROVIDER,
    model: str = DEFAULT_EMBEDDING_MODEL,
    dimensions: int = DEFAULT_EMBEDDING_DIM,
    base_url: str = DEFAULT_EMBEDDING_BASE_URL,
    api_key: str | None = DEFAULT_EMBEDDING_API_KEY,
) -> list[float]:
    if provider == "local_hash":
        return local_hash_embedding(text, dimensions)
    if provider == "huggingface_local":
        return huggingface_local_embedding(text, model, dimensions)
    if provider == "openai_compatible":
        if not api_key:
            raise ValueError("Missing EMBEDDING_API_KEY for openai_compatible provider")
        return openai_compatible_embedding(text, model, dimensions, base_url, api_key)
    raise ValueError(f"Unsupported embedding provider: {provider}")


def embed_text_batch(
    texts: list[str],
    provider: str = DEFAULT_EMBEDDING_PROVIDER,
    model: str = DEFAULT_EMBEDDING_MODEL,
    dimensions: int = DEFAULT_EMBEDDING_DIM,
    base_url: str = DEFAULT_EMBEDDING_BASE_URL,
    api_key: str | None = DEFAULT_EMBEDDING_API_KEY,
) -> list[list[float]]:
    """Batch embedding — preferred over embed_text() for multiple texts."""
    if provider == "huggingface_local":
        return huggingface_local_batch_embedding(texts, model, dimensions)
    # Fallback: call one by one for other providers
    return [embed_text(t, provider, model, dimensions, base_url, api_key) for t in texts]


def chunk_hash(payload: dict[str, Any]) -> str:
    serialized = repr(sorted(payload.items()))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
