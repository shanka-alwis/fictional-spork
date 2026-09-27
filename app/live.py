"""Shared on-demand live frames.

One ffmpeg process per camera decodes the (sub-)stream to JPEG frames that every
viewer polls. Browsers only open ~6 connections per server, so viewers fetch single
frames instead of holding a stream open per camera. The process starts on the first
request and stops once nobody has asked for a frame for a few seconds.
"""
import subprocess
import threading
import time

from . import ffmpeg

IDLE_TIMEOUT = 10


class LiveStream:
    def __init__(self, url, cfg, fps, width):
        self.url, self.cfg, self.fps, self.width = url, cfg, fps, width
        self.frame: bytes | None = None
        self.seq = 0
        self.last_request = time.time()
        self.error: str | None = None
        self.proc: subprocess.Popen | None = None
        self.cond = threading.Condition()
        self._start()

    def _start(self):
        cmd = ffmpeg.mjpeg_cmd(self.cfg.ffmpeg, self.url, self.fps, self.width)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        out = self.proc.stdout
        while True:
            chunk = out.read1(65536) if hasattr(out, "read1") else out.read(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2)
                if start < 0 or end < 0:
                    break
                with self.cond:
                    self.frame = buf[start:end + 2]
                    self.seq += 1
                    self.cond.notify_all()
                buf = buf[end + 2:]
            if len(buf) > 8 * 1024 * 1024:
                buf = b""
        err = self.proc.stderr.read().decode(errors="replace").strip()
        with self.cond:
            self.error = err[-300:] or "stream ended"
            self.cond.notify_all()

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def wait_frame(self, last_seq, timeout=5.0):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq or not self.alive or self.error, timeout)
            return self.seq, self.frame

    def stop(self):
        if self.alive:
            self.proc.kill()
            self.proc.wait()


class LiveManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.streams: dict[int, LiveStream] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._reaper, daemon=True).start()

    def get(self, cam_id, url, fps, width) -> LiveStream:
        """The running stream for a camera, (re)started as needed."""
        with self._lock:
            s = self.streams.get(cam_id)
            if s is None or not s.alive or s.url != url or s.fps != fps or s.width != width:
                if s:
                    s.stop()
                s = self.streams[cam_id] = LiveStream(url, self.cfg, fps, width)
            s.last_request = time.time()
            return s

    def latest_frame(self, cam_id) -> bytes | None:
        with self._lock:
            s = self.streams.get(cam_id)
            return s.frame if s and s.alive else None

    def _reaper(self):
        while not self._stop.wait(2):
            with self._lock:
                for cam_id, s in list(self.streams.items()):
                    if time.time() - s.last_request > IDLE_TIMEOUT:
                        s.stop()
                        del self.streams[cam_id]

    def shutdown(self):
        self._stop.set()
        with self._lock:
            for s in self.streams.values():
                s.stop()
            self.streams.clear()
