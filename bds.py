#!/usr/bin/env python3
# bds.py — Cross-platform BDS Agent CLI
# Works from any directory on macOS / Linux / Windows
# Auto-discovers project root and uses the correct Python interpreter.
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# ── Bootstrap: find project root ────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
_ROOT = _SCRIPT_DIR
_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    _VENV_PY = _ROOT / ".venv314" / "Scripts" / "python.exe"
else:
    _VENV_PY = _ROOT / ".venv314" / "bin" / "python"

_PY = _VENV_PY if _VENV_PY.exists() else sys.executable

PID_FILE = _ROOT / "bds.pid"
LOG_FILE = _ROOT / "bds.log"


def _run(args: list[str], background: bool = False) -> int:
    env = os.environ.copy()
    env["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] = "0.0"
    env["PYTHONPATH"] = str(_ROOT)
    kwargs: dict = {"cwd": str(_ROOT), "env": env}
    if background:
        kwargs["stdout"] = open(LOG_FILE, "a")
        kwargs["stderr"] = subprocess.STDOUT
        if _IS_WIN:
            # Windows: DETACHED_PROCESS so child outlives parent
            DETACHED_PROCESS = 0x00000008
            kwargs["creationflags"] = DETACHED_PROCESS
            proc = subprocess.Popen([str(_PY)] + args, **kwargs)  # type: ignore
        else:
            kwargs["preexec_fn"] = os.setsid
            proc = subprocess.Popen([str(_PY)] + args, **kwargs)  # type: ignore
        PID_FILE.write_text(str(proc.pid))
        print(f"PID {proc.pid}")
        return 0
    return subprocess.run([str(_PY)] + args, **kwargs).returncode  # type: ignore


def pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def is_running(p: int) -> bool:
    try:
        os.kill(p, 0)
        return True
    except OSError:
        return False


def start() -> None:
    existing = pid()
    if existing and is_running(existing):
        print(f"BDS Agent dang chay (PID {existing})")
        return
    print("Khoi dong BDS Agent...")
    _run(["-m", "uvicorn", "api_app:app", "--host", "0.0.0.0", "--port", "8000"], background=True)
    print("Da khoi dong thanh cong")
    print("  Mo: http://localhost:8000")


def stop() -> None:
    existing = pid()
    if not existing or not is_running(existing):
        PID_FILE.unlink(missing_ok=True)
        print("BDS Agent khong chay")
        return
    try:
        if _IS_WIN:
            os.kill(existing, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(existing), signal.SIGTERM)
        time.sleep(1)
        if is_running(existing):
            if _IS_WIN:
                os.kill(existing, signal.SIGTERM)
            else:
                os.killpg(os.getpgid(existing), signal.SIGKILL)
    except OSError:
        pass
    PID_FILE.unlink(missing_ok=True)
    print("Da dung BDS Agent")


def status() -> None:
    existing = pid()
    if existing and is_running(existing):
        print(f"Dang chay (PID {existing})")
    else:
        PID_FILE.unlink(missing_ok=True)
        print("Khong chay")


def log(n: int = 30) -> None:
    if not LOG_FILE.exists():
        print("Khong co log")
        return
    lines = LOG_FILE.read_text().strip().splitlines()
    for line in lines[-n:]:
        print(line)


def crawl() -> None:
    _run(["facebook_group_scraper.py"])


def enrich() -> None:
    _run(["llm_enricher.py"])


def telegram_test() -> None:
    _run(["-c", (
        "from services.telegram import get_notifier;"
        "n=get_notifier();"
        "ok=n.send('OK: BDS CLI hoat dong tot!');"
        "print('Gui thanh cong:', ok)"
    )])


# ── Commands ──────────────────────────────────────────────────────────────────
_COMMANDS: dict[str, callable] = {
    "start":    start,
    "stop":     stop,
    "restart":  lambda: (stop(), start()),
    "status":   status,
    "log":      lambda: log(int(sys.argv[2]) if len(sys.argv) > 2 else 30),
    "crawl":    crawl,
    "enrich":   enrich,
    "telegram": telegram_test,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else None

    if not cmd or cmd not in _COMMANDS:
        print("BDS Agent CLI")
        print("=" * 38)
        print("  bds start     Khoi dong server")
        print("  bds stop      Dung server")
        print("  bds restart   Restart server")
        print("  bds status    Trang thai")
        print("  bds log       Xem log (30 dong cuoi)")
        print("  bds log 100   Xem 100 dong cuoi")
        print("  bds crawl     Chay scraper 1 lan")
        print("  bds enrich    Chay enricher 1 lan")
        print("  bds telegram  Test Telegram")
        sys.exit(0 if not cmd else 1)

    _COMMANDS[cmd]()
