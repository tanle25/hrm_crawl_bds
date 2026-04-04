"""
services/zalo.py — Python wrapper around zca-js Node.js scripts.

Provides:
  - ZaloSession: manages login via zcookies.json (cookie + imei + userAgent)
  - zalo_send(): send a single message
  - zalo_bulk_send(): send multiple messages with delay

Env vars:
  ZALO_COOKIE       — Zalo cookie JSON string (array or stringified array)
  ZALO_IMEI         — Zalo imei / z_uuid (from localStorage)
  ZALO_USER_AGENT   — Browser userAgent string
  ZALO_BULK_DELAY_S — delay between bulk messages (default: 5)
  ZALO_NODE_PATH    — path to node binary (default: node)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env from project root so ZALO_IMEI / ZALO_USER_AGENT are available
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

logger = logging.getLogger("zalo")

# ── Config ────────────────────────────────────────────────────────────────────

ZALO_DIR = Path(__file__).parent.parent / "zalo"
COOKIES_FILE = ZALO_DIR / "zcookies.json"
SEND_SCRIPT = ZALO_DIR / "zalo_send.js"
SEND_SCRIPT_JS = str(ZALO_DIR / "zalo_send.js")
BULK_DELAY = int(os.getenv("ZALO_BULK_DELAY_S", "5"))
NODE_BIN = os.getenv("ZALO_NODE_PATH", "node")


def _cookie_from_env() -> str | None:
    raw = os.getenv("ZALO_COOKIE", "") or None
    if not raw:
        return None
    # Accept both JSON string and raw string
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return raw  # already JSON
        return json.dumps(parsed)
    except json.JSONDecodeError:
        return raw  # treat as raw cookie string


def _run_js(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    cmd = [NODE_BIN, str(SEND_SCRIPT), *args]
    env = {**os.environ, "ZALO_BULK_DELAY_S": str(BULK_DELAY)}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(ZALO_DIR))


# ── ZaloSession ────────────────────────────────────────────────────────────────

@dataclass
class ZaloSession:
    """
    Manages a Zalo session.

    login()     — login using zcookies.json or ZALO_* env vars
    send()      — send text to a user or group
    bulk_send() — send multiple messages with delay
    status()    — check session state
    clear()     — delete saved credentials
    """
    logged_in: bool = field(default=False, init=False)

    def _do_login(self, cookie: str | None = None, imei: str | None = None,
                  user_agent: str | None = None) -> dict[str, Any]:
        """Call JS login and return parsed result."""
        args = ["login"]
        if cookie:
            args += ["--cookie", cookie]
        if imei:
            args += ["--imei", imei]
        if user_agent:
            args += ["--ua", user_agent]

        try:
            result = _run_js(args, timeout=30)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Login timed out. Check Zalo credentials.")

        if result.returncode == 0 and "[OK]" in result.stdout:
            self.logged_in = True
            logger.info("[zalo] Login OK")
            return {"status": "ok"}

        raise RuntimeError(f"Login failed: {result.stderr.strip() or result.stdout.strip()}")

    def login(
        self,
        *,
        cookie: str | None = None,
        imei: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        """
        Login to Zalo using provided credentials or env vars / zcookies.json.

        Args:
            cookie:      Zalo cookie (JSON string or raw string)
            imei:        Zalo z_uuid from localStorage
            user_agent:  Browser userAgent string
        """
        return self._do_login(cookie=cookie, imei=imei, user_agent=user_agent)

    def status(self) -> dict[str, Any]:
        """Check session state."""
        try:
            result = _run_js(["status"], timeout=10)
            if result.returncode == 0:
                data = json.loads(result.stdout.strip())
                self.logged_in = data.get("logged_in", False)
                return data
        except Exception as exc:
            logger.warning("[zalo] Status check failed: %s", exc)
        return {
            "logged_in": self.logged_in,
            "credentials": None,
        }

    def clear(self) -> dict[str, Any]:
        """Delete saved credentials (zcookies.json)."""
        try:
            _run_js(["clear-session"], timeout=10)
        except Exception:
            pass
        if COOKIES_FILE.exists():
            COOKIES_FILE.unlink()
        self.logged_in = False
        return {"status": "ok"}

    def find_user(self, phone: str) -> dict[str, Any]:
        """
        Find a Zalo user by phone number (from friends list).
        Returns { zalo_id, display_name, avatar }.
        """
        try:
            result = _run_js(["find-user", phone], timeout=20)
            data = json.loads(result.stdout.strip())
            if result.returncode != 0 or (isinstance(data, dict) and data.get("error")):
                raise RuntimeError(data.get("error") or result.stderr.strip() or result.stdout.strip())
            return data
        except json.JSONDecodeError:
            raise RuntimeError(f"Find user failed: {result.stderr.strip() or result.stdout.strip()}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Find user failed: {exc.stderr or exc.stdout}") from exc

    def list_friends(self) -> list[dict[str, Any]]:
        """List all Zalo friends."""
        try:
            result = _run_js(["list-friends"], timeout=60)
            data = json.loads(result.stdout.strip())
            if result.returncode != 0 or (isinstance(data, dict) and data.get("error")):
                raise RuntimeError(data.get("error") or result.stderr.strip() or result.stdout.strip())
            return data
        except json.JSONDecodeError:
            raise RuntimeError(f"List friends failed: {result.stderr.strip() or result.stdout.strip()}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"List friends failed: {exc.stderr or exc.stdout}") from exc

    def account_info(self) -> dict[str, Any]:
        """Get current Zalo account info."""
        try:
            result = _run_js(["account-info"], timeout=20)
            data = json.loads(result.stdout.strip())
            if result.returncode != 0 or (isinstance(data, dict) and data.get("error")):
                raise RuntimeError(data.get("error") or result.stderr.strip() or result.stdout.strip())
            return data
        except json.JSONDecodeError:
            raise RuntimeError(f"Account info failed: {result.stderr.strip() or result.stdout.strip()}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Account info failed: {exc.stderr or exc.stdout}") from exc

    def friends_summary(self) -> dict[str, Any]:
        """Get summary of friends activity status."""
        try:
            result = _run_js(["friends-summary"], timeout=90)
            data = json.loads(result.stdout.strip())
            if result.returncode != 0 or (isinstance(data, dict) and data.get("error")):
                raise RuntimeError(data.get("error") or result.stderr.strip() or result.stdout.strip())
            return data
        except json.JSONDecodeError:
            raise RuntimeError(f"Friends summary failed: {result.stderr.strip() or result.stdout.strip()}")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Friends summary failed: {exc.stderr or exc.stdout}") from exc

    def zalo_forward(self, message: str, user_ids: list[str]) -> dict[str, Any]:
        """
        Forward a message to multiple Zalo users (max 100 per batch).
        Calls the API once per batch of up to 100 users.
        """
        if not user_ids:
            return {"ok": 0, "sent": 0, "failed": 0, "batches": 0}
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False)
        tmp.write(json.dumps({"message": message, "userIds": user_ids}))
        tmp.close()
        result = _run_js(["forward", tmp.name], timeout=120)
        return json.loads(result.stdout.strip())

    def zalo_bulk_forward(
        self,
        message: str,
        user_ids: list[str],
        batch_size: int = 100,
        delay_between_batches: int = 2,
    ) -> dict[str, Any]:
        """
        Forward a message to all user_ids in batches of batch_size (default 100).
        """
        if not user_ids:
            return {"ok": 0, "sent": 0, "failed": 0, "batches": 0}
        total = len(user_ids)
        batches = (total + batch_size - 1) // batch_size
        sent = failed = 0
        for i in range(batches):
            batch = user_ids[i * batch_size : (i + 1) * batch_size]
            try:
                result = self.zalo_forward(message, batch)
                sent += result.get("ok", 0)
                failed += result.get("fail", 0)
            except Exception as exc:
                failed += len(batch)
                logger.error("[zalo] Forward batch %s failed: %s", i + 1, exc)
            if i < batches - 1:
                time.sleep(delay_between_batches)
        return {"ok": True, "sent": sent, "failed": failed, "total": total, "batches": batches}

    def send(self, user_id: str, message: str, *, group: bool = False) -> dict[str, Any]:
        """
        Send a single Zalo message.

        Args:
            user_id: Zalo user ID or group ID
            message: Message text
            group:   Send to group instead of user
        """
        cmd = "send-group" if group else "send"
        try:
            result = _run_js([cmd, user_id, message], timeout=30)
            if result.returncode == 0:
                logger.info("[zalo] Sent to %s", user_id)
                return {"status": "ok", "user_id": user_id}
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        except subprocess.CalledProcessError as exc:
            logger.error("[zalo] Send failed: %s", exc.stderr or exc.stdout)
            raise RuntimeError(f"Send failed: {exc.stderr or exc.stdout}") from exc

    def bulk_send(
        self,
        messages: list[dict[str, str]],
        delay: int | None = None,
        on_progress: callable | None = None,
    ) -> dict[str, Any]:
        """
        Send multiple messages sequentially.

        Args:
            messages:     List of {"user_id": "...", "message": "...", "group": bool}
            delay:        Seconds between messages (min 3, default from ZALO_BULK_DELAY_S)
            on_progress:  fn(sent: int, total: int) called after each send
        """
        if not messages:
            return {"status": "ok", "sent": 0, "failed": 0}

        delay = max(3, delay if delay is not None else BULK_DELAY)
        total = len(messages)
        sent = failed = 0

        for i, item in enumerate(messages):
            uid = item.get("user_id") or item.get("userId") or item.get("zalo_id") or ""
            msg = item.get("message") or item.get("msg") or ""
            is_group = bool(item.get("group", False))

            if not uid or not msg:
                failed += 1
                continue

            cmd = "send-group" if is_group else "send"
            try:
                result = _run_js([cmd, uid, msg], timeout=30)
                if result.returncode == 0:
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

            if on_progress:
                on_progress(sent + failed, total)

            if i < total - 1:
                time.sleep(delay)

        logger.info("[zalo] Bulk done: %s sent, %s failed", sent, failed)
        return {"status": "ok", "sent": sent, "failed": failed, "total": total}


# ── Module-level singleton ──────────────────────────────────────────────────────

_zalo_session: ZaloSession | None = None


def get_session() -> ZaloSession:
    global _zalo_session
    if _zalo_session is None:
        _zalo_session = ZaloSession()
    return _zalo_session


def zalo_send(user_id: str, message: str, **kwargs) -> dict[str, Any]:
    """Send a single message using the global session."""
    return get_session().send(user_id, message, **kwargs)


def zalo_bulk_send(messages: list[dict[str, str]], **kwargs) -> dict[str, Any]:
    """Send bulk messages using the global session."""
    return get_session().bulk_send(messages, **kwargs)
