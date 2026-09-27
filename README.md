# DVR: self-hosted CCTV recorder with scheduled cloud upload

A small, self-hosted network video recorder in the spirit of AgentDVR. It records
your IP cameras around the clock (or on a schedule), shows them live in the
browser, and uploads the footage to Google Drive (or any other cloud drive,
or a NAS folder) on a schedule you choose.

- **Live view**: grid of all cameras (1–4 columns). Double-click a tile for full screen.
- **Recording**: continuous, or per-camera time windows (e.g. Mon–Fri 22:00–06:00).
  Streams are saved without re-encoding (`ffmpeg -c copy`) as 1–60 minute MP4 segments,
  so CPU use stays low.
- **Recordings browser**: pick a camera and a day, then click the 24h timeline or a row to
  play. Segments play back to back. Download or delete individual clips.
- **Scheduled uploads**: any number of destinations, each with its own cron schedule and
  camera selection. Only finished segments are uploaded, each exactly once. Failed files are
  retried on the next run. You can have the local copy deleted after upload.
- **Retention**: delete local footage after N days or once it passes N GB, with an
  option to never delete footage that hasn't been uploaded yet.
- **Login**, event log and disk usage.

Uploads go through [rclone](https://rclone.org), which supports Google Drive, OneDrive,
Dropbox, Google Cloud Storage, S3/Backblaze/Wasabi, SFTP, WebDAV and more.

## Quick start (Docker)

```bash
git clone <this repo> dvr && cd dvr
# edit docker-compose.yml: set TZ and DVR_PASSWORD
docker compose up -d --build
```

Open `http://<server-ip>:8090`, sign in, then:

1. **Cameras → Add camera**: paste the camera's RTSP URL and click **Test connection**.
2. **Uploads → Add destination**: set up the drive (below), pick a schedule, click **Test**, then save.

### Connecting Google Drive

rclone needs to be authorised once. The config is stored in `data/rclone/rclone.conf`.

```bash
docker compose run --rm dvr rclone config
```

- `n` (new remote), name it `gdrive`, storage type `drive`.
- Leave client id/secret blank, or [create your own](https://rclone.org/drive/#making-your-own-client-id)
  for better upload quotas. Choose scope `1` (full access) or `3` (`drive.file`: only files rclone creates).
- When asked "Use web browser to automatically authenticate?", answer **n** because the server has no browser.
  rclone prints a command like `rclone authorize "drive" "..."`. Run it on any computer that has
  rclone and a browser, sign in to Google, then paste the token back.

Then in the web UI add a destination with type *Cloud drive via rclone* and target `gdrive:CCTV`.
Files are uploaded as `CCTV/<camera name>/<YYYY-MM-DD>/<YYYYMMDD_HHMMSS>.mp4`.

Other providers work the same way. Pick `onedrive`, `dropbox`, `s3` and so on in `rclone config`.

### Finding your camera's stream URL

Most IP cameras expose RTSP. Common formats:

| Brand | URL |
|---|---|
| Hikvision | `rtsp://user:pass@IP:554/Streaming/Channels/101` (102 = sub-stream) |
| Dahua / Amcrest | `rtsp://user:pass@IP:554/cam/realmonitor?channel=1&subtype=0` |
| Reolink | `rtsp://user:pass@IP:554/h264Preview_01_main` |
| TP-Link Tapo | `rtsp://user:pass@IP:554/stream1` (create a "camera account" in the Tapo app) |
| Generic ONVIF | check the camera's web UI, or use ONVIF Device Manager |

HTTP/MJPEG URLs and local video files also work. A local file is played on a loop, which is handy for testing.
Recordings play in the browser when the camera sends H.264. H.265/HEVC footage still records and
uploads fine, but may only play in Safari or Edge, or after downloading (e.g. in VLC).

## Running without Docker

Requires Python 3.10+, `ffmpeg` and (for cloud uploads) `rclone` on the PATH.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DVR_PASSWORD=change-me python -m app.main      # http://localhost:8090
```

## Configuration

| Env var | Default | |
|---|---|---|
| `DVR_DATA_DIR` | `./data` | Database and recordings (`recordings/<camera id>/*.mp4`) |
| `DVR_PORT` / `DVR_HOST` | `8090` / `0.0.0.0` | Web server |
| `DVR_USERNAME` / `DVR_PASSWORD` | `admin` / empty | Login. An empty password means no login is required |
| `DVR_SECRET` | generated | Session signing key (auto-saved to `data/.secret`) |
| `RCLONE_CONFIG` | rclone default | rclone config file |
| `DVR_FFMPEG` / `DVR_RCLONE` | `ffmpeg` / `rclone` | Binary paths |
| `TZ` | system | Time zone for file names and schedules |

Upload schedules use standard cron syntax (`minute hour day month weekday`). For example,
`0 2 * * *` runs every day at 02:00 and `*/30 * * * *` runs every 30 minutes.

## How it works

```
camera ──RTSP──▶ ffmpeg (-c copy, segment muxer) ──▶ data/recordings/<cam>/20260927_140000.mp4
                                                         │
     browser ◀── MJPEG live view (ffmpeg, on demand)     │  cron (APScheduler)
     browser ◀── /api/recordings (HTTP range playback)   ▼
                                               rclone copyto ──▶ gdrive:CCTV/<camera>/<date>/…
```

- `app/recorder.py` runs one ffmpeg per camera. It restarts crashed or stalled streams
  with backoff, and starts or stops recording as schedule windows open and close.
- Segments are fragmented MP4, so a power cut loses at most a few seconds and the file stays playable.
- `app/uploader.py` uploads only segments that ffmpeg has finished writing. Upload state is kept in
  SQLite, so each file is sent once per destination.
- `app/live.py` runs one low-resolution MJPEG transcode per camera for all viewers, and stops it
  when nobody is watching.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The end-to-end tests use the real `ffmpeg` and `rclone` when they're installed, and are skipped otherwise.

Not implemented yet (AgentDVR has these): motion detection and alerts, PTZ control, ONVIF discovery
and multi-user accounts.
