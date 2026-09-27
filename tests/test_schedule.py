from datetime import datetime

import pytest

from app.schedule import in_schedule, should_record, validate

NIGHT = [{"days": [0, 1, 2, 3, 4], "start": "22:00", "end": "06:00"}]  # Mon-Fri nights


def test_overnight_window():
    assert in_schedule(NIGHT, datetime(2026, 9, 21, 23, 0))      # Mon 23:00
    assert in_schedule(NIGHT, datetime(2026, 9, 22, 5, 59))      # Tue 05:59, from Mon night
    assert not in_schedule(NIGHT, datetime(2026, 9, 22, 6, 0))
    assert not in_schedule(NIGHT, datetime(2026, 9, 21, 12, 0))
    assert in_schedule(NIGHT, datetime(2026, 9, 26, 3, 0))       # Sat 03:00, from Fri night
    assert not in_schedule(NIGHT, datetime(2026, 9, 26, 23, 0))  # Sat night not scheduled


def test_day_window():
    s = [{"days": [5, 6], "start": "09:00", "end": "17:30"}]
    assert in_schedule(s, datetime(2026, 9, 26, 9, 0))
    assert not in_schedule(s, datetime(2026, 9, 26, 17, 30))
    assert not in_schedule(s, datetime(2026, 9, 25, 10, 0))


def test_should_record_modes():
    cam = {"enabled": True, "record_mode": "continuous"}
    assert should_record(cam)
    assert not should_record({**cam, "enabled": False})
    assert not should_record({**cam, "record_mode": "off"})
    assert should_record({**cam, "record_mode": "schedule", "schedule": NIGHT}, datetime(2026, 9, 21, 23, 0))


def test_validate_rejects_bad_input():
    with pytest.raises(ValueError):
        validate([{"days": [7], "start": "01:00", "end": "02:00"}])
    with pytest.raises(ValueError):
        validate([{"start": "25:00", "end": "02:00"}])


def test_friendly_ffmpeg_errors():
    from app.ffmpeg import explain
    assert "Camera Account" in explain("method DESCRIBE failed: 401 Unauthorized")
    assert explain("something else") == "something else"
