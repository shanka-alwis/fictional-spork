"""Copies finished recordings to upload destinations (rclone remotes or local/mounted folders)."""
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import events, storage

RCLONE_TIMEOUT = 6 * 60 * 60
# Only files in our own layout (<camera>/<YYYY-MM-DD>/<file>.mp4) are ever deleted from a destination,
# so pointing a destination at a folder that holds other files can't lose them.
RCLONE_OWN_FILES = "/*/????-??-??/*.mp4"


class UploadError(Exception):
    pass


class Uploader:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        self._running: set[int] = set()
        self._lock = threading.Lock()

    def is_running(self, dest_id) -> bool:
        with self._lock:
            return dest_id in self._running

    # -- transports --------------------------------------------------------
    def _rclone(self, *args, timeout=RCLONE_TIMEOUT) -> subprocess.CompletedProcess:
        cmd = [self.cfg.rclone]
        if self.cfg.rclone_config:
            cmd += ["--config", self.cfg.rclone_config]
        cmd += list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise UploadError(f"rclone not found ({self.cfg.rclone})")
        except subprocess.TimeoutExpired:
            raise UploadError("rclone timed out")
        if p.returncode != 0:
            raise UploadError((p.stderr or p.stdout).strip()[-500:] or f"rclone exit {p.returncode}")
        return p

    @staticmethod
    def _join_remote(target: str, rel: str) -> str:
        return target.rstrip("/") + "/" + rel if not target.endswith(":") else target + rel

    def _copy(self, dest, src: Path, rel: str) -> str:
        if dest["type"] == "local":
            out = Path(dest["target"]) / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(out.name + ".part")
            shutil.copy2(src, tmp)  # keeps mtime, which remote cleanup ages files by
            os.replace(tmp, out)
            return str(out)
        if dest["type"] == "rclone":
            remote = self._join_remote(dest["target"], rel)
            self._rclone("copyto", str(src), remote)
            return remote
        raise UploadError(f"unknown destination type {dest['type']}")

    def test(self, dest) -> str:
        """Check the destination is reachable/writable. Returns a human message."""
        if dest["type"] == "local":
            p = Path(dest["target"])
            if not p.is_absolute():
                raise UploadError("folder must be an absolute path")
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".bigeye-write-test"
            try:
                probe.write_text("ok")
                probe.unlink()
            except OSError as e:
                raise UploadError(f"folder is not writable: {e}")
            return f"{p} is writable"
        if dest["type"] == "rclone":
            if ":" not in dest["target"]:
                raise UploadError("target must look like remote:path (see `rclone listremotes`)")
            self._rclone("mkdir", dest["target"], timeout=60)
            self._rclone("lsf", "--max-depth", "1", dest["target"], timeout=60)
            return f"connected to {dest['target']}"
        raise UploadError(f"unknown destination type {dest['type']}")

    def list_remotes(self) -> list[str]:
        try:
            return [r for r in self._rclone("listremotes", timeout=20).stdout.split() if r]
        except UploadError:
            return []

    def cleanup_remote(self, dest) -> int:
        """Delete footage older than `remote_keep_days` from the destination. Returns files removed."""
        days = int(dest.get("remote_keep_days") or 0)
        if days <= 0:
            return 0
        if dest["type"] == "local":
            root = Path(dest["target"])
            cutoff = time.time() - days * 86400
            removed = 0
            for f in root.glob("*/????-??-??/*.mp4"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            for d in root.glob("*/????-??-??"):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            return removed
        if dest["type"] == "rclone":
            p = self._rclone("delete", "-v", "--min-age", f"{days}d", "--include", RCLONE_OWN_FILES, dest["target"])
            removed = sum(1 for line in p.stderr.splitlines() if ": Deleted" in line)
            if removed:
                self._rclone("rmdirs", "--leave-root", dest["target"])
            return removed
        return 0

    # -- job ---------------------------------------------------------------
    def pending(self, dest) -> list[dict]:
        cams = dest.get("camera_ids")
        recs = storage.list_recordings(self.cfg.recordings_dir, set(cams) if cams else None)
        done = self.db.uploaded_paths(dest["id"])
        pending = [r for r in recs if r["ready"] and r["rel_path"] not in done]
        pending.sort(key=lambda r: r["start"])  # oldest first
        return pending

    def run(self, dest_id) -> dict:
        with self._lock:
            if dest_id in self._running:
                return {"skipped": True}
            self._running.add(dest_id)
        try:
            return self._run(dest_id)
        finally:
            with self._lock:
                self._running.discard(dest_id)

    def run_async(self, dest_id) -> bool:
        if self.is_running(dest_id):
            return False
        threading.Thread(target=self.run, args=(dest_id,), daemon=True).start()
        return True

    def _run(self, dest_id) -> dict:
        dest = self.db.get_destination(dest_id)
        if not dest:
            return {"skipped": True}
        cameras = {c["id"]: c for c in self.db.list_cameras()}
        ok = failed = 0
        last_error = None
        for rec in self.pending(dest):
            cam = cameras.get(rec["camera_id"])
            cam_name = storage.safe_name(cam["name"]) if cam else f"camera-{rec['camera_id']}"
            rel = f"{cam_name}/{rec['start'][:10]}/{rec['filename']}"
            try:
                remote = self._copy(dest, Path(rec["path"]), rel)
            except (UploadError, OSError) as e:
                failed += 1
                last_error = str(e)
                self.db.record_upload(dest_id, rec["rel_path"], "failed", rec["size"], error=last_error)
                events.emit("error", f"upload:{dest['name']}", f"{rec['rel_path']}: {last_error}")
                if failed >= 3 and ok == 0:
                    break  # destination is probably down; try again next run
                continue
            ok += 1
            self.db.record_upload(dest_id, rec["rel_path"], "done", rec["size"], remote_path=remote)
            if dest["delete_after_upload"]:
                try:
                    os.remove(rec["path"])
                except FileNotFoundError:
                    pass

        if failed:
            status = f"{ok} uploaded, {failed} failed: {last_error}"
        else:
            status = f"{ok} uploaded" if ok else "nothing to upload"
        cleanup_failed = False
        try:
            cleaned = self.cleanup_remote(dest)
            if cleaned:
                status += f"; removed {cleaned} file(s) older than {dest['remote_keep_days']} days"
        except (UploadError, OSError) as e:
            cleanup_failed = True
            status += f"; cleanup failed: {e}"
        self.db.set_destination_result(dest_id, status)
        events.emit("error" if failed or cleanup_failed else "info", f"upload:{dest['name']}", status)
        return {"uploaded": ok, "failed": failed}

    def pending_summary(self, dest) -> dict:
        p = self.pending(dest)
        return {"pending": len(p), "pending_bytes": sum(r["size"] for r in p)}
