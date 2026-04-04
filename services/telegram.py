"""
services/telegram.py — Telegram Bot notification wrapper.

Provides:
  - TelegramNotifier: manages bot token, chat ID, and sending
  - get_notifier(): module-level singleton
  - notify(), notify_lead(), notify_cookie_dead(), notify_crawl_done()

Env vars (from .env):
  TELEGRAM_BOT_TOKEN  — bot token from @BotFather (e.g. 123456:ABC-DEF...)
  TELEGRAM_CHAT_ID    — your Telegram user ID or group ID (e.g. 987654321)
  TELEGRAM_ENABLED    — "true" to enable notifications (default: false)

Usage:
  from services.telegram import get_notifier
  get_notifier().notify("Hello from BDS Agent!")
  get_notifier().notify_lead("session_abc", "Nguyễn Văn A", "0909123456", "mua đất")
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

logger = logging.getLogger("telegram")

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
_BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""


# ── Notifier ──────────────────────────────────────────────────────────────────

@dataclass
class TelegramNotifier:
    bot_token: str
    chat_id: str
    enabled: bool

    def _api(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a Telegram Bot API call. Returns parsed JSON response dict."""
        if not self.bot_token or not self.chat_id:
            return {"ok": False, "description": "Bot token or chat_id not configured"}
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        try:
            resp = requests.get(url, params=params or {}, timeout=15)
            return resp.json()
        except requests.RequestException as exc:
            logger.warning("[telegram] API error: %s", exc)
            return {"ok": False, "description": str(exc)}

    def send(
        self,
        text: str,
        *,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
    ) -> bool:
        """
        Send a plain text message. Returns True on success.
        Messages are truncated to 4096 chars (Telegram limit).
        """
        if not self.enabled:
            logger.debug("[telegram] Skipped (disabled): %s", text[:80])
            return False
        if not self.bot_token or not self.chat_id:
            logger.warning("[telegram] Skipped (no token or chat_id)")
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text[:4096],
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        result = self._api("sendMessage", payload)
        if result.get("ok"):
            return True
        logger.warning("[telegram] Send failed: %s", result.get("description"))
        return False

    def status(self) -> dict[str, Any]:
        """Return bot info: enabled, configured, me (bot username)."""
        if not self.bot_token:
            return {"ok": False, "configured": False, "enabled": self.enabled}
        me = self._api("getMe")
        if not me.get("ok"):
            return {"ok": False, "configured": True, "enabled": self.enabled, "error": me.get("description")}
        return {
            "ok": True,
            "configured": True,
            "enabled": self.enabled,
            "bot_username": me.get("result", {}).get("username"),
        }

    # ── Notification templates ────────────────────────────────────────────────

    def notify_lead(
        self,
        session_id: str,
        name: str | None,
        phone: str,
        intent: str | None = None,
        property_type: str | None = None,
        location: str | None = None,
        budget: str | None = None,
    ) -> bool:
        """Notify admin when a lead (phone number) is captured from chat."""
        TYPE_LABELS = {
            "dat": "Đất", "nha": "Nhà", "can_ho": "Căn hộ",
            "phong_tro": "Phòng trọ", "biet_thu": "Biệt thự", "mat_bang": "Mặt bằng",
        }
        INTENT_LABELS = {"ban": "Bán", "mua": "Mua", "thue": "Thuê"}

        lines = [
            "📲 <b>LEAD MỚI!</b>",
            "━━━━━━━━━━━━━━━━",
            f"👤 <b>Tên:</b> {name or 'Không rõ'}",
            f"📱 <b>SDT:</b> {phone}",
        ]
        if intent:
            lines.append(f"🏷️ <b>Nhu cầu:</b> {INTENT_LABELS.get(intent, intent)}")
        if property_type:
            lines.append(f"🏠 <b>Loại BĐS:</b> {TYPE_LABELS.get(property_type, property_type)}")
        if location:
            lines.append(f"📍 <b>Khu vực:</b> {location}")
        if budget:
            lines.append(f"💰 <b>Ngân sách:</b> {budget}")
        lines += [
            "━━━━━━━━━━━━━━━━",
            f"⏰ {time.strftime('%d/%m/%Y %H:%M')}",
            f"🆔 Session: <code>{session_id}</code>",
        ]
        return self.send("\n".join(lines))

    def notify_cookie_dead(self, cookie_name: str, error: str | None = None) -> bool:
        """Notify admin when a Facebook cookie is marked dead."""
        error_part = f"\n❗ <b>Lỗi:</b> <code>{error}</code>" if error else ""
        text = (
            "⚠️ <b>Cookie Die!</b>\n\n"
            f"🍪 Profile: <b>{cookie_name}</b>\n"
            f"🚫 Trạng thái: DEAD\n"
            f"{error_part}\n\n"
            "⏰ Vào lúc: {ts}\n\n"
            "👉 Vào Settings → Cookies để kiểm tra và thay cookie mới."
        ).format(ts=time.strftime("%d/%m/%Y %H:%M"))
        return self.send(text)

    def notify_cookie_alive(self, cookie_name: str) -> bool:
        """Notify admin when a cookie is validated as alive."""
        text = (
            "✅ <b>Cookie Alive</b>\n\n"
            f"🍪 Profile: <b>{cookie_name}</b>\n"
            f"🟢 Trạng thái: ALIVE\n\n"
            "⏰ Vào lúc: {ts}"
        ).format(ts=time.strftime("%d/%m/%Y %H:%M"))
        return self.send(text)

    def notify_crawl_done(
        self,
        group_url: str,
        posts_found: int,
        posts_inserted: int,
        duration_sec: float | None = None,
        error: str | None = None,
    ) -> bool:
        """Notify admin when a crawl run completes (per group)."""
        dur_part = f" ⏱ {duration_sec:.0f}s" if duration_sec else ""
        err_part = f"\n❗ <b>Lỗi:</b> <code>{error}</code>" if error else ""
        inserted_emoji = "🆕" if posts_inserted > 0 else "—"
        text = (
            "🕷️ <b>Crawl Done</b>\n\n"
            "📌 Group: {group}\n"
            "🔍 Posts thấy: {found}\n"
            "{emoji} Posts mới: <b>{inserted}</b>{dur}\n"
            "{err}"
            "\n⏰ Vào lúc: {ts}"
        ).format(
            group=group_url[:80],
            found=posts_found,
            emoji=inserted_emoji,
            inserted=posts_inserted,
            dur=dur_part,
            err=err_part,
            ts=time.strftime("%d/%m/%Y %H:%M"),
        )
        return self.send(text)

    def notify_crawl_started(self, group_url: str) -> bool:
        """Notify admin when a crawl run starts."""
        text = (
            "🕷️ <b>Crawl Started</b>\n\n"
            "📌 Group: {group}\n"
            "⏰ Vào lúc: {ts}"
        ).format(
            group=group_url[:80],
            ts=time.strftime("%d/%m/%Y %H:%M"),
        )
        return self.send(text)


# ── Module singleton ───────────────────────────────────────────────────────────

_notifier: TelegramNotifier | None = None


def get_notifier() -> TelegramNotifier:
    """Return (or create) the global TelegramNotifier singleton."""
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier(
            bot_token=BOT_TOKEN,
            chat_id=CHAT_ID,
            enabled=ENABLED,
        )
    return _notifier
