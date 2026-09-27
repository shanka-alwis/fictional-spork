"""Keeps one ffmpeg recording process per camera running whenever it should be recording."""
import os
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from . import events, ffmpeg
from .schedule import should_record

STALL_SECONDS = 90       # restart if no bytes written for this long
MAX_BACKOFF = 120


class CameraRecorder:
    def __init__(self, camera: dict, cfg):
        self.camera = camera
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.last_error: str | None = None
        self.failures = 0
        self.next_attempt = 0.0
        self.stderr_tail: deque = deque(maxlen=20)

    @property
    def out_dir(self) -> Path:
        return self.cfg.recordings_dir / str(self.camera["id"])

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        cam = self.camera
        cmd = ffmpeg.record_cmd(self.cfg.ffmpeg, cam["url"], str(self.out_dir),
                                cam["segment_seconds"], cam["audio"])
        self.stderr_tail.clear()
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE)
        except FileNotFoundError:
            self._fail(f"ffmpeg not found ({self.cfg.ffmpeg})")
            return
        self.started_at = time.time()
        threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
        events.emit("info", cam["name"], "recording started")

    def _drain_stderr(self, proc):
        for line in proc.stderr:
            self.stderr_tail.append(events.redact(line.decode(errors="replace").rstrip()))

    def stop(self, reason="recording stopped"):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGINT)  # lets ffmpeg flush the current segment
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            events.emit("info", self.camera["name"], reason)
        self.started_at = None

    def _fail(self, message):
        self.last_error = message
        self.failures += 1
        backoff = min(MAX_BACKOFF, 5 * 2 ** (self.failures - 1))
        self.next_attempt = time.time() + backoff
        events.emit("error", self.camera["name"], f"{message} (retrying in {backoff}s)")

    def _latest_write(self) -> float:
        try:
            return max((e.stat().st_mtime for e in os.scandir(self.out_dir) if e.name.endswith(".mp4")),
                       default=0.0)
        except FileNotFoundError:
            return 0.0

    def tick(self, now: float):
        want = should_record(self.camera)
        if self.proc is not None and self.proc.poll() is not None:
            code = self.proc.returncode
            tail = self.stderr_tail[-1] if self.stderr_tail else ""
            self.proc = None
            self.started_at = None
            if want:
                self._fail(f"ffmpeg exited with code {code}: {tail}".rstrip(": "))
        if want and not self.running and now >= self.next_attempt:
            self.start()
        elif not want and self.running:
            self.stop()
        elif self.running and now - self.started_at > STALL_SECONDS:
            if now - self._latest_write() > STALL_SECONDS:
                self.stop("stream stalled, restarting")
                self._fail("no data received from camera")
            elif self.failures and now - self.started_at > 300:
                self.failures = 0  # healthy for a while: reset backoff
                self.last_error = None

    def status(self) -> dict:
        want = should_record(self.camera)
        if self.running:
            state = "recording"
        elif want and self.last_error:
            state = "error"
        elif want:
            state = "starting"
        else:
            state = "idle"
        return {"state": state, "since": self.started_at, "error": self.last_error if state == "error" else None}


class RecorderManager:
    def __init__(self, db, cfg, interval: float = 5.0):
        self.db = db
        self.cfg = cfg
        self.interval = interval
        self.recorders: dict[int, CameraRecorder] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="recorder", daemon=True)
        self._thread.start()

    def shutdown(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
        with self._lock:
            for rec in self.recorders.values():
                rec.stop()
            self.recorders.clear()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.sync()
            except Exception as e:  # never let the supervisor die
                events.emit("error", "recorder", f"supervisor error: {e}")
            self._stop.wait(self.interval)

    def sync(self):
        """Reconcile running processes with the camera table."""
        cameras = {c["id"]: c for c in self.db.list_cameras()}
        now = time.time()
        with self._lock:
            for cam_id in list(self.recorders):
                if cam_id not in cameras:
                    self.recorders.pop(cam_id).stop()
            for cam_id, cam in cameras.items():
                rec = self.recorders.get(cam_id)
                if rec is None:
                    rec = self.recorders[cam_id] = CameraRecorder(cam, self.cfg)
                elif rec.camera != cam:
                    restart = any(rec.camera.get(k) != cam.get(k) for k in ("url", "segment_seconds", "audio"))
                    if restart and rec.running:
                        rec.stop("settings changed, restarting")
                    rec.camera = cam
                    rec.failures, rec.next_attempt, rec.last_error = 0, 0.0, None
                rec.tick(now)

    def status(self, cam_id) -> dict:
        with self._lock:
            rec = self.recorders.get(cam_id)
            return rec.status() if rec else {"state": "idle", "since": None, "error": None}

    def is_recording(self, cam_id) -> bool:
        with self._lock:
            rec = self.recorders.get(cam_id)
            return bool(rec and rec.running)
