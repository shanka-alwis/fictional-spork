"""In-memory event log shown on the Logs page."""
import logging
import re
import threading
from collections import deque
from datetime import datetime

log = logging.getLogger("dvr")
_events: deque = deque(maxlen=500)
_lock = threading.Lock()
_CRED_RE = re.compile(r"(://[^:/@\s]+:)[^@\s]+@")


def redact(text: str) -> str:
    """Hide passwords embedded in stream URLs (rtsp://user:pass@host)."""
    return _CRED_RE.sub(r"\1***@", text)


def emit(level: str, source: str, message: str):
    message = redact(message)
    getattr(log, level if level in ("info", "warning", "error") else "info")("[%s] %s", source, message)
    with _lock:
        _events.append({"time": datetime.now().isoformat(timespec="seconds"),
                        "level": level, "source": source, "message": message})


def recent(limit=200):
    with _lock:
        return list(_events)[-limit:][::-1]
