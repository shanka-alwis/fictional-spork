import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_RCLONE = shutil.which("rclone") is not None


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DVR_DISABLE_WORKERS", "1")
    monkeypatch.delenv("DVR_PASSWORD", raising=False)
    return Config(tmp_path / "data")


@pytest.fixture
def client(cfg):
    with TestClient(create_app(cfg)) as c:
        yield c


def make_segment(cfg, cam_id, name, size=1024, age=3600):
    """Create a fake finished recording segment."""
    import os
    import time
    d = cfg.recordings_dir / str(cam_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"\0" * size)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
                    "-t", "4", "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", str(out)], check=True)
    return out
