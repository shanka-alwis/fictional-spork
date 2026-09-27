"""Records a looping test video with the real ffmpeg, then uploads the segments."""
import time

from app.config import Config
from app.db import Database
from app.ffmpeg import snapshot
from app.recorder import RecorderManager
from app.storage import list_recordings
from app.uploader import Uploader


def test_record_segments_and_upload(tmp_path, sample_video, monkeypatch):
    monkeypatch.setattr("app.storage.READY_AFTER_SECONDS", 1)
    cfg = Config(tmp_path / "data")
    db = Database(cfg.db_path)
    cam = db.create_camera({"name": "Test", "url": str(sample_video), "enabled": True,
                            "record_mode": "continuous", "schedule": [], "segment_seconds": 30, "audio": False})
    mgr = RecorderManager(db, cfg, interval=0.5)
    mgr.sync()
    assert mgr.is_recording(cam)
    time.sleep(4)
    mgr.shutdown()

    recs = list_recordings(cfg.recordings_dir)
    assert recs and recs[0]["size"] > 1000
    time.sleep(1.1)
    dest = db.create_destination({"name": "local", "type": "local", "target": str(tmp_path / "out"),
                                  "cron": "0 * * * *", "camera_ids": None, "delete_after_upload": False,
                                  "enabled": True})
    assert Uploader(db, cfg).run(dest)["uploaded"] == len(recs)


def test_snapshot(sample_video):
    jpeg = snapshot("ffmpeg", str(sample_video))
    assert jpeg[:2] == b"\xff\xd8"
