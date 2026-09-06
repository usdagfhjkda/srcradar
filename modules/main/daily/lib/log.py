"""Minimal stderr + file logger used by diff.py (and any other recon tool).

The orchestrator (daily_monitor.sh) sets LOG_FILE via env so every child
process can append to the same per-run log.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


_RUN_ID = os.environ.get("RUN_ID", time.strftime("%Y%m%d-%H%M%S"))
_LOG_FILE = os.environ.get("LOG_FILE")  # set by daily_monitor.sh


def _emit(level: str, msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [{level}] {_RUN_ID} {msg}"
    print(line, file=sys.stderr, flush=True)
    if _LOG_FILE:
        try:
            Path(_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            # Logging must never raise into the caller.
            pass


def info(msg: str) -> None:
    _emit("INFO", msg)


def warn(msg: str) -> None:
    _emit("WARN", msg)


def error(msg: str) -> None:
    _emit("ERROR", msg)


def get_run_id() -> str:
    return _RUN_ID