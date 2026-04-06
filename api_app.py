"""BDS Agent FastAPI application — thin entry point.

Routes are defined inline for FastAPI dependency-injection compatibility.
Business logic lives in `services/`.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, field_validator

from db import connect_db, ensure_schema, get_database_url
from llm_enricher import process_once
from services.analysis import (
    analyze_query_with_llm,
    call_chat_model,
    get_conversation_state,
    list_collected_info,
    process_conversation_turn,
    semantic_search,
)
from services.posts import fetch_post_detail, fetch_posts, fetch_stats
from services.zalo import ZaloSession, get_session

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env", override=True)  # reload fresh on every start

logger = logging.getLogger("bds-api")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

# ── Enricher lifecycle ────────────────────────────────────────────────────────

ENRICHER_STOP = threading.Event()
ENRICHER_THREAD: threading.Thread | None = None
SCHEDULER_STOP = threading.Event()
SCHEDULER_THREAD: threading.Thread | None = None


def build_enricher_args():
    class Args:
        database_url = None
        provider = "openrouter"
        model = os.getenv("OPENROUTER_PROVIDER", "qwen/qwen3.6-plus-preview:free")
        base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        api_key = os.getenv("OPENROUTER_API_KEY")
        once = False
        poll_interval = int(os.getenv("ENRICHER_POLL_INTERVAL", "15"))
    return Args()


def enrichment_worker_loop() -> None:
    from services.llm import LLM_PROVIDER, OLLAMA_API_KEY, OPENROUTER_API_KEY

    enricher_logger = logging.getLogger("bds-api.enricher")
    if not OLLAMA_API_KEY() and not OPENROUTER_API_KEY():
        enricher_logger.critical(
            "[enricher] Neither OLLAMA_API_KEY nor OPENROUTER_API_KEY is set — exiting."
        )
        return
    enricher_logger.info("[enricher] Starting — provider=%s", LLM_PROVIDER())

    while not ENRICHER_STOP.is_set():
        handled = False
        try:
            handled = process_once(None)
        except Exception:
            enricher_logger.exception("[enricher] Unexpected error — sleeping 5s")
            time.sleep(5)
        if not handled:
            ENRICHER_STOP.wait(int(os.getenv("ENRICHER_POLL_INTERVAL", "15")))
    enricher_logger.info("[enricher] Shutdown signal received — exiting.")


def _resolve_python_executable(project_root: Path) -> str:
    if sys.platform == "win32":
        venv_python = project_root / ".venv314" / "Scripts" / "python.exe"
    else:
        venv_python = project_root / ".venv314" / "bin" / "python"
    return str(venv_python) if venv_python.exists() else sys.executable


def _resolve_group_urls(group_ids: list[int]) -> list[str]:
    if not group_ids:
        return []
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT url
                FROM fb_groups
                WHERE id = ANY(%s)
                ORDER BY id ASC
                """,
                (group_ids,),
            )
            return [row[0] for row in cur.fetchall()]


def _start_crawl_process(group_ids: list[int] | None = None, *, schedule_id: int | None = None) -> tuple[bool, str]:
    from db import get_crawler_settings

    project_root = Path(__file__).parent.resolve()
    python_exec = _resolve_python_executable(project_root)

    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        settings = get_crawler_settings(conn)
    workers = int(settings.get("workers", "1"))

    args = [
        python_exec,
        str(project_root / "facebook_group_scraper.py"),
        "--headless",
        "--use-db-cookies",
        "--scroll-rounds", "16",
        "--workers", str(workers),
    ]

    resolved_urls = _resolve_group_urls(group_ids or [])
    if resolved_urls:
        for url in resolved_urls:
            args += ["--group-url", url]
    else:
        args += ["--use-db-groups"]

    env = os.environ.copy()
    env["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] = "0.0"

    log_name = f"scraper_schedule_{schedule_id}.log" if schedule_id is not None else "scraper_admin.log"
    log_path = Path(tempfile.gettempdir()) / log_name
    with open(log_path, "a", encoding="utf-8") as log_out:
        subprocess.Popen(args, cwd=str(project_root), env=env, stdout=log_out, stderr=subprocess.STDOUT)

    return True, f"Crawl started ({workers} worker{'s' if workers > 1 else ''}). Log: {log_path}"


def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            return True
        if part.startswith("*/"):
            step = int(part[2:])
            if step > 0 and value % step == 0:
                return True
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            if int(start) <= value <= int(end):
                return True
            continue
        if int(part) == value:
            return True
    return False


def _cron_matches(expr: str, dt: datetime) -> bool:
    minute, hour, day, month, weekday = expr.split()
    cron_weekday = (dt.weekday() + 1) % 7
    return (
        _cron_field_matches(minute, dt.minute)
        and _cron_field_matches(hour, dt.hour)
        and _cron_field_matches(day, dt.day)
        and _cron_field_matches(month, dt.month)
        and _cron_field_matches(weekday, cron_weekday)
    )


def _compute_next_run(expr: str, now: datetime) -> datetime | None:
    candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 14):
        if _cron_matches(expr, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    return None


def scheduler_worker_loop() -> None:
    scheduler_logger = logging.getLogger("bds-api.scheduler")
    scheduler_logger.info("Schedule worker started.")

    while not SCHEDULER_STOP.is_set():
        try:
            now = datetime.now(UTC).replace(second=0, microsecond=0)
            with connect_db(get_database_url(None)) as conn:
                ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, name, group_ids, cron_expr, enabled, last_run_at, next_run_at
                        FROM crawl_schedules
                        WHERE enabled = TRUE
                        ORDER BY id ASC
                        """
                    )
                    schedules = cur.fetchall()

                    for row in schedules:
                        schedule_id, name, group_ids, cron_expr, _enabled, last_run_at, next_run_at = row
                        should_run = False
                        if next_run_at is None:
                            next_run = _compute_next_run(cron_expr, now - timedelta(minutes=1))
                            cur.execute(
                                "UPDATE crawl_schedules SET next_run_at = %s, updated_at = NOW() WHERE id = %s",
                                (next_run, schedule_id),
                            )
                            next_run_at = next_run

                        if next_run_at and next_run_at <= now:
                            if not last_run_at or last_run_at.replace(second=0, microsecond=0) < next_run_at.replace(second=0, microsecond=0):
                                should_run = True

                        if should_run:
                            ok, message = _start_crawl_process(group_ids or [], schedule_id=schedule_id)
                            next_run = _compute_next_run(cron_expr, now)
                            cur.execute(
                                """
                                UPDATE crawl_schedules
                                SET last_run_at = NOW(),
                                    next_run_at = %s,
                                    updated_at = NOW()
                                WHERE id = %s
                                """,
                                (next_run, schedule_id),
                            )
                            scheduler_logger.info("[schedule:%s] %s — %s", schedule_id, name, message if ok else "failed")
                conn.commit()
        except Exception:
            scheduler_logger.exception("Schedule worker error.")

        SCHEDULER_STOP.wait(30)

    scheduler_logger.info("Schedule worker stopped.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global ENRICHER_THREAD, SCHEDULER_THREAD
    auto_start = os.getenv("AUTO_START_ENRICHER", "true").lower() in {"1", "true", "yes"}
    if auto_start and ENRICHER_THREAD is None:
        ENRICHER_STOP.clear()
        ENRICHER_THREAD = threading.Thread(
            target=enrichment_worker_loop,
            name="llm-enricher",
            daemon=True,
        )
        ENRICHER_THREAD.start()
        logger.info("Enricher background thread started.")
    if SCHEDULER_THREAD is None:
        SCHEDULER_STOP.clear()
        SCHEDULER_THREAD = threading.Thread(
            target=scheduler_worker_loop,
            name="crawl-scheduler",
            daemon=True,
        )
        SCHEDULER_THREAD.start()
        logger.info("Schedule background thread started.")
    yield
    ENRICHER_STOP.set()
    SCHEDULER_STOP.set()
    logger.info("Shutdown signal sent to enricher.")


app = FastAPI(title="BDS Agent API", version="2.0.0", lifespan=lifespan)

# ── Request models ────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    limit: int = 5
    model: str | None = None
    session_id: str | None = None  # client-side session identifier


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    """Serve the dashboard HTML."""
    template_path = ROOT_DIR / "templates" / "index.html"
    if template_path.exists():
        return FileResponse(str(template_path))
    return HTMLResponse(content="<h1>BDS Agent</h1><p>templates/index.html not found.</p>", status_code=503)


@app.get("/api/health")
def health() -> dict[str, str]:
    """Basic liveness check."""
    return {"status": "ok"}


@app.get("/metrics")
def metrics_endpoint() -> PlainTextResponse:
    """Prometheus metrics in plain-text exposition format."""
    from services.metrics import metrics
    # Refresh gauges before exporting
    for status in ("pending", "processing", "completed", "retry"):
        metrics.refresh_gauge("bds_enricher_queue_depth", status=status)
    metrics.refresh_gauge("bds_posts_total")
    return PlainTextResponse(
        content=metrics.export(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/api/status")
def status() -> dict:
    """Enricher queue depth and processing status."""
    conn = connect_db(get_database_url(None))
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status IN ('pending', 'retry')) AS q_pending,
                    COUNT(*) FILTER (WHERE status = 'processing')          AS q_processing,
                    COUNT(*) FILTER (WHERE status = 'completed')            AS q_completed,
                    COUNT(*) FILTER (WHERE status = 'retry')                AS q_retry
                FROM llm_enrichment_queue
                """
            )
            row = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM canonical_posts")
            total_posts = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM search_chunks")
            total_chunks = cur.fetchone()[0]
    finally:
        conn.close()

    return {
        "enricher": {
            "thread_alive": ENRICHER_THREAD is not None and ENRICHER_THREAD.is_alive(),
            "poll_interval": int(os.getenv("ENRICHER_POLL_INTERVAL", "15")),
        },
        "queue": {
            "pending": row[0] or 0,
            "processing": row[1] or 0,
            "completed": row[2] or 0,
            "retry": row[3] or 0,
        },
        "database": {
            "total_posts": total_posts,
            "total_chunks": total_chunks,
        },
    }


@app.get("/api/stats")
def stats() -> dict:
    return fetch_stats()


@app.get("/api/posts")
def list_posts(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    q: str | None = None,
    type: str | None = Query(default=None, description="Filter: ban | can_mua | cho_thue"),
) -> dict:
    items, total = fetch_posts(limit=limit, offset=offset, q=q, type_filter=type)
    return {"items": items, "limit": limit, "offset": offset, "total": total}


@app.get("/api/posts/{post_id}")
def get_post(post_id: int) -> dict:
    item = fetch_post_detail(post_id)
    if not item:
        raise HTTPException(status_code=404, detail="Post not found.")
    return item


@app.get("/api/search")
def search_posts(
    q: str,
    limit: int = Query(default=5, ge=1, le=20),
) -> dict:
    filters = analyze_query_with_llm(q)
    return {"items": semantic_search(q, limit, filters=filters), "query_filters": filters}


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict:
    # Step 0: Resolve session
    session_id = request.session_id or "default"

    # Step 1: Pure semantic search — let the vector DB find relevant results
    raw_matches = semantic_search(request.message, request.limit * 4, filters=None)

    # Step 2: Get conversation state and call chat model
    state = get_conversation_state(session_id)
    payload = call_chat_model(
        request.message,
        raw_matches,
        model=request.model,
        query_filters=None,
        session_id=session_id,
        conversation_state=state,
    )

    # Step 3: Extract and persist structured info from this turn
    process_conversation_turn(session_id, request.message, payload.get("answer", ""))

    payload["matches"] = raw_matches[: request.limit]  # return only top N for UI
    payload["session_id"] = session_id
    return payload


# ── Zalo endpoints ──────────────────────────────────────────────────────────────

_zalo_session: ZaloSession | None = None


def get_zalo_session() -> ZaloSession:
    global _zalo_session
    if _zalo_session is None:
        _zalo_session = ZaloSession()
    return _zalo_session


class ZaloSendRequest(BaseModel):
    user_id: str
    message: str
    group: bool = False


class ZaloFindUserRequest(BaseModel):
    phone: str


class ZaloFriendsRequest(BaseModel):
    pass  # unused, for symmetry


class ZaloBulkRequest(BaseModel):
    messages: list[dict]
    delay: int | None = None


class ZaloForwardRequest(BaseModel):
    message: str | None = None
    user_ids: list[str]
    reference: dict[str, Any] | None = None
    batch_size: int = 100
    delay_between_batches: int = 2


class ZaloLoginRequest(BaseModel):
    cookie: str | None = None  # optional — will read from zcookies.json if not provided
    imei: str | None = None
    user_agent: str | None = None


@app.post("/api/zalo/login")
def zalo_login(req: ZaloLoginRequest) -> dict:
    """
    Login to Zalo with cookie + imei + userAgent.
    Credentials are saved to zcookies.json for reuse.
    """
    session = get_zalo_session()
    try:
        return session.login(cookie=req.cookie, imei=req.imei, user_agent=req.user_agent)
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc))


@app.get("/api/zalo/status")
def zalo_status() -> dict:
    """Check Zalo login status."""
    return get_zalo_session().status()


@app.post("/api/zalo/send")
def zalo_send(req: ZaloSendRequest) -> dict:
    """Send a single message to a Zalo user or group."""
    try:
        return get_zalo_session().send(req.user_id, req.message, group=req.group)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/zalo/find-user")
def zalo_find_user(req: ZaloFindUserRequest) -> dict:
    """Find Zalo user ID by phone number."""
    try:
        return get_zalo_session().find_user(req.phone)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/zalo/friends")
def zalo_friends() -> dict:
    """List all Zalo friends with IDs."""
    try:
        friends = get_zalo_session().list_friends()
        return {"friends": friends}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/zalo/account")
def zalo_account() -> dict:
    """Get current Zalo account info and status."""
    try:
        return get_zalo_session().account_info()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/zalo/friends/summary")
def zalo_friends_summary() -> dict:
    """Get summary of all friends — online, active, inactive counts."""
    try:
        return get_zalo_session().friends_summary()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/zalo/bulk")
def zalo_bulk(req: ZaloBulkRequest) -> dict:
    """Send multiple messages sequentially (legacy — one-by-one)."""
    try:
        return get_zalo_session().bulk_send(req.messages, delay=req.delay)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/zalo/forward")
def zalo_forward(req: ZaloForwardRequest) -> dict:
    """
    Forward a message to multiple Zalo users in batches of up to 100.
    Each batch = 1 Zalo API call. Delay between batches configurable.
    """
    try:
        return get_zalo_session().zalo_bulk_forward(
            message=req.message or "",
            user_ids=req.user_ids,
            reference=req.reference,
            batch_size=req.batch_size,
            delay_between_batches=req.delay_between_batches,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/zalo/session")
def zalo_clear_session() -> dict:
    """Delete saved Zalo session."""
    return get_zalo_session().clear()


# ── Telegram notifications ─────────────────────────────────────────────────────

class TelegramConfig(BaseModel):
    bot_token: str | None = None
    chat_id: str | None = None
    enabled: bool | None = None


class TelegramSend(BaseModel):
    message: str


@app.get("/api/admin/telegram/status")
def telegram_status() -> dict:
    """Return Telegram bot status and configuration."""
    from services.telegram import get_notifier
    n = get_notifier()
    info = n.status()
    return {
        "enabled": n.enabled,
        "has_token": bool(n.bot_token),
        "has_chat_id": bool(n.chat_id),
        **info,
    }


@app.post("/api/admin/telegram/config")
def telegram_config(body: TelegramConfig) -> dict:
    """
    Update Telegram bot configuration.
    Changes are written to .env and take effect immediately (in-process).
    For permanent effect, update .env manually or provide all values.
    """
    import re as _re
    from pathlib import Path as _Path
    from services.telegram import _notifier, get_notifier

    updates: dict[str, str] = {}
    if body.bot_token is not None:
        token = body.bot_token.strip()
        if token and not _re.match(r"^\d+:[A-Za-z0-9_-]+$", token):
            raise HTTPException(status_code=400, detail="Invalid Telegram bot token format.")
        updates["TELEGRAM_BOT_TOKEN"] = token
    if body.chat_id is not None:
        updates["TELEGRAM_CHAT_ID"] = body.chat_id.strip()
    if body.enabled is not None:
        updates["TELEGRAM_ENABLED"] = "true" if body.enabled else "false"

    # Write to .env
    env_path = _Path(__file__).parent / ".env"
    if updates and env_path.exists():
        lines = env_path.read_text().splitlines()
        keys_found = set()
        for key in updates:
            keys_found.add(key)
        new_lines: list[str] = []
        for line in lines:
            prefix = next((k + "=" for k in keys_found if line.startswith(k + "=")), None)
            if prefix:
                new_lines.append(prefix + updates[prefix])
                keys_found.discard(prefix.rstrip("="))
            else:
                new_lines.append(line)
        for k in keys_found:
            new_lines.append(k + "=" + updates[k])
        env_path.write_text("\n".join(new_lines) + "\n")

    # Update in-process singleton
    if body.bot_token is not None:
        get_notifier().bot_token = body.bot_token.strip()
    if body.chat_id is not None:
        get_notifier().chat_id = body.chat_id.strip()
    if body.enabled is not None:
        get_notifier().enabled = bool(body.enabled)

    return {"ok": True, "updated": list(updates.keys())}


@app.post("/api/admin/telegram/send")
def telegram_send(body: TelegramSend) -> dict:
    """Send a test message via Telegram."""
    from services.telegram import get_notifier
    n = get_notifier()
    if not n.bot_token or not n.chat_id:
        raise HTTPException(status_code=400, detail="Telegram not configured. Set bot token and chat ID first.")
    ok = n.send(body.message)
    if not ok:
        raise HTTPException(status_code=502, detail="Telegram send failed. Check bot token and chat ID.")
    return {"ok": True}


@app.post("/api/admin/telegram/test")
def telegram_test() -> dict:
    """Send a test message to verify Telegram setup."""
    from services.telegram import get_notifier
    n = get_notifier()
    if not n.bot_token or not n.chat_id:
        raise HTTPException(status_code=400, detail="Telegram not configured.")
    ok = n.send(
        "✅ <b>BDS Agent — Telegram OK!</b>\n\n"
        "Bot đã kết nối thành công.\n"
        "Bạn sẽ nhận thông báo khi có sự kiện quan trọng."
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Telegram test failed. Check bot token and chat ID.")
    return {"ok": True}


# ── Admin: Collected customer leads ──────────────────────────────────────────

@app.get("/api/admin/leads")
def get_leads() -> dict:
    """
    List all collected customer info from chat sessions.
    Returns leads with: name, phone, intent, property type, location, budget.
    """
    from services.analysis import list_collected_info
    return {"leads": list_collected_info(), "total": len(list_collected_info())}


@app.post("/api/admin/leads/{session_id}/notify")
def notify_lead_zalo(session_id: str, message: str | None = None) -> dict:
    """
    Send a Zalo message to a lead by session_id.
    Looks up the collected phone number from conversation state.
    """
    from services.analysis import list_collected_info
    leads = list_collected_info()
    lead = next((l for l in leads if l["session_id"] == session_id), None)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found or no phone collected.")
    if not lead.get("phone"):
        raise HTTPException(status_code=400, detail="No phone number for this lead.")
    if not message:
        # Default notification message
        message = (
            f"Xin chao {lead.get('name') or 'quy khach'},\n"
            "Cua hang bat dong san Thanh Hoa da nhan duoc yeu cau tu anh/chj.\n"
            "Chung toi se lien lac trong thoi gian som nhat!\n"
            "Cam on!"
        )
    # Find Zalo user by phone
    try:
        zalo_user = get_zalo_session().find_user(lead["phone"])
    except Exception:
        raise HTTPException(status_code=404, detail=f"Khong tim thay nguoi dung Zalo voi SDT {lead['phone']}.")
    zalo_id = zalo_user.get("zalo_id") or zalo_user.get("userId")
    if not zalo_id:
        raise HTTPException(status_code=400, detail="Zalo ID not found.")
    try:
        result = get_zalo_session().send(str(zalo_id), message)
        return {"ok": True, "result": result, "lead": lead}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/admin/leads/{session_id}/clear")
def clear_lead(session_id: str) -> dict:
    """Clear a lead from conversation state after it's been acted on."""
    from services.analysis import clear_conversation_state
    clear_conversation_state(session_id)
    return {"ok": True, "session_id": session_id}


# ── Cookie management ──────────────────────────────────────────────────────────

@app.get("/api/admin/cookies")
def list_cookies() -> dict:
    """List all Facebook cookie profiles."""
    from db import list_facebook_cookies
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        return {"cookies": list_facebook_cookies(conn)}


class CookieUpdate(BaseModel):
    name: str
    cookies_json: list[dict]

    @field_validator("cookies_json", mode="before")
    @classmethod
    def normalize_cookies_json(cls, value):
        # Accept both raw cookie arrays and common export wrappers:
        # { "cookies": [...] } or { "url": "...", "cookies": [...] }
        if isinstance(value, dict):
            cookies = value.get("cookies")
            if isinstance(cookies, list):
                return cookies
        return value


@app.post("/api/admin/cookies")
def add_cookie(body: CookieUpdate) -> dict:
    """Add or update a Facebook cookie profile."""
    from db import upsert_facebook_cookie
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        cookie_id = upsert_facebook_cookie(conn, body.name, body.cookies_json)
    return {"ok": True, "id": cookie_id, "name": body.name}


@app.post("/api/admin/cookies/{cookie_id}/validate")
def validate_cookie(cookie_id: int) -> dict:
    """Validate a cookie by checking Facebook login status via Playwright."""
    from playwright.sync_api import sync_playwright
    from db import mark_cookie_dead, mark_cookie_alive, get_cookie_by_id
    cookie_record = None
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        cookie_record = get_cookie_by_id(conn, cookie_id)
    if not cookie_record:
        raise HTTPException(status_code=404, detail="Cookie not found.")
    cookie_json = cookie_record.get("cookies_json")
    if not cookie_json:
        raise HTTPException(status_code=400, detail="No cookie data stored in this profile.")
    alive = False
    exc_msg = None
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                tempfile.gettempdir() + "/fb_validate_" + str(cookie_id),
                headless=True,
                viewport={"width": 1280, "height": 800},
            )
            ctx.add_cookies(cookie_json)
            page = ctx.new_page()
            page.goto("https://www.facebook.com", timeout=30000)
            page.wait_for_timeout(3000)
            page_url = page.url or ""
            ctx.close()
        alive = "facebook.com" in page_url and "login" not in page_url.lower()
    except Exception as exc:
        exc_msg = str(exc)
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        if alive:
            mark_cookie_alive(conn, cookie_id)
            cookie_name = cookie_record.get("name", f"id={cookie_id}")
            def _notify_alive():
                from services.telegram import get_notifier
                get_notifier().notify_cookie_alive(cookie_name)
            threading.Thread(target=_notify_alive, daemon=True).start()
        else:
            mark_cookie_dead(conn, cookie_id, exc_msg)
            cookie_name = cookie_record.get("name", f"id={cookie_id}")
            def _notify_dead():
                from services.telegram import get_notifier
                get_notifier().notify_cookie_dead(cookie_name, exc_msg)
            threading.Thread(target=_notify_dead, daemon=True).start()
    return {"ok": True, "cookie_id": cookie_id, "status": "alive" if alive else "dead", "detail": exc_msg if exc_msg else None}


@app.post("/api/admin/cookies/{cookie_id}/status")
def update_cookie_status(cookie_id: int, status: str) -> dict:
    """Manually set cookie status (alive/dead)."""
    from db import mark_cookie_alive, mark_cookie_dead
    if status not in ("alive", "dead"):
        raise HTTPException(status_code=400, detail="status must be 'alive' or 'dead'.")
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        if status == "alive":
            mark_cookie_alive(conn, cookie_id)
        else:
            mark_cookie_dead(conn, cookie_id, "Manually marked dead")
    return {"ok": True, "cookie_id": cookie_id, "status": status}


@app.delete("/api/admin/cookies/{cookie_id}")
def delete_cookie(cookie_id: int) -> dict:
    from db import delete_facebook_cookie
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        deleted = delete_facebook_cookie(conn, cookie_id)
    return {"ok": deleted, "id": cookie_id}


# ── Group management ────────────────────────────────────────────────────────────

@app.get("/api/admin/groups")
def list_groups() -> dict:
    """List all Facebook groups for crawling."""
    from db import list_facebook_groups
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        return {"groups": list_facebook_groups(conn)}


class GroupUpsert(BaseModel):
    url: str
    name: str | None = None
    priority: int = 0
    scroll_rounds: int = 16
    max_posts_per_group: int = 120


@app.post("/api/admin/groups")
def add_group(body: GroupUpsert) -> dict:
    """Add or update a Facebook group."""
    from db import upsert_facebook_group
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        group_id = upsert_facebook_group(
            conn,
            url=body.url,
            name=body.name,
            priority=body.priority,
            scroll_rounds=body.scroll_rounds,
            max_posts_per_group=body.max_posts_per_group,
        )
    return {"ok": True, "id": group_id, "url": body.url}


@app.delete("/api/admin/groups/{group_id}")
def delete_group(group_id: int) -> dict:
    """Delete (deactivate) a Facebook group."""
    from db import connect_db, ensure_schema, get_database_url
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fb_groups SET status = 'inactive', updated_at = NOW() WHERE id = %s",
                (group_id,),
            )
        conn.commit()
    return {"ok": True, "id": group_id}


# ── Crawl run ────────────────────────────────────────────────────────────────

class CrawlNow(BaseModel):
    group_ids: list[int] = []
    cookie_ids: list[int] = []


@app.post("/api/admin/crawl/now")
def crawl_now(body: CrawlNow) -> dict:
    """Trigger a crawl run immediately for specified groups."""
    ok, message = _start_crawl_process(body.group_ids or [])
    return {"ok": ok, "message": message}


# ── Crawler settings ────────────────────────────────────────────────────────────

@app.get("/api/admin/crawler/settings")
def get_crawler_settings_api() -> dict:
    from db import get_crawler_settings
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        return {"settings": get_crawler_settings(conn)}


class CrawlerSettingsUpdate(BaseModel):
    rotation_enabled: bool | None = None
    rotation_after_groups: int | None = None
    default_cookie_id: int | None = None
    workers: int | None = None


@app.post("/api/admin/crawler/settings")
def update_crawler_settings(body: CrawlerSettingsUpdate) -> dict:
    from db import set_crawler_setting
    updates: list[str] = []
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        if body.rotation_enabled is not None:
            set_crawler_setting(conn, "rotation_enabled", str(body.rotation_enabled).lower())
            updates.append("rotation_enabled=" + str(body.rotation_enabled))
        if body.rotation_after_groups is not None:
            set_crawler_setting(conn, "rotation_after_groups", str(body.rotation_after_groups))
            updates.append("rotation_after_groups=" + str(body.rotation_after_groups))
        if body.default_cookie_id is not None:
            set_crawler_setting(conn, "default_cookie_id", str(body.default_cookie_id))
            updates.append("default_cookie_id=" + str(body.default_cookie_id))
        if body.workers is not None:
            w = max(1, min(8, int(body.workers)))
            set_crawler_setting(conn, "workers", str(w))
            updates.append("workers=" + str(w))
    return {"ok": True, "updated": updates}


@app.get("/api/admin/crawl/status")
def crawl_status() -> dict:
    """Return current crawl counts."""
    from db import connect_db, ensure_schema, get_database_url
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM canonical_posts")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM llm_enrichment_queue WHERE status = 'pending'")
            pending = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM llm_post_analyses WHERE status = 'completed'")
            done = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM crawl_history WHERE status = 'running'")
            running = cur.fetchone()[0]
    return {"total_posts": total, "enrichment_pending": pending, "enrichment_done": done, "crawls_running": running}


# ── Schedules ────────────────────────────────────────────────────────────────

@app.get("/api/admin/schedules")
def list_schedules() -> dict:
    from db import list_crawl_schedules
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        return {"schedules": list_crawl_schedules(conn)}


class ScheduleUpsert(BaseModel):
    name: str
    group_ids: list[int] = []
    cookie_ids: list[int] = []
    cron_expr: str = "0 */4 * * *"
    enabled: bool = True


@app.post("/api/admin/schedules")
def upsert_schedule(body: ScheduleUpsert) -> dict:
    from db import upsert_crawl_schedule
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        sid = upsert_crawl_schedule(
            conn,
            name=body.name,
            group_ids=body.group_ids,
            cookie_ids=body.cookie_ids,
            cron_expr=body.cron_expr,
            enabled=body.enabled,
        )
    return {"ok": True, "id": sid}


@app.delete("/api/admin/schedules/{schedule_id}")
def delete_schedule(schedule_id: int) -> dict:
    from db import delete_crawl_schedule
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        delete_crawl_schedule(conn, schedule_id)
    return {"ok": True, "id": schedule_id}
