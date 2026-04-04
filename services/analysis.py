"""LLM-powered query analysis and chat response generation."""
from __future__ import annotations

import json
import logging
import os
import re
import regex
import threading
import time
import uuid
from typing import Any

from vectorizer import (
    DEFAULT_EMBEDDING_BASE_URL,
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROVIDER,
    embed_text,
    vector_to_sql_literal,
)
from services.llm import (
    LLM_PROVIDER,
    OLLAMA_API_KEY,
    OPENROUTER_API_KEY,
    call_llm,
    extract_json_object,
)
from services.text_utils import (
    canonicalize_property_type,
    canonicalize_transaction_type,
    fold_vietnamese,
    normalize_query_terms,
    result_haystack,
)

logger = logging.getLogger("bds-api.analysis")

# ── Conversation state management ─────────────────────────────────────────────

_CONV_LOCK = threading.Lock()
_CONV_STATES: dict[str, dict[str, Any]] = {}  # session_id → state dict
_CONV_EXPIRE_SECONDS = 1800  # 30 minutes inactivity


def _clean_expired() -> None:
    """Remove expired conversation states."""
    now = time.time()
    expired = [sid for sid, s in _CONV_STATES.items() if now - s.get("_last_active", 0) > _CONV_EXPIRE_SECONDS]
    for sid in expired:
        del _CONV_STATES[sid]


def get_conversation_state(session_id: str) -> dict[str, Any]:
    """Get (or init) conversation state for a session."""
    with _CONV_LOCK:
        _clean_expired()
        if session_id not in _CONV_STATES:
            _CONV_STATES[session_id] = {
                "_session_id": session_id,
                "_created_at": time.time(),
                "_last_active": time.time(),
                "collected_phone": None,
                "collected_name": None,
                "intent_identified": None,  # 'ban' | 'mua' | 'thue' | 'hỏi' | None
                "property_type_wanted": None,
                "location_wanted": None,
                "budget_wanted": None,
                "pending_info_request": None,  # what info we still need
                "info_collection_complete": False,
                "last_matches_shown": [],
                "greeting_done": False,
            }
        state = _CONV_STATES[session_id]
        state["_last_active"] = time.time()
        return dict(state)  # return copy


def update_conversation_state(session_id: str, user_message: str, bot_response: str) -> dict[str, Any]:
    """Update conversation state after a turn."""
    with _CONV_LOCK:
        state = _CONV_STATES.get(session_id, {})
        state["_last_active"] = time.time()

        # Record the turn
        if "history" not in state:
            state["history"] = []
        state["history"].append({"role": "user", "content": user_message, "ts": time.time()})
        state["history"].append({"role": "assistant", "content": bot_response, "ts": time.time()})

        # Track greeting
        if not state.get("greeting_done") and any(
            w in user_message.lower() for w in ["xin chào", "chào", "hello", "hi", "nhờ", "hỏi"]
        ):
            state["greeting_done"] = True

        return dict(state)


def clear_conversation_state(session_id: str) -> None:
    """Delete a conversation state."""
    with _CONV_LOCK:
        _CONV_STATES.pop(session_id, None)


def process_conversation_turn(session_id: str, user_message: str, bot_response: str) -> dict[str, Any]:
    """
    Extract structured info from user message and update conversation state.

    Called after each chat turn to keep state up-to-date with:
    - Contact info (name, phone)
    - Intent (ban / mua / thue / hỏi)
    - Preferences (property type, location, budget)
    """
    import re

    text = user_message.strip()

    # --- Phone extraction ---
    phone_patterns = [
        r"(?:sdt|dt|đt|phone|mobile|mob|so dien thoai|so|dthoai)[:\s]*(\d[\d\s\-\.]{7,14}\d)",
        r"\b(0\d{9,10})\b",
        r"\b(\d{3}[.\- ]?\d{3}[.\- ]?\d{4})\b",
    ]
    found_phones = []
    for pat in phone_patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            digits = re.sub(r"\D", "", m.group(1))
            if len(digits) >= 9 and len(digits) <= 12:
                found_phones.append(digits)
    new_phone = found_phones[0] if found_phones else None

    # --- Name extraction (heuristic: capitalized words not common greeting words) ---
    greeting_words = {"xin", "chào", "chao", "hello", "hi", "vâng", "vang", "dạ", "da", "ạ", "a", "ôi", "ơi", "em", "anh", "chị", "bạn"}
    words = regex.findall(r"\b\p{Lu}\p{Ll}+\b", text)  # Title-case words
    candidate_names = [w for w in words if w.lower() not in greeting_words]
    new_name = candidate_names[0] if candidate_names else None

    # --- Intent detection ---
    text_lower = text.lower()
    BAN_SIGNALS = ["bán", "ban", "cần bán", "can ban", "đang bán", "dang ban", "bán lẻ", "bán đất", "bán nhà"]
    MUA_SIGNALS = ["mua", "cần mua", "can mua", "tìm mua", "tim mua", "đang tìm mua", "dang tim mua", "muốn mua", "muon mua", "cần tìm", "can tim"]
    THUE_SIGNALS = ["thuê", "thue", "cho thuê", "cho thue", "cần thuê", "can thue", "cho thuê", "cần thuê nhà", "cần thuê đất"]
    identified_intent = None
    if any(s in text_lower for s in BAN_SIGNALS):
        identified_intent = "ban"
    elif any(s in text_lower for s in MUA_SIGNALS):
        identified_intent = "mua"
    elif any(s in text_lower for s in THUE_SIGNALS):
        identified_intent = "thue"

    # --- Property type ---
    property_keywords = {
        "dat": ["đất", "dat", "đất nền", "dat nen", "dat o", "đất ở"],
        "nha": ["nhà", "nha", "nhà phố", "nha pho", "nhà mặt tiền", "nha mat tien", "nhà trong hẻm"],
        "can_ho": ["căn hộ", "can ho", "chung cư", "chung cu", "apartment"],
        "phong_tro": ["phòng trọ", "phong tro", "phong tro", "thuê phòng", "thue phong"],
        "biet_thu": ["biệt thự", "biet thu", "bungalow"],
        "mat_bang": ["mặt bằng", "mat bang", "mặt tiền", "mat tien", "shop", "cửa hàng"],
    }
    identified_property = None
    for ptype, keywords in property_keywords.items():
        if any(k in text_lower for k in keywords):
            identified_property = ptype
            break

    # --- Budget extraction ---
    budget_patterns = [
        r"(\d+(?:[.,]\d+)?\s*(?:từ|t|ty|ty_))",
        r"(\d+(?:[.,]\d+)?\s*triỷu|tr|tr_)",
        r"(?:từ|den|dến)\s*(\d+(?:[.,]\d+)?\s*(?:từ|t|ty|trieu|tr))",
    ]
    found_budgets = []
    for pat in budget_patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            found_budgets.append(m.group(0))
    budget = found_budgets[0] if found_budgets else None

    with _CONV_LOCK:
        state = _CONV_STATES.get(session_id, {})
        state["_last_active"] = time.time()

        # Update contact info
        if new_phone and not state.get("collected_phone"):
            state["collected_phone"] = new_phone
        if new_name and not state.get("collected_name"):
            state["collected_name"] = new_name

        # Update intent
        if identified_intent and not state.get("intent_identified"):
            state["intent_identified"] = identified_intent

        # Update preferences
        if identified_property and not state.get("property_type_wanted"):
            state["property_type_wanted"] = identified_property
        if budget and not state.get("budget_wanted"):
            state["budget_wanted"] = budget

        # Check if info collection is complete (has phone at minimum)
        state["info_collection_complete"] = bool(state.get("collected_phone"))

        # Telegram notification: first time a phone number is captured
        if new_phone and not state.get("_phone_notified"):
            state["_phone_notified"] = True
            # Snapshot state for background thread (state dict may change by the time thread runs)
            snapshot = dict(state)
            new_phone_val = new_phone

            def _notify():
                try:
                    from services.telegram import get_notifier
                    n = get_notifier()
                    if n.enabled:
                        n.notify_lead(
                            session_id=session_id,
                            name=snapshot.get("collected_name"),
                            phone=new_phone_val,
                            intent=snapshot.get("intent_identified"),
                            property_type=snapshot.get("property_type_wanted"),
                            location=snapshot.get("location_wanted"),
                            budget=snapshot.get("budget_wanted"),
                        )
                except Exception:
                    pass  # Don't let notification errors break chat
            threading.Thread(target=_notify, daemon=True).start()

        # Record turn
        if "history" not in state:
            state["history"] = []
        state["history"].append({"role": "user", "content": user_message, "ts": time.time()})
        state["history"].append({"role": "assistant", "content": bot_response, "ts": time.time()})

        # Track greeting
        if not state.get("greeting_done") and any(
            w in user_message.lower() for w in ["xin chào", "chào", "hello", "hi", "nhờ", "hỏi"]
        ):
            state["greeting_done"] = True

        return dict(state)


def list_collected_info() -> list[dict[str, Any]]:
    """Return all collected customer info from active sessions (for admin)."""
    with _CONV_LOCK:
        _clean_expired()
        result = []
        for sid, state in _CONV_STATES.items():
            if state.get("collected_phone") or state.get("collected_name"):
                result.append({
                    "session_id": sid,
                    "name": state.get("collected_name"),
                    "phone": state.get("collected_phone"),
                    "intent": state.get("intent_identified"),
                    "property_type": state.get("property_type_wanted"),
                    "location": state.get("location_wanted"),
                    "budget": state.get("budget_wanted"),
                    "last_activity": state.get("_last_active"),
                    "created_at": state.get("_created_at"),
                    "history_size": len(state.get("history") or []),
                })
        return result

SEMANTIC_MIN_SIMILARITY = float(os.getenv("SEMANTIC_MIN_SIMILARITY", "0.32"))


# ── Query filter construction ───────────────────────────────────────────────────

def fallback_query_filters(message: str) -> dict[str, Any]:
    from services.text_utils import PROPERTY_ALIASES, TRANSACTION_ALIASES, STOP_WORDS as _STOP_WORDS

    folded = fold_vietnamese((message or "").lower())
    tokens = normalize_query_terms(message)
    property_types: list[str] = []
    transaction_types: list[str] = []
    locations: list[str] = []

    for alias, canonical in PROPERTY_ALIASES.items():
        if alias in folded and canonical not in property_types:
            property_types.append(canonical)
    for alias, canonical in TRANSACTION_ALIASES.items():
        if alias in folded and canonical not in transaction_types:
            transaction_types.append(canonical)

    query_terms = [term for term in tokens if term not in _STOP_WORDS]
    if query_terms:
        current: list[str] = []
        for term in query_terms:
            if term in _STOP_WORDS:
                if len(current) >= 1:
                    locations.append(" ".join(current))
                current = []
                continue
            current.append(term)
        if len(current) >= 1:
            locations.append(" ".join(current))

    unique_locations: list[str] = []
    for location in locations:
        location = location.strip()
        if len(location) < 3:
            continue
        if location not in unique_locations:
            unique_locations.append(location)

    return {
        "property_types": property_types,
        "transaction_types": transaction_types,
        "locations": unique_locations[:3],
        "strict_location_match": bool(unique_locations),
        "must_match_terms": [term for term in query_terms if len(term) >= 3][:8],
    }


def analyze_query_with_llm(message: str, model: str | None = None) -> dict[str, Any]:
    if not OLLAMA_API_KEY() and not OPENROUTER_API_KEY():
        logger.warning("No LLM API key configured — using fallback filters.")
        return fallback_query_filters(message)

    fallback = fallback_query_filters(message)
    schema = {
        "property_types": ["dat | nha | can_ho | phong_tro | biet_thu | mat_bang | khac"],
        "transaction_types": ["ban | cho_thue | can_mua | khac"],
        "locations": ["dia danh quan trong can khop, giu nguyen cu phap tieng Viet"],
        "strict_location_match": True,
        "must_match_terms": ["cac tu khoa bat buoc nen co trong ket qua, neu co"],
    }
    messages = [
        {
            "role": "system",
            "content": (
                "Ban la bo phan hieu truy van tim kiem bat dong san. "
                "Hay tra ve DUY NHAT mot object JSON hop le, khong markdown, khong giai thich. "
                "Nhiem vu: rut ra bo loc tim kiem tu cau hoi cua nguoi dung. "
                "Neu nguoi dung noi ro dia danh cu the thi strict_location_match phai la true. "
                "Khong suy doan them dia danh khong co trong cau hoi. "
                f"Schema mong muon: {json.dumps(schema, ensure_ascii=False)}"
            ),
        },
        {"role": "user", "content": message},
    ]
    try:
        raw = call_llm(messages, model=model, timeout=60)
        parsed = extract_json_object(raw)
    except Exception as exc:
        logger.warning("LLM query analysis failed: %s — using fallback.", exc)
        return fallback

    property_types = [
        canonicalize_property_type(value)
        for value in parsed.get("property_types") or []
        if canonicalize_property_type(value)
    ]
    transaction_types = [
        canonicalize_transaction_type(value)
        for value in parsed.get("transaction_types") or []
        if canonicalize_transaction_type(value)
    ]
    locations = []
    for location in parsed.get("locations") or []:
        clean = " ".join(str(location).strip().split())
        if clean and clean not in locations:
            locations.append(clean)
    must_match_terms = normalize_query_terms(" ".join(parsed.get("must_match_terms") or []))
    return {
        "property_types": property_types or fallback.get("property_types") or [],
        "transaction_types": transaction_types or fallback.get("transaction_types") or [],
        "locations": locations or fallback.get("locations") or [],
        "strict_location_match": bool(
            parsed.get("strict_location_match")
            if parsed.get("strict_location_match") is not None
            else fallback.get("strict_location_match")
        ),
        "must_match_terms": must_match_terms or fallback.get("must_match_terms") or [],
    }


# ── Response shaping ───────────────────────────────────────────────────────────

def format_price(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f} từ"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.0f} triỷu"
    return f"{number:,.0f} đ"


def shape_analysis(analysis: dict[str, Any] | None) -> dict[str, Any] | None:
    if not analysis:
        return None
    return {
        "title": analysis.get("title") or analysis.get("summary"),
        "summary": analysis.get("summary"),
        "highlights": analysis.get("highlights") or [],
        "transaction_type": analysis.get("transaction_type"),
        "property_type": analysis.get("property_type"),
        "price_text": analysis.get("price_text") or format_price(analysis.get("price_value_vnd")),
        "price_value_vnd": analysis.get("price_value_vnd"),
        "area_text": analysis.get("area_text"),
        "area_sqm": analysis.get("area_sqm"),
        "address_text": analysis.get("address_text"),
        "phones": analysis.get("phones") or [],
        "confidence": analysis.get("confidence"),
        # Classification fields from LLM enrichment
        "is_ban": analysis.get("is_ban"),
        "is_mua": analysis.get("is_mua"),
        "is_cho_thue": analysis.get("is_cho_thue"),
        "post_intent": analysis.get("post_intent"),
    }


def build_display_preview(content_preview: str, analysis: dict[str, Any] | None) -> str:
    shaped = shape_analysis(analysis)
    if shaped and shaped.get("highlights"):
        return " · ".join(shaped["highlights"][:2])
    if shaped and shaped.get("title"):
        return shaped["title"]
    if shaped and shaped.get("summary"):
        return shaped["summary"]
    return content_preview


# ── Semantic search ───────────────────────────────────────────────────────────

def semantic_search(
    query: str,
    limit: int,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    from db import connect_db, ensure_schema, get_database_url

    filters = filters or {}
    query_embedding = embed_text(
        query,
        provider=os.getenv("EMBEDDING_PROVIDER", DEFAULT_EMBEDDING_PROVIDER),
        model=os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", str(DEFAULT_EMBEDDING_DIM))),
        base_url=os.getenv("EMBEDDING_BASE_URL", DEFAULT_EMBEDDING_BASE_URL),
    )
    vector_literal = vector_to_sql_literal(query_embedding)
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sc.id,
                       sc.canonical_post_id,
                       sc.chunk_type,
                       sc.chunk_text,
                       cp.representative_author,
                       cp.representative_datetime,
                       cp.primary_post_url,
                       cp.source_group_url,
                       LEFT(cp.representative_content, 500),
                       cp.representative_images,
                       latest.result_json,
                       1 - (sc.embedding <=> %s::vector) AS similarity
                FROM search_chunks sc
                JOIN canonical_posts cp ON cp.id = sc.canonical_post_id
                JOIN LATERAL (
                    SELECT result_json
                    FROM llm_post_analyses
                    WHERE canonical_post_id = cp.id
                      AND status = 'completed'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                ) latest ON TRUE
                WHERE sc.source_analysis_id IS NOT NULL
                ORDER BY sc.embedding <=> %s::vector
                LIMIT %s
                """,
                (vector_literal, vector_literal, max(limit * 8, 24)),
            )
            rows = cur.fetchall()

    items = [
        {
            "chunk_id": row[0],
            "canonical_post_id": row[1],
            "chunk_type": row[2],
            "chunk_text": row[3],
            "author": row[4],
            "datetime": row[5],
            "post_url": row[6],
            "group_url": row[7],
            "content_preview": row[8],
            "display_preview": build_display_preview(row[8], row[10]),
            "images": row[9] or [],
            "analysis": shape_analysis(row[10]),
            "similarity": float(row[11]),
        }
        for row in rows
    ]

    normalized_query = fold_vietnamese(query.lower())
    query_terms = normalize_query_terms(query)
    phrases = [
        " ".join(query_terms[i : i + 2])
        for i in range(len(query_terms) - 1)
    ]
    requested_property_types = {
        canonicalize_property_type(v)
        for v in (filters.get("property_types") or [])
        if canonicalize_property_type(v)
    }
    requested_transaction_types = {
        canonicalize_transaction_type(v)
        for v in (filters.get("transaction_types") or [])
        if canonicalize_transaction_type(v)
    }
    requested_locations = [
        fold_vietnamese(loc).lower().strip()
        for loc in (filters.get("locations") or [])
        if str(loc).strip()
    ]
    requested_terms = {
        term
        for term in normalize_query_terms(" ".join(filters.get("must_match_terms") or []))
        if len(term) >= 3
    }
    strict_location_match = bool(filters.get("strict_location_match"))

    filtered: list[dict[str, Any]] = []
    for item in items:
        haystack = result_haystack(item)
        analysis = item.get("analysis") or {}
        haystack_property = canonicalize_property_type(analysis.get("property_type"))
        haystack_transaction = canonicalize_transaction_type(analysis.get("transaction_type"))

        # Property type filter
        if requested_property_types and haystack_property not in requested_property_types:
            continue

        # Transaction type filter
        if requested_transaction_types and haystack_transaction not in requested_transaction_types:
            continue

        # Location filter — use semantic similarity as proxy
        similarity = item["similarity"]
        location_match = (
            not requested_locations
            or any(loc in haystack for loc in requested_locations)
        )
        strong_location = any(loc in haystack for loc in requested_locations) if requested_locations else False

        # Relaxed threshold: allow results if semantic sim is decent OR location/terms match
        # Remove must_match_terms from blocking logic — price info is in result_json, not haystack
        keyword_match = any(term in haystack for term in query_terms)
        if similarity >= SEMANTIC_MIN_SIMILARITY or strong_location or keyword_match:
            filtered.append(item)

    deduped: dict[int, dict[str, Any]] = {}
    for item in filtered:
        key = item["canonical_post_id"]
        existing = deduped.get(key)
        score = (
            item["similarity"],
            len(item.get("chunk_text") or ""),
            1 if item.get("chunk_type") == "detailed_listing" else 0,
        )
        existing_score = (
            (existing["similarity"], len(existing.get("chunk_text") or ""),
             1 if existing.get("chunk_type") == "detailed_listing" else 0)
            if existing
            else (-1, -1, -1)
        )
        if existing is None or score > existing_score:
            deduped[key] = item

    ranked = sorted(
        deduped.values(),
        key=lambda item: (
            1 if requested_locations and any(loc in result_haystack(item) for loc in requested_locations) else 0,
            1 if requested_property_types and canonicalize_property_type((item.get("analysis") or {}).get("property_type")) in requested_property_types else 0,
            item["similarity"],
        ),
        reverse=True,
    )
    return ranked[:limit]


# ── Chat model ─────────────────────────────────────────────────────────────────

def build_chat_context(matches: list[dict[str, Any]]) -> str:
    sections = []
    for idx, item in enumerate(matches, start=1):
        analysis = item.get("analysis") or {}
        sections.append(
            "\n".join(
                [
                    f"[Tai lieu {idx}]",
                    f"canonical_post_id: {item['canonical_post_id']}",
                    f"author: {item.get('author') or ''}",
                    f"group_url: {item.get('group_url') or ''}",
                    f"post_url: {item.get('post_url') or ''}",
                    f"similarity: {item.get('similarity'):.4f}",
                    f"chunk_type: {item.get('chunk_type')}",
                    f"chunk_text: {item.get('chunk_text')}",
                    f"analysis_json: {json.dumps(analysis, ensure_ascii=False)}",
                ]
            )
        )
    return "\n\n".join(sections)


def build_info_collection_section(state: dict[str, Any] | None) -> str:
    """Build the info-collection instructions for the system prompt."""
    if not state:
        return ""

    collected = []
    missing = []

    if state.get("collected_name"):
        collected.append(f"- Tên: {state['collected_name']}")
    else:
        missing.append("tên")

    if state.get("collected_phone"):
        collected.append(f"- SĐT: {state['collected_phone']}")
    else:
        missing.append("số điện thoại")

    if state.get("intent_identified"):
        collected.append(f"- Mục đích: {state['intent_identified']}")
    if state.get("property_type_wanted"):
        collected.append(f"- Loại BDS: {state['property_type_wanted']}")
    if state.get("location_wanted"):
        collected.append(f"- Khu vực: {state['location_wanted']}")
    if state.get("budget_wanted"):
        collected.append(f"- Ngân sách: {state['budget_wanted']}")

    parts = []
    if collected:
        parts.append("Thông tin đã thu thập được:\n" + "\n".join(collected))
    if missing:
        parts.append(f"Cần thu thập thêm: {', '.join(missing)}")

    return "\n\n".join(parts)


def call_chat_model(
    message: str,
    matches: list[dict[str, Any]],
    model: str | None = None,
    query_filters: dict[str, Any] | None = None,
    session_id: str | None = None,
    conversation_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from fastapi import HTTPException

    if not OLLAMA_API_KEY() and not OPENROUTER_API_KEY():
        raise HTTPException(status_code=500, detail="No LLM provider configured (need OLLAMA_API_KEY or OPENROUTER_API_KEY).")

    context = build_chat_context(matches)
    info_section = build_info_collection_section(conversation_state)

    # Conversation-aware system prompt
    system_prompt = (
        "Ban la mot chuyen gia tu van bat dong san tai Thanh Hoa, than thien va nhiet tinh. "
        "Ban co quyen su dung du lieu thuc te tu he thong de tra loi cac cau hoi cua nguoi dung. "
        "Hay tra loi TU NHIEN, than thien, nhu dang chat voi mot nguoi ban. "
        "Tra loi NGAN GON: 2-3 cau, neu co tin phu hop thi goi y that thu. "
        "KHONG DUNG: **bold**, ## headers, markdown, so thu tu, bullet list, emoji nhieu, goi y dai. "
        "Neu thong tin trong context khong du, hay noi that rang hien chua co du lieu phu hop va goi y nguoi dung thu tu khoa khac. "
        "Neu cau hoi chung chung (vi du: 'toi co 2 ty'), hay dua ra nhan xet ngan gon dua tren cac tin dang hien co. "
        "Ban chi su dung du lieu trong context, khong biet gi ngoai no.\n\n"
        "QUY TAC QUAN TRONG - THU THAP THONG TIN KHACH HANG:\n"
        "- Khi nguoi dung hoi ve bat dong san (mua, thue, ban), hay thu thap:\n"
        "  1. Ten nguoi lien he (neu chua co)\n"
        "  2. So dien thoai de quan tri vien co the lien lac (bat buoc - rat quan trong)\n"
        "  3. Loai bat dong san muon mua/thue/ban\n"
        "  4. Khu vuc quan tam\n"
        "  5. Ngan sach / gia muon ban (neu co)\n"
        "- Khi da co so dien thoai, vui long:\n"
        '  - Tra loi: "Cam on anh/chj {ten}. So dien thoai {sdt} da duoc ghi nhan. '
        'Quan tri vien se lien lac trong thoi gian som nhat!"\n'
        "  - KHONG yeu cau them thong tin nao nua\n"
        "- Neu nguoi dung chi hoi chung (gia dat, cach tinh...), chi can tra loi truc tiep, khong can thu thap thong tin\n"
        "- Cuoi moi cau tra loi, neu chua co SDT, hay noi ngan: "
        '"De quan tri vien ho tro tot hon, neu anh/chj co the chia se so dien thoai de lien lac thi tot qua a."'
    )
    if info_section:
        system_prompt += f"\n\n{info_section}"

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Cau hoi nguoi dung: {message}\n"
                f"(Bo loc chi mang tinh tham khao, khong bat buoc dung — hay dua vao noi dung thuc te cua context)\n\n"
                f"--- Du lieu tim duoc ---\n{context}"
            ),
        },
    ]
    try:
        # Always use primary provider (ollama_cloud) for chatbot
        answer = call_llm(messages=messages, model=model, timeout=120, task="chat")
    except RuntimeError as exc:
        logger.error("Chat model failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))

    return {
        "answer": answer,
        "matches": matches,
        "model": model or LLM_PROVIDER(),
    }
