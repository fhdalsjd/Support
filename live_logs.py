"""live_logs.py — in-memory ring buffer of recent log records, for the web
dashboard's "Live Logs" panel.

This mirrors exactly what you'd see in the Railway / terminal logs (same
messages, same loggers: sniper, smart-sl, wallet, membot, security, trading),
just without leaving the dashboard. Not persisted to disk and not meant as a
long-term audit trail -- it only needs to answer "what has the process been
doing in the last few minutes" while it's running.
"""
from __future__ import annotations

import logging
import threading

_MAX_RECORDS = 300
_lock = threading.Lock()
_records: list[dict] = []


class DashboardLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        with _lock:
            _records.append({
                "time": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            })
            if len(_records) > _MAX_RECORDS:
                del _records[: len(_records) - _MAX_RECORDS]


def install(level: int = logging.INFO) -> None:
    """Attach the dashboard log handler to the root logger exactly once."""
    root = logging.getLogger()
    if any(isinstance(h, DashboardLogHandler) for h in root.handlers):
        return
    handler = DashboardLogHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)


def get_recent(limit: int = 150) -> list[dict]:
    with _lock:
        return list(reversed(_records[-limit:]))
