from datetime import datetime, timedelta

from app.db import Database
from app.storage import apply_retention

from .conftest import make_segment

NOW = datetime(2026, 9, 27, 12, 0)


def never_full(path):
    return 50.0, 500, 1000


def setup(cfg):
    db = Database(cfg.db_path)
    dest = db.create_destination({"name": "d", "type": "local", "target": "/x", "cron": "0 1 * * *",
                                  "camera_ids": None, "delete_after_upload": False, "enabled": True})
    return db, dest


def mark_uploaded(db, dest, rel, when):
    db.record_upload(dest, rel, "done", 1)
    with db.conn() as c:
        c.execute("UPDATE uploads SET updated_at = ? WHERE rel_path = ?", (when.isoformat(timespec="seconds"), rel))


def test_deletes_one_day_after_upload(cfg):
    db, dest = setup(cfg)
    a = make_segment(cfg, 1, "20260925_100000.mp4")
    b = make_segment(cfg, 1, "20260926_100000.mp4")
    c = make_segment(cfg, 1, "20260926_110000.mp4")  # never uploaded
    mark_uploaded(db, dest, "1/20260925_100000.mp4", NOW - timedelta(hours=25))
    mark_uploaded(db, dest, "1/20260926_100000.mp4", NOW - timedelta(hours=11))
    assert apply_retention(db, cfg.recordings_dir, NOW, never_full) == 1
    assert not a.exists() and b.exists() and c.exists()


def test_never_deletes_unuploaded_by_default(cfg):
    db, _ = setup(cfg)
    old = make_segment(cfg, 1, "20200101_000000.mp4")
    assert apply_retention(db, cfg.recordings_dir, NOW, never_full) == 0
    db.update_settings({"max_age_days": 30})
    assert apply_retention(db, cfg.recordings_dir, NOW, never_full) == 1
    assert not old.exists()


def test_disk_full_removes_oldest_uploaded_only(cfg):
    db, dest = setup(cfg)
    segs = [make_segment(cfg, 1, f"2026092{d}_100000.mp4", size=100) for d in (4, 5, 6)]
    unuploaded = make_segment(cfg, 1, "20260923_100000.mp4", size=100)
    for d in (4, 5, 6):
        mark_uploaded(db, dest, f"1/2026092{d}_100000.mp4", NOW - timedelta(hours=1))
    # 1000-byte disk at 90% used; freeing 100 bytes brings it to 80% (< 85%)
    assert apply_retention(db, cfg.recordings_dir, NOW, lambda p: (90.0, 100, 1000)) == 1
    assert unuploaded.exists() and not segs[0].exists() and segs[1].exists() and segs[2].exists()
