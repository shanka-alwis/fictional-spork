import pytest

from app.db import Database
from app.storage import list_recordings
from app.uploader import Uploader

from .conftest import HAS_RCLONE, make_segment


def setup(cfg, dest):
    db = Database(cfg.db_path)
    cam = db.create_camera({"name": "Front Door", "url": "rtsp://x", "enabled": True, "record_mode": "off",
                            "schedule": [], "segment_seconds": 300, "audio": False})
    dest_id = db.create_destination({"cron": "0 * * * *", "camera_ids": None, "enabled": True,
                                     "delete_after_upload": False, "name": "d", **dest})
    return db, cam, dest_id


def test_local_upload_is_incremental(cfg, tmp_path):
    target = tmp_path / "drive"
    db, cam, dest_id = setup(cfg, {"type": "local", "target": str(target)})
    make_segment(cfg, cam, "20260927_100000.mp4", size=10)
    make_segment(cfg, cam, "20260927_100500.mp4", age=0)  # still being written
    up = Uploader(db, cfg)
    assert up.run(dest_id) == {"uploaded": 1, "failed": 0}
    assert (target / "Front Door" / "2026-09-27" / "20260927_100000.mp4").stat().st_size == 10
    assert up.run(dest_id) == {"uploaded": 0, "failed": 0}
    assert db.get_destination(dest_id)["last_status"] == "nothing to upload"


def test_delete_after_upload(cfg, tmp_path):
    db, cam, dest_id = setup(cfg, {"type": "local", "target": str(tmp_path / "drive"), "delete_after_upload": True})
    make_segment(cfg, cam, "20260927_100000.mp4")
    Uploader(db, cfg).run(dest_id)
    assert list_recordings(cfg.recordings_dir) == []


def test_camera_filter(cfg, tmp_path):
    db, cam, dest_id = setup(cfg, {"type": "local", "target": str(tmp_path / "drive")})
    db.update_destination(dest_id, {"camera_ids": [cam + 1]})
    make_segment(cfg, cam, "20260927_100000.mp4")
    assert Uploader(db, cfg).run(dest_id)["uploaded"] == 0


def test_failure_is_recorded(cfg, tmp_path):
    cfg.rclone = "/nonexistent/rclone"
    db, cam, dest_id = setup(cfg, {"type": "rclone", "target": "gdrive:CCTV"})
    make_segment(cfg, cam, "20260927_100000.mp4")
    assert Uploader(db, cfg).run(dest_id) == {"uploaded": 0, "failed": 1}
    assert "rclone not found" in db.get_destination(dest_id)["last_status"]


@pytest.mark.skipif(not HAS_RCLONE, reason="rclone not installed")
def test_rclone_upload(cfg, tmp_path):
    # rclone treats a bare absolute path as the local backend, so this runs the real rclone code path.
    remote = tmp_path / "remote"
    remote.mkdir()
    conf = tmp_path / "rclone.conf"
    conf.write_text(f"[fake]\ntype = alias\nremote = {remote}\n")
    cfg.rclone_config = str(conf)
    db, cam, dest_id = setup(cfg, {"type": "rclone", "target": "fake:CCTV"})
    make_segment(cfg, cam, "20260927_100000.mp4", size=42)
    up = Uploader(db, cfg)
    assert up.test(db.get_destination(dest_id)).startswith("connected")
    assert up.list_remotes() == ["fake:"]
    assert up.run(dest_id)["uploaded"] == 1
    assert (remote / "CCTV" / "Front Door" / "2026-09-27" / "20260927_100000.mp4").stat().st_size == 42
