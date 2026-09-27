"""Cron scheduling for upload destinations and the retention sweep."""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import events, storage


def parse_cron(expr: str) -> CronTrigger:
    """Validate a 5-field crontab expression (raises ValueError)."""
    return CronTrigger.from_crontab(expr.strip())


class Jobs:
    def __init__(self, db, cfg, uploader):
        self.db = db
        self.cfg = cfg
        self.uploader = uploader
        self.scheduler = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1,
                                                           "misfire_grace_time": 3600})

    def start(self):
        self.scheduler.add_job(self.retention, "interval", minutes=10, id="retention")
        self.reload()
        self.scheduler.start()

    def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def retention(self):
        try:
            storage.apply_retention(self.db, self.cfg.recordings_dir)
        except Exception as e:
            events.emit("error", "retention", str(e))

    def reload(self):
        """Re-create one cron job per enabled destination."""
        for job in self.scheduler.get_jobs():
            if job.id.startswith("dest-"):
                job.remove()
        for dest in self.db.list_destinations():
            if not dest["enabled"]:
                continue
            try:
                trigger = parse_cron(dest["cron"])
            except ValueError as e:
                events.emit("error", f"upload:{dest['name']}", f"bad schedule '{dest['cron']}': {e}")
                continue
            self.scheduler.add_job(self.uploader.run, trigger, args=[dest["id"]], id=f"dest-{dest['id']}")

    def next_run(self, dest_id):
        job = self.scheduler.get_job(f"dest-{dest_id}")
        nrt = getattr(job, "next_run_time", None) if job else None
        return nrt.isoformat(timespec="seconds") if nrt else None
