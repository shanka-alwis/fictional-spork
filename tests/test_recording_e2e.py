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


def test_live_frame_endpoint(tmp_path, sample_video, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app
    monkeypatch.setenv("BIGEYE_DISABLE_WORKERS", "1")
    monkeypatch.delenv("BIGEYE_PASSWORD", raising=False)
    with TestClient(create_app(Config(tmp_path))) as c:
        cam = c.post("/api/cameras", json={"name": "T", "url": "rtsp://unused", "live_url": str(sample_video)}).json()
        r = c.get(f"/api/cameras/{cam['id']}/frame")
        assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
        assert c.app.state.live.streams[cam["id"]].url == str(sample_video)  # used the live-view stream
        bad = c.post("/api/cameras", json={"name": "B", "url": str(tmp_path / "missing.mp4")}).json()
        assert c.get(f"/api/cameras/{bad['id']}/frame").status_code == 503
