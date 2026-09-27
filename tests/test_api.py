import os

from app.config import Config
from app.main import create_app
from fastapi.testclient import TestClient

from .conftest import make_segment


def test_camera_crud(client):
    r = client.post("/api/cameras", json={"name": "Door", "url": "rtsp://u:p@10.0.0.2/s1"})
    assert r.status_code == 201
    cam = r.json()
    assert cam["record_mode"] == "continuous" and cam["status"]["state"] == "idle"
    r = client.put(f"/api/cameras/{cam['id']}", json={
        "name": "Door", "url": "rtsp://x", "record_mode": "schedule",
        "schedule": [{"days": [0], "start": "22:00", "end": "06:00"}]})
    assert r.json()["schedule"][0]["start"] == "22:00"
    assert client.post("/api/cameras", json={"name": "", "url": "x"}).status_code == 422
    assert client.delete(f"/api/cameras/{cam['id']}").status_code == 200
    assert client.get("/api/cameras").json() == []


def test_destination_validation(client):
    bad = client.post("/api/destinations", json={"name": "d", "type": "local", "target": "/x", "cron": "nope"})
    assert bad.status_code == 422
    r = client.post("/api/destinations", json={"name": "d", "type": "local", "target": "/x", "cron": "*/5 * * * *"})
    assert r.status_code == 201 and r.json()["pending"] == 0


def test_recordings_listing_and_delete(client, cfg):
    cam = client.post("/api/cameras", json={"name": "Yard", "url": "rtsp://x"}).json()
    make_segment(cfg, cam["id"], "20260920_101500.mp4")
    make_segment(cfg, cam["id"], "20260921_090000.mp4")
    make_segment(cfg, cam["id"], "notes.txt")
    assert client.get("/api/recordings/dates").json() == ["2026-09-21", "2026-09-20"]
    recs = client.get("/api/recordings", params={"date": "2026-09-20"}).json()
    assert [r["filename"] for r in recs] == ["20260920_101500.mp4"]
    assert "path" not in recs[0]
    assert client.get(f"/api/recordings/{cam['id']}/20260920_101500.mp4").status_code == 200
    assert client.get(f"/api/recordings/{cam['id']}/..%2Fdvr.sqlite3").status_code == 404
    assert client.delete(f"/api/recordings/{cam['id']}/20260920_101500.mp4").status_code == 200
    assert len(client.get("/api/recordings").json()) == 1


def test_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DVR_DISABLE_WORKERS", "1")
    monkeypatch.setenv("DVR_PASSWORD", "s3cret")
    with TestClient(create_app(Config(tmp_path))) as c:
        assert c.get("/api/cameras").status_code == 401
        assert c.get("/api/session").json() == {"auth_enabled": True, "user": None}
        assert c.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
        assert c.post("/api/login", json={"username": "admin", "password": "s3cret"}).status_code == 200
        assert c.get("/api/cameras").status_code == 200
        c.post("/api/logout")
        assert c.get("/api/cameras").status_code == 401


def test_settings_roundtrip(client):
    s = client.get("/api/settings").json()
    assert s["local_keep_days_after_upload"] == 1 and s["max_disk_percent"] == 85
    s["local_keep_days_after_upload"] = 2
    assert client.put("/api/settings", json=s).json()["local_keep_days_after_upload"] == 2
    assert client.put("/api/settings", json={**s, "max_disk_percent": 150}).status_code == 422


def test_tapo_camera_live_url(client):
    cam = client.post("/api/cameras", json={"name": "Gate", "url": "rtsp://a:b@10.0.0.5:554/stream1",
                                           "live_url": "rtsp://a:b@10.0.0.5:554/stream2"}).json()
    assert cam["live_url"].endswith("stream2")
    cam = client.put(f"/api/cameras/{cam['id']}", json={"name": "Gate", "url": "rtsp://x", "live_url": " "}).json()
    assert cam["live_url"] is None


def test_destination_defaults(client):
    d = client.post("/api/destinations", json={"name": "Drive", "type": "rclone", "target": "gdrive:CCTV"}).json()
    assert d["cron"] == "0 1 * * *" and d["remote_keep_days"] == 30
    assert d["pending"] == 0 and d["pending_bytes"] == 0


def test_status_reports_rates(client, cfg):
    cam = client.post("/api/cameras", json={"name": "Yard", "url": "rtsp://x"}).json()
    from datetime import datetime, timedelta
    import os
    start = datetime.now() - timedelta(minutes=10)
    p = make_segment(cfg, cam["id"], start.strftime("%Y%m%d_%H%M%S.mp4"), size=300_000)
    end = (start + timedelta(minutes=5)).timestamp()
    os.utime(p, (end, end))
    st = client.get("/api/status").json()
    assert round(st["bytes_per_day"]["Yard"]) == 300_000 * 288  # 5-minute segment -> 288 per day


def test_static_ui(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>DVR</title>" in r.text
