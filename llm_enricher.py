import argparse
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

from build_embeddings import rebuild_embeddings_for_post
from db import claim_enrichment_job, connect_db, ensure_schema, get_database_url, save_enrichment_result
from services.llm import (
    LLM_PROVIDER,
    OLLAMA_API_KEY,
    OLLAMA_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    call_llm,
    extract_json_object,
)
from vectorizer import (
    DEFAULT_EMBEDDING_BASE_URL,
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROVIDER,
)


ROOT_DIR = Path(__file__).resolve().parent
PROMPT_VERSION = "v2"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze crawled posts with LLM and store enrichments without overwriting raw data.")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=int, default=15)
    return parser


def build_embedding_settings() -> SimpleNamespace:
    return SimpleNamespace(
        provider=os.getenv("EMBEDDING_PROVIDER", DEFAULT_EMBEDDING_PROVIDER),
        model=os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", str(DEFAULT_EMBEDDING_DIM))),
        base_url=os.getenv("EMBEDDING_BASE_URL", DEFAULT_EMBEDDING_BASE_URL),
    )


def build_enrichment_messages(job: dict) -> list[dict[str, str]]:
    schema = {
        "is_bds_post": True,
        "title": "short catchy headline (max 15 words), e.g. 'Dat 90m2 mat tien duong 24m, gan cho Dong Ve, Thanh Hoa'",
        "post_intent": "ban | cho_thue | can_mua | cau_hoi | khac",
        "is_ban": True,
        "is_mua": False,
        "is_cho_thue": False,
        "property_type": "dat | nha | can_ho | phong_tro | biet_thu | mat_bang | du_an | khac",
        "price_text": "clean price string, e.g. '8.2 ty', '1.5 ty/can/nam', '5 trieu/thang'",
        "price_value_vnd": "number or null",
        "price_per_sqm_text": "price per m2, e.g. '25 trieu/m2' or null",
        "area_text": "clean area string, e.g. '90m2 (5x18m)'",
        "area_sqm": "number or null",
        "address_text": "clean short address, e.g. 'Duong 24m SunSport, gan KS Muong Thanh, TP Thanh Hoa'",
        "district": "quan/huyen cu the, e.g. 'TP Thanh Hoa', 'Tho Xuan' or null",
        "province": "'Thanh Hoa' or null",
        "phones": ["list of phone strings"],
        "contact_name": "name of contact person or null",
        "summary": "2-3 sentence summary in plain Vietnamese, no bullet points, no markdown",
        "highlights": ["3 short bullet points about key advantages, max 20 words each"],
        "confidence": 0.0,
        "legal_status": "so_hong | giay_phep | chua_xac_dinh | khong_xac_dinh | null",
        "facing_direction": "dong | nam | tay | bac | dong_nam | dong_bac | nam_bac | nam_tay | bac_tay | khong_xac_dinh | null",
        "has_road_access": True,
        "road_width_text": "road width if mentioned, e.g. '4m', '6m', '12m' or null",
    }
    user_payload = {
        "canonical_post_id": job["canonical_post_id"],
        "author": job.get("author"),
        "datetime": job.get("datetime"),
        "post_url": job.get("primary_post_url"),
        "content": job.get("content"),
        "images": job.get("images", []),
    }
    return [
        {
            "role": "system",
            "content": (
                "Ban la bo phan phan tich va phan loai bai dang bat dong san tai Thanh Hoa. "
                "Tra ve DUY NHAT mot object JSON hop le, khong markdown, khong giai thich.\n\n"
                "QUY TAC PHAN LOAI:\n"
                "1. Tin BAN (is_ban=True): nguoi dang RAO BAN / CAN BAN / BAN / MO BAN bat dong san (dat, nha, can ho, mat bang...)\n"
                "   Chi can nguoi dang CO Y DINH BAN la is_ban=True, KE CA khi trong cau goc co:\n"
                "   - 'can ban', 'can tien ban gap', 'can ra nhanh lo dat', 'ban gap', 'ban lo'\n"
                "   - 'mo ban', 'chinh chu ban', 'rao ban', 'ban nha', 'ban dat'\n"
                "2. Tin MUA (is_mua=True): nguoi dang TIM MUA / CAN MUA / MUON MUA bat dong san\n"
                "3. Tin THUE (is_cho_thue=True): nguoi dang CHO THUE / CAN THUE bat dong san\n"
                "4. Cau hoi / khong xac dinh: chi hoi gia, hoi cach, khong co noi dung mua/ban/thue cu the\n\n"
                "VI DU PHAN LOAI:\n"
                "- 'ban dat 200m2 gia 3 ty' → is_ban=True (dang ban)\n"
                "- 'can ban nha mat tien duong 24m' → is_ban=True\n"
                "- 'can tien ban gap lo dat' → is_ban=True (can tien = can ban)\n"
                "- 'can ra nhanh lo dat hud4' → is_ban=True\n"
                "- 'rao ban nha cap 4' → is_ban=True\n"
                "- 'ban gap can ho chung cu' → is_ban=True\n"
                "- 'mo ban toa A3 cuoi cung' → is_ban=True (mo ban = dang ban)\n"
                "- 'chinh chu ban nha' → is_ban=True\n"
                "- 'tim mua dat 100m2 TP Thanh Hoa' → is_mua=True\n"
                "- 'can mua nha quan 3' → is_mua=True\n"
                "- 'cho thue phong tro 2 trieu/thang' → is_cho_thue=True\n"
                "- 'cho thue mat bang 50m2' → is_cho_thue=True\n"
                "- 'gia dat Thanh Hoa bao nhieu' → cau hoi (is_ban=False, is_mua=False, is_cho_thue=False)\n\n"
                "LUON tra ve day du cac truong, neu khong co thong tin thi null hoac [].\n"
                "Schema: " + json.dumps(schema, ensure_ascii=False)
            ),
        },
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False),
        },
    ]


def process_once(args: argparse.Namespace | None = None) -> bool:
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / ".env", override=True)

    from services.llm import LLM_PROVIDER, OLLAMA_MODEL, OPENROUTER_MODEL

    embedding_settings = build_embedding_settings()
    db_url = getattr(args, "database_url", None) if args else None
    with connect_db(get_database_url(db_url)) as conn:
        ensure_schema(conn)
        job = claim_enrichment_job(conn)
        if not job:
            return False
        post_id = job.get("canonical_post_id")
        logging.info("[enricher] Claimed job post_id=%s attempt=%s", post_id, job.get("attempts"))
        raw_response = ""
        result_json = None
        error_message = None
        start_time = time.monotonic()
        try:
            # Use task="enrich" which uses the free OpenRouter model
            raw_response = call_llm(build_enrichment_messages(job), task="enrich")
            result_json = extract_json_object(raw_response)
            elapsed = time.monotonic() - start_time
            logging.info(
                "[enricher] LLM call succeeded post_id=%s elapsed=%.1fs",
                post_id, elapsed,
            )
        except Exception as exc:
            error_message = str(exc)
            logging.warning("[enricher] LLM call failed post_id=%s error=%s", post_id, exc)
        actual_provider = LLM_PROVIDER()
        actual_model = OLLAMA_MODEL() if actual_provider == "ollama_cloud" else OPENROUTER_MODEL()
        save_enrichment_result(
            conn,
            job=job,
            provider=actual_provider,
            model=actual_model,
            prompt_version=PROMPT_VERSION,
            input_content=job.get("content") or "",
            raw_response=raw_response,
            result_json=result_json,
            error_message=error_message,
        )
        if error_message is None and result_json is not None:
            rebuild_embeddings_for_post(
                conn,
                canonical_post_id=job["canonical_post_id"],
                provider=embedding_settings.provider,
                model=embedding_settings.model,
                dimensions=embedding_settings.dimensions,
                base_url=embedding_settings.base_url,
            )
            conn.commit()
            logging.info("[enricher] Embeddings rebuilt post_id=%s", post_id)
        return True


def main() -> int:
    load_dotenv(ROOT_DIR / ".env")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    if not OLLAMA_API_KEY() and not OPENROUTER_API_KEY():
        logging.critical("[enricher] Neither OLLAMA_API_KEY nor OPENROUTER_API_KEY is set.")
        raise SystemExit(1)
    logging.info("[enricher] Starting -- provider=%s", LLM_PROVIDER())
    args = build_parser().parse_args()
    if args.once:
        process_once(args)
        return 0
    while True:
        handled = process_once(args)
        if not handled:
            time.sleep(args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())
