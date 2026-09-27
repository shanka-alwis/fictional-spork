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


def _disk_percent(path: Path) -> tuple[float, int, int]:
    """(used percent, free bytes, total bytes) of the filesystem holding `path`."""
    st = os.statvfs(path)
    free, total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    return (100.0 * (1 - free / total) if total else 0.0), free, total


_last_full_warning = 0.0


def apply_retention(db, recordings_dir: Path, now: datetime | None = None, disk_percent=None) -> int:
    """Delete local segments per settings. Returns number of files removed.

    1. Footage uploaded to any destination is deleted `local_keep_days_after_upload` days later.
    2. Footage older than `max_age_days` is deleted even if not uploaded (0 = never).
    3. If the disk is fuller than `max_disk_percent`, the oldest *uploaded* footage goes first.
       Footage that hasn't been uploaded is never removed by this rule.
    """
    global _last_full_warning
    now = now or datetime.now()
    disk_percent = disk_percent or _disk_percent
    s = db.get_settings()
    keep = timedelta(days=float(s.get("local_keep_days_after_upload") or 0))
    max_age = float(s.get("max_age_days") or 0)
    max_pct = float(s.get("max_disk_percent") or 0)
    uploaded = db.first_upload_times()

    recs = [r for r in list_recordings(recordings_dir) if r["ready"]]
    recs.sort(key=lambda r: r["start"])  # oldest first
    age_cutoff = (now - timedelta(days=max_age)).isoformat() if max_age > 0 else None

    removed = 0
    remaining = []
    for r in recs:
        up = uploaded.get(r["rel_path"])
        expired = up is not None and now - datetime.fromisoformat(up) >= keep
        too_old = age_cutoff is not None and r["start"] < age_cutoff
        if (expired or too_old) and _remove(r["path"]):
            removed += 1
        else:
            remaining.append(r)

    if max_pct > 0:
        pct, free, total = disk_percent(recordings_dir)
        if pct > max_pct:
            for r in remaining:
                if pct <= max_pct:
                    break
                if r["rel_path"] in uploaded and _remove(r["path"]):
                    removed += 1
                    free += r["size"]
                    pct = 100.0 * (1 - free / total)
            if pct > max_pct and time.time() - _last_full_warning > 3600:
                _last_full_warning = time.time()
                events.emit("warning", "retention",
                            f"disk is {pct:.0f}% full and the rest of the footage hasn't been uploaded yet")
    if removed:
        events.emit("info", "retention", f"deleted {removed} local recording(s)")
    return removed


def _remove(path) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def daily_rates(recordings_dir: Path) -> dict[int, float]:
    """Measured bytes/day per camera, from the last 48h of segments."""
    since = (datetime.now() - timedelta(hours=48)).isoformat()
    size: dict[int, int] = {}
    secs: dict[int, float] = {}
    for r in list_recordings(recordings_dir):
        if r["start"] < since or not r["ready"]:
            continue
        dur = (datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"])).total_seconds()
        if dur <= 0:
            continue
        size[r["camera_id"]] = size.get(r["camera_id"], 0) + r["size"]
        secs[r["camera_id"]] = secs.get(r["camera_id"], 0) + dur
    return {cam: size[cam] / secs[cam] * 86400 for cam in size if secs[cam] >= 60}
