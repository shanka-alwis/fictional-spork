"""Recording schedule windows.

A schedule is a list of windows: {"days": [0..6] (Mon=0), "start": "HH:MM", "end": "HH:MM"}.
A window whose end is before its start runs overnight, into the following day.
"""
from datetime import datetime, timedelta


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def validate(schedule) -> list[dict]:
    if not isinstance(schedule, list):
        raise ValueError("schedule must be a list")
    out = []
    for w in schedule:
        days = sorted({int(d) for d in w.get("days", range(7))})
        if any(d < 0 or d > 6 for d in days):
            raise ValueError("days must be 0 (Mon) .. 6 (Sun)")
        start, end = str(w["start"]), str(w["end"])
        for t in (start, end):
            if not 0 <= _minutes(t) <= 24 * 60:
                raise ValueError(f"bad time {t}")
        out.append({"days": days, "start": start, "end": end})
    return out


def in_schedule(schedule, when: datetime | None = None) -> bool:
    when = when or datetime.now()
    now_min = when.hour * 60 + when.minute
    today = when.weekday()
    yesterday = (when - timedelta(days=1)).weekday()
    for w in schedule:
        start, end = _minutes(w["start"]), _minutes(w["end"])
        days = w.get("days", range(7))
        if start <= end:
            if today in days and start <= now_min < end:
                return True
        else:  # overnight window
            if today in days and now_min >= start:
                return True
            if yesterday in days and now_min < end:
                return True
    return False


def should_record(camera: dict, when: datetime | None = None) -> bool:
    if not camera.get("enabled"):
        return False
    mode = camera.get("record_mode")
    if mode == "continuous":
        return True
    if mode == "schedule":
        return in_schedule(camera.get("schedule") or [], when)
    return False
