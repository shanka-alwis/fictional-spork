"""Big Eye, an AgentDVR-style NVR: record IP cameras with ffmpeg and upload footage on a schedule."""
import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware

from . import events, ffmpeg, schedule, storage
from .config import Config
from .db import Database
from .jobs import Jobs, parse_cron
from .live import LiveManager
from .recorder import RecorderManager
from .uploader import UploadError, Uploader

STATIC_DIR = Path(__file__).parent / "static"


# -- request models ----------------------------------------------------------
class CameraIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1)
    live_url: str | None = None  # lower-resolution stream for live view (e.g. Tapo stream2)
    enabled: bool = True
    record_mode: Literal["continuous", "schedule", "off"] = "continuous"
    schedule: list[dict] = []
    segment_seconds: int = Field(300, ge=30, le=3600)
    audio: bool = False

    @field_validator("live_url")
    @classmethod
    def _live_url(cls, v):
        return v.strip() or None if v else None

    @field_validator("schedule")
    @classmethod
    def _schedule(cls, v):
        return schedule.validate(v)


class DestinationIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: Literal["rclone", "local"]
    target: str = Field(min_length=1)
    cron: str = "0 1 * * *"
    camera_ids: list[int] | None = None
    delete_after_upload: bool = False
    remote_keep_days: int = Field(30, ge=0, le=3650)  # 0 = keep forever
    enabled: bool = True

    @field_validator("cron")
    @classmethod
    def _cron(cls, v):
        parse_cron(v)
        return v.strip()


class SettingsIn(BaseModel):
    local_keep_days_after_upload: float = Field(ge=0, le=3650)
    max_age_days: float = Field(ge=0, le=3650)
    max_disk_percent: float = Field(ge=0, le=99)
    live_fps: int = Field(ge=1, le=30)
    live_width: int = Field(ge=160, le=3840)


class LoginIn(BaseModel):
    username: str
    password: str


class TestUrlIn(BaseModel):
    url: str


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config()
    db = Database(cfg.db_path)
    recorder = RecorderManager(db, cfg)
    uploader = Uploader(db, cfg)
    jobs = Jobs(db, cfg, uploader)
    live = LiveManager(cfg)

    @asynccontextmanager
    async def lifespan(app):
        if not cfg.disable_workers:
            recorder.start()
            jobs.start()
        events.emit("info", "system", "Big Eye started" + ("" if cfg.auth_enabled else
                    " (no BIGEYE_PASSWORD set: web UI is not password protected)"))
        yield
        jobs.shutdown()
        recorder.shutdown()
        live.shutdown()

    app = FastAPI(title="Big Eye", lifespan=lifespan)
    app.state.cfg, app.state.db, app.state.recorder = cfg, db, recorder
    app.state.uploader, app.state.jobs, app.state.live = uploader, jobs, live

    # -- auth -----------------------------------------------------------------
    open_paths = {"/api/login", "/api/session"}

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if cfg.auth_enabled and path.startswith("/api/") and path not in open_paths:
            if not request.session.get("user"):
                return JSONResponse({"detail": "login required"}, status_code=401)
        return await call_next(request)

    # added after the auth middleware so it wraps it and request.session is available
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, same_site="strict",
                       max_age=30 * 24 * 3600)

    @app.get("/api/session")
    def session(request: Request):
        return {"auth_enabled": cfg.auth_enabled,
                "user": request.session.get("user") if cfg.auth_enabled else "admin"}

    @app.post("/api/login")
    def login(body: LoginIn, request: Request):
        if not cfg.auth_enabled:
            return {"ok": True}
        good = (hmac.compare_digest(body.username.encode(), cfg.username.encode())
                & hmac.compare_digest(body.password.encode(), cfg.password.encode()))
        if not good:
            events.emit("warning", "auth", f"failed login for '{body.username}'")
            raise HTTPException(401, "invalid username or password")
        request.session["user"] = body.username
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    # -- cameras ---------------------------------------------------------------
    def camera_or_404(cam_id):
        cam = db.get_camera(cam_id)
        if not cam:
            raise HTTPException(404, "camera not found")
        return cam

    def resync():
        if not cfg.disable_workers:
            recorder.sync()

    def with_status(cam):
        return {**cam, "status": recorder.status(cam["id"])}

    @app.get("/api/cameras")
    def list_cameras():
        return [with_status(c) for c in db.list_cameras()]

    @app.post("/api/cameras", status_code=201)
    def create_camera(body: CameraIn):
        cam_id = db.create_camera(body.model_dump())
        events.emit("info", body.name, "camera added")
        resync()
        return with_status(db.get_camera(cam_id))

    @app.get("/api/cameras/{cam_id}")
    def get_camera(cam_id: int):
        return with_status(camera_or_404(cam_id))

    @app.put("/api/cameras/{cam_id}")
    def update_camera(cam_id: int, body: CameraIn):
        camera_or_404(cam_id)
        db.update_camera(cam_id, body.model_dump())
        resync()
        return with_status(db.get_camera(cam_id))

    @app.delete("/api/cameras/{cam_id}")
    def delete_camera(cam_id: int):
        cam = camera_or_404(cam_id)
        db.delete_camera(cam_id)
        resync()
        events.emit("info", cam["name"], "camera removed (recordings kept on disk)")
        return {"ok": True}

    @app.post("/api/cameras/test")
    def test_camera(body: TestUrlIn):
        try:
            jpeg = ffmpeg.snapshot(cfg.ffmpeg, body.url)
        except RuntimeError as e:
            raise HTTPException(400, events.redact(str(e)))
        return Response(jpeg, media_type="image/jpeg")

    @app.get("/api/cameras/{cam_id}/snapshot")
    def snapshot(cam_id: int):
        cam = camera_or_404(cam_id)
        frame = live.latest_frame(cam_id)
        if frame is None:
            try:
                frame = ffmpeg.snapshot(cfg.ffmpeg, cam["live_url"] or cam["url"])
            except RuntimeError as e:
                raise HTTPException(502, events.redact(str(e)))
        return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/cameras/{cam_id}/frame")
    async def live_frame(cam_id: int):
        """Latest live JPEG. The page polls this a few times a second per camera."""
        cam = camera_or_404(cam_id)
        s = db.get_settings()
        stream = live.get(cam_id, cam["live_url"] or cam["url"], int(s["live_fps"]), int(s["live_width"]))
        frame = stream.frame
        if frame is None:  # just started: wait for the first frame
            _, frame = await asyncio.to_thread(stream.wait_frame, 0, 10.0)
        if frame is None:
            raise HTTPException(503, ffmpeg.explain(events.redact(stream.error or "")) or "waiting for camera")
        return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # -- recordings ------------------------------------------------------------
    @app.get("/api/recordings")
    def list_recordings(camera_id: int | None = None, date: str | None = None, limit: int = 500):
        recs = storage.list_recordings(cfg.recordings_dir, {camera_id} if camera_id is not None else None, date)
        uploaded = db.upload_map()
        out = []
        for r in recs[:limit]:
            r = {k: v for k, v in r.items() if k != "path"}
            r["uploaded_to"] = uploaded.get(r["rel_path"], [])
            out.append(r)
        return out

    @app.get("/api/recordings/dates")
    def recording_dates(camera_id: int | None = None):
        return storage.recording_dates(cfg.recordings_dir, camera_id)

    @app.get("/api/recordings/{cam_id}/{filename}")
    def get_recording(cam_id: int, filename: str, download: bool = False):
        p = storage.resolve(cfg.recordings_dir, cam_id, filename)
        if not p:
            raise HTTPException(404, "recording not found")
        return FileResponse(p, media_type="video/mp4", filename=filename if download else None,
                            content_disposition_type="attachment" if download else "inline")

    @app.delete("/api/recordings/{cam_id}/{filename}")
    def delete_recording(cam_id: int, filename: str):
        p = storage.resolve(cfg.recordings_dir, cam_id, filename)
        if not p:
            raise HTTPException(404, "recording not found")
        p.unlink()
        return {"ok": True}

    # -- upload destinations ---------------------------------------------------
    def dest_or_404(dest_id):
        d = db.get_destination(dest_id)
        if not d:
            raise HTTPException(404, "destination not found")
        return d

    def dest_view(d, rates=None):
        rates = storage.daily_rates(cfg.recordings_dir) if rates is None else rates
        cams = d["camera_ids"]
        per_day = sum(v for k, v in rates.items() if not cams or k in cams)
        return {**d, "running": uploader.is_running(d["id"]), "next_run": jobs.next_run(d["id"]),
                **uploader.pending_summary(d), "bytes_per_day": per_day,
                "estimated_remote_bytes": per_day * d["remote_keep_days"] if d["remote_keep_days"] else None}

    @app.get("/api/destinations")
    def list_destinations():
        rates = storage.daily_rates(cfg.recordings_dir)
        return [dest_view(d, rates) for d in db.list_destinations()]

    @app.post("/api/destinations", status_code=201)
    def create_destination(body: DestinationIn):
        dest_id = db.create_destination(body.model_dump())
        jobs.reload()
        return dest_view(db.get_destination(dest_id))

    @app.put("/api/destinations/{dest_id}")
    def update_destination(dest_id: int, body: DestinationIn):
        dest_or_404(dest_id)
        db.update_destination(dest_id, body.model_dump())
        jobs.reload()
        return dest_view(db.get_destination(dest_id))

    @app.delete("/api/destinations/{dest_id}")
    def delete_destination(dest_id: int):
        dest_or_404(dest_id)
        db.delete_destination(dest_id)
        jobs.reload()
        return {"ok": True}

    @app.post("/api/destinations/{dest_id}/run")
    def run_destination(dest_id: int):
        dest_or_404(dest_id)
        started = uploader.run_async(dest_id)
        return {"started": started}

    @app.post("/api/destinations/test")
    def test_destination(body: DestinationIn):
        try:
            return {"ok": True, "message": uploader.test(body.model_dump())}
        except (UploadError, OSError) as e:
            raise HTTPException(400, str(e))

    @app.get("/api/rclone/remotes")
    def rclone_remotes():
        return uploader.list_remotes()

    @app.get("/api/uploads")
    def recent_uploads(limit: int = 100):
        return db.recent_uploads(limit)

    # -- settings, status, logs ------------------------------------------------
    @app.get("/api/settings")
    def get_settings():
        return db.get_settings()

    @app.put("/api/settings")
    def put_settings(body: SettingsIn):
        db.update_settings(body.model_dump())
        return db.get_settings()

    @app.post("/api/retention/run")
    def run_retention():
        return {"removed": storage.apply_retention(db, cfg.recordings_dir)}

    @app.get("/api/status")
    def status():
        cams = db.list_cameras()
        rates = storage.daily_rates(cfg.recordings_dir)
        smart = cfg.data_dir / "smart-status.txt"
        return {
            "cameras": len(cams),
            "recording": sum(1 for c in cams if recorder.is_recording(c["id"])),
            "bytes_per_day": {c["name"]: rates.get(c["id"]) for c in cams},
            "total_bytes_per_day": sum(rates.values()),
            "disk_health": smart.read_text().strip()[:2000] if smart.exists() else None,
            **storage.disk_usage(cfg.recordings_dir),
        }

    @app.get("/api/logs")
    def logs(limit: int = 200):
        return events.recent(limit)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import os
    uvicorn.run(create_app(), host=os.environ.get("BIGEYE_HOST", "0.0.0.0"),
                port=int(os.environ.get("BIGEYE_PORT", "8090")))


if __name__ == "__main__":
    main()
