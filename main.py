#!/usr/bin/env python3
"""PS VIBE Gaming Lounge — Staff Bot Entry Point.
Modularized in Phase 4. All code lives in bot/ package.
"""
import os
import sys
import time
import signal
import asyncio
import logging

# Import the entire bot package (infrastructure + handlers + app)
from bot import main, keep_alive, ensure_sheet_headers
from bot import _load_cfg, _load_members, _bg_cache_refresh


if __name__ == "__main__":
    import subprocess

    _my_pid   = os.getpid()
    _LOCK_PATH = "/tmp/ps_vibe_bot.lock"

    # ── Step 1: Kill ALL other python3 main.py processes (no cooperation needed) ──
    try:
        _result = subprocess.run(
            ["pgrep", "-f", "python3 main.py"],
            capture_output=True, text=True,
        )
        for _pid_str in _result.stdout.strip().split("\n"):
            try:
                _pid = int(_pid_str.strip())
            except ValueError:
                continue
            if _pid == _my_pid:
                continue
            logging.warning("Duplicate bot process found (PID %d) — killing...", _pid)
            try:
                os.kill(_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(2)   # give SIGTERM a moment to land
        # Force-kill anything still alive
        for _pid_str in _result.stdout.strip().split("\n"):
            try:
                _pid = int(_pid_str.strip())
            except ValueError:
                continue
            if _pid == _my_pid:
                continue
            try:
                os.kill(_pid, signal.SIGKILL)
                logging.warning("Force-killed PID %d", _pid)
            except ProcessLookupError:
                pass   # already gone — good
    except Exception as _e:
        logging.warning("Process scan failed: %s", _e)

    # ── Step 2: Write PID lock so future restarts can identify us ─────────
    try:
        with open(_LOCK_PATH, "w") as _lf:
            _lf.write(str(_my_pid))
    except Exception:
        pass
    logging.info("Bot started — PID %d", _my_pid)
    ensure_sheet_headers()

    # ── Step 3: Clean shutdown on SIGTERM (Replit workflow stop) ──────────
    def _sigterm_handler(signum, frame):
        logging.info("SIGTERM received — shutting down (PID %d).", _my_pid)
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # ── Step 4: Start keep-alive server & polling loop ────────────────────
    if keep_alive:
        keep_alive()
    while True:
        try:
            # Fresh event loop on every (re)start so run_polling can install
            # its signal handlers even after a previous loop was closed.
            asyncio.set_event_loop(asyncio.new_event_loop())
            main()
        except KeyboardInterrupt:
            logging.info("Bot stopped by operator.")
            break
        except Exception as exc:
            from telegram.error import Conflict
            if isinstance(exc, Conflict):
                logging.warning("Conflict detected — waiting 30 s for Telegram session to expire...")
                time.sleep(30)
            else:
                logging.error("Bot crashed: %s — restarting in 5 s...", exc, exc_info=True)
                time.sleep(5)
