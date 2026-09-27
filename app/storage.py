"""Recording files on disk: listing, readiness and retention."""
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import events

NAME_RE = re.compile(r"^(\d{8}_\d{6})\.mp4$")
# A segment counts as finished once ffmpeg hasn't touched it for this long.
READY_AFTER_SECONDS = 20


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "camera"


def parse_start(filename: str) -> datetime | None:
    m = NAME_RE.match(filename)
    return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S") if m else None


def list_recordings(recordings_dir: Path, camera_ids=None, date: str | None = None) -> list[dict]:
    """All segments, newest first. `date` is YYYY-MM-DD."""
    out = []
    now = time.time()
    if not recordings_dir.exists():
        return out
    for cam_dir in recordings_dir.iterdir():
        if not cam_dir.is_dir() or not cam_dir.name.isdigit():
            continue
        cam_id = int(cam_dir.name)
        if camera_ids is not None and cam_id not in camera_ids:
            continue
        for entry in os.scandir(cam_dir):
            start = parse_start(entry.name)
            if start is None:
                continue
            if date and start.strftime("%Y-%m-%d") != date:
                continue
            st = entry.stat()
            out.append({
                "camera_id": cam_id,
                "filename": entry.name,
                "rel_path": f"{cam_id}/{entry.name}",
                "path": entry.path,
                "start": start.isoformat(),
                "end": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "size": st.st_size,
                "ready": now - st.st_mtime >= READY_AFTER_SECONDS and st.st_size > 0,
            })
    out.sort(key=lambda r: (r["start"], r["camera_id"]), reverse=True)
    return out


def recording_dates(recordings_dir: Path, camera_id: int | None = None) -> list[str]:
    dates = set()
    for r in list_recordings(recordings_dir, [camera_id] if camera_id is not None else None):
        dates.add(r["start"][:10])
    return sorted(dates, reverse=True)


def resolve(recordings_dir: Path, camera_id: int, filename: str) -> Path | None:
    """Safe path lookup for a recording (no traversal)."""
    if not NAME_RE.match(filename):
        return None
    p = recordings_dir / str(int(camera_id)) / filename
    return p if p.is_file() else None


def disk_usage(recordings_dir: Path) -> dict:
    used = sum(r["size"] for r in list_recordings(recordings_dir))
    try:
        st = os.statvfs(recordings_dir)
        free, total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    except OSError:
        free = total = 0
    return {"recordings_bytes": used, "disk_free_bytes": free, "disk_total_bytes": total}


def apply_retention(db, recordings_dir: Path) -> int:
    """Delete old segments per settings. Returns number of files removed."""
    s = db.get_settings()
    days = int(s.get("retention_days") or 0)
    max_bytes = float(s.get("max_storage_gb") or 0) * 1024 ** 3
    require_upload = bool(s.get("retention_require_upload"))
    uploaded = db.upload_map() if require_upload else {}

    recs = [r for r in list_recordings(recordings_dir) if r["ready"]]
    recs.sort(key=lambda r: r["start"])  # oldest first
    if require_upload:
        recs = [r for r in recs if uploaded.get(r["rel_path"])]

    removed = 0
    cutoff = (datetime.now() - timedelta(days=days)).isoformat() if days > 0 else None
    total = sum(r["size"] for r in list_recordings(recordings_dir))
    for r in recs:
        too_old = cutoff is not None and r["start"] < cutoff
        over_quota = max_bytes > 0 and total > max_bytes
        if not (too_old or over_quota):
            continue
        try:
            os.remove(r["path"])
            removed += 1
            total -= r["size"]
        except FileNotFoundError:
            pass
    if removed:
        events.emit("info", "retention", f"deleted {removed} old recording(s)")
    return removed
