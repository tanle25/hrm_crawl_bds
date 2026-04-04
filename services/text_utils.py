"""Vietnamese text normalization, query parsing, and canonicalization utilities."""
from __future__ import annotations

import re
import unicodedata
from typing import Any


# ── Vietnamese normalization ────────────────────────────────────────────────────

STOP_WORDS: set[str] = {
    "tim", "kiem", "cho", "toi", "minh", "can", "muon", "hoi", "co", "khong",
    "o", "tai", "gan", "khu", "vuc", "nha", "dat", "ban", "thue", "cho_thue",
}

PROPERTY_ALIASES: dict[str, str] = {
    "dat": "dat",
    "dat nen": "dat",
    "dat tho cu": "dat",
    "dat tho cu": "dat",
    "nha": "nha",
    "nha pho": "nha",
    "can ho": "can_ho",
    "chung cu": "can_ho",
    "phong tro": "phong_tro",
    "nha tro": "phong_tro",
    "biet thu": "biet_thu",
    "villa": "biet_thu",
    "mat bang": "mat_bang",
}

TRANSACTION_ALIASES: dict[str, str] = {
    "ban": "ban",
    "mua": "can_mua",
    "can mua": "can_mua",
    "thue": "cho_thue",
    "cho thue": "cho_thue",
    "thue nha": "cho_thue",
}


def fold_vietnamese(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text or "")
    without_marks = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return without_marks.replace("đ", "d").replace("Đ", "D")


def normalize_query_terms(text: str) -> list[str]:
    terms = re.findall(r"\w+", fold_vietnamese((text or "").lower()))
    return [term for term in terms if len(term) >= 2]


def canonicalize_property_type(value: str | None) -> str | None:
    if not value:
        return None
    folded = fold_vietnamese(value).lower().replace("_", " ").strip()
    for alias, canonical in PROPERTY_ALIASES.items():
        if alias in folded:
            return canonical
    return folded.replace(" ", "_")


def canonicalize_transaction_type(value: str | None) -> str | None:
    if not value:
        return None
    folded = fold_vietnamese(value).lower().replace("_", " ").strip()
    for alias, canonical in TRANSACTION_ALIASES.items():
        if alias in folded:
            return canonical
    return folded.replace(" ", "_")


def result_haystack(item: dict[str, Any]) -> str:
    analysis = item.get("analysis") or {}
    # Build intent label string for full-text search
    intent_parts = []
    if analysis.get("is_ban"):
        intent_parts.extend(["ban", "tin ban", "dang ban", "rao ban"])
    if analysis.get("is_mua"):
        intent_parts.extend(["mua", "tin mua", "can mua", "tim mua"])
    if analysis.get("is_cho_thue"):
        intent_parts.extend(["thue", "tin thue", "cho thue", "can thue"])
    if analysis.get("post_intent"):
        intent_parts.append(analysis["post_intent"])
    intent_str = " ".join(intent_parts)
    parts = [
        item.get("author") or "",
        item.get("chunk_text") or "",
        item.get("content_preview") or "",
        analysis.get("summary") or "",
        analysis.get("address_text") or "",
        analysis.get("price_text") or "",
        analysis.get("area_text") or "",
        analysis.get("property_type") or "",
        analysis.get("transaction_type") or "",
        intent_str,
        " ".join(analysis.get("phones") or []),
        item.get("group_url") or "",
    ]
    return fold_vietnamese(" ".join(parts).lower())
