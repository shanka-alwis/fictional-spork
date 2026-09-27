"""Runtime configuration, read from environment variables."""
import os
import secrets
from pathlib import Path


class Config:
    def __init__(self, data_dir: str | os.PathLike | None = None):
        self.data_dir = Path(data_dir or os.environ.get("DVR_DATA_DIR", "./data")).resolve()
        self.recordings_dir = self.data_dir / "recordings"
        self.db_path = self.data_dir / "dvr.sqlite3"
        self.ffmpeg = os.environ.get("DVR_FFMPEG", "ffmpeg")
        self.rclone = os.environ.get("DVR_RCLONE", "rclone")
        # Optional explicit rclone config file; rclone's own default is used otherwise.
        self.rclone_config = os.environ.get("RCLONE_CONFIG") or None
        self.username = os.environ.get("DVR_USERNAME", "admin")
        self.password = os.environ.get("DVR_PASSWORD", "")
        self.disable_workers = os.environ.get("DVR_DISABLE_WORKERS") == "1"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.secret_key = os.environ.get("DVR_SECRET") or self._load_secret()

    @property
    def auth_enabled(self) -> bool:
        return bool(self.password)

    def _load_secret(self) -> str:
        path = self.data_dir / ".secret"
        if path.exists():
            return path.read_text().strip()
        key = secrets.token_hex(32)
        path.write_text(key)
        path.chmod(0o600)
        return key
