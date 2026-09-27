# DVR: self-hosted CCTV recorder with nightly Google Drive upload

A small, self-hosted network video recorder in the spirit of AgentDVR, set up for
**Tapo cameras on a Linux Mint Mac mini**. It records every camera around the clock,
shows them live in the browser, and uploads the footage to Google Drive every night.

**Default behaviour** (all adjustable in the web page):

| | |
|---|---|
| Recording | 24/7, full quality (`stream1`), in 5-minute MP4 clips, with audio |
| Live view | low-resolution `stream2`, so a grid of 6 cameras stays light |
| Upload to Google Drive | every night at **01:00** |
| On the Mac mini | clips are deleted **1 day after they're uploaded** |
| On Google Drive | clips are deleted after **30 days** |
| Disk-full safeguard | above 85% full, the oldest *already uploaded* clips go first. Footage that isn't uploaded yet is never deleted |

Other features: recordings timeline and playback, per-camera GB/day measurement with a
Drive space estimate, disk health (SMART), login, event log.

## Install on the Mac mini (Linux Mint)

### 1. Prepare the cameras (Tapo app)

For each camera:

- **Camera settings → Advanced settings → Camera account**: create a username and password.
  This is separate from your Tapo/TP-Link login; the DVR uses it to read the video. Using the
  same camera account on all cameras makes setup quicker.
- Note its IP address (**Camera settings → Device info**), and reserve that address for it in
  your router's DHCP settings so it never changes.

### 2. Install the DVR

Plug the Mac mini into the router with an **Ethernet cable** (faster and more reliable than Wi-Fi
for the nightly upload). Then open a terminal:

```bash
sudo apt install -y git
git clone https://github.com/shanka-alwis/fictional-spork.git dvr
cd dvr
sudo ./scripts/install.sh
```

The installer:

- installs ffmpeg, rclone and Python;
- copies the app to `/opt/dvr` and footage goes to `/var/lib/dvr`;
- asks for the web page password (user `admin`), stored in `/etc/dvr.env`;
- creates a `dvr` service that starts at boot and restarts itself if it ever crashes;
- **turns off sleep/suspend** so recording never stops (skip with `--keep-sleep`);
- on a Mac mini, turns on **power on after a power cut** (skip with `--no-power-restore`);
- runs a daily disk health check, shown on the Settings page.

To update later: `cd dvr && git pull && sudo ./scripts/install.sh` (your settings and footage are kept).

### 3. Connect Google Drive

Run this **as yourself, without sudo**, and answer the prompts roughly as below (the exact wording
varies a little between rclone versions):

```bash
rclone config --config /var/lib/dvr/rclone.conf
```

- `n` (new remote), name: `gdrive`, storage: `drive`
- client_id / client_secret: leave blank (press Enter)
- scope: `1` (full access)
- service_account_file: blank; "Edit advanced config?": `n`
- "Use web browser to automatically authenticate?": `y`. A browser opens: sign in with the Google
  account that has the 5 TB plan and allow access
- "Configure this as a Shared Drive?": `n`, then `y` to confirm and `q` to quit

### 4. Set it up in the web page

Open `http://<mac-mini-ip>:8090` from any device on your network (the installer prints the address)
and sign in as `admin`.

1. **Cameras → Add camera**: type *TP-Link Tapo*, enter the IP address and the camera account,
   click **Test connection** (you should see a picture), then **Save**. Repeat for each camera.
2. **Uploads → Add destination**: type *Cloud drive via rclone*, target `gdrive:CCTV`, schedule
   *Daily at 01:00*, keep 30 days. Click **Test**, then **Save**.
3. Optionally click **Upload now** once to check everything reaches Drive.

Footage appears in Google Drive as `CCTV/<camera name>/<YYYY-MM-DD>/<YYYYMMDD_HHMMSS>.mp4`.

### Checking on it

- **Settings** shows GB recorded per camera per day, free disk space and disk health.
- **Uploads** shows the next and last upload, files waiting, and the Drive space 30 days will need.
- **Logs** shows camera disconnects, uploads and clean-ups.
- On the Mac mini: `systemctl status dvr` and `journalctl -u dvr -f`.

### Good to know

- **Upload speed.** Every night's upload must finish before the next one. Check the Uploads page
  after the first few nights: if "files waiting" keeps growing, your upload bandwidth is too slow
  for full quality from all cameras.
- **Tapo RTSP limits.** Tapo cameras accept only a few RTSP connections at once. The DVR uses one
  for recording and, while someone is watching, one for live view. If you also point another RTSP
  app at the same camera, one of them may be refused.
- **Codecs.** C200/C210-type cameras send H.264, which every browser plays. If a model sends H.265,
  recording and upload still work, but in-browser playback may need Safari/Edge (or download the clip).
- **Other cameras.** Choose *Other RTSP / HTTP camera* and paste the stream URL(s).

## Running with Docker instead

```bash
# edit docker-compose.yml: set TZ and DVR_PASSWORD
docker compose up -d --build
docker compose run --rm dvr rclone config   # answer "n" to the web-browser question; see rclone docs
```

## Configuration (environment variables)

Set in `/etc/dvr.env` (installer) or `docker-compose.yml`, then `sudo systemctl restart dvr`.

| Variable | Default | |
|---|---|---|
| `DVR_DATA_DIR` | `/var/lib/dvr` | Database and footage (`recordings/<camera id>/*.mp4`) |
| `DVR_PORT` / `DVR_HOST` | `8090` / `0.0.0.0` | Web server |
| `DVR_USERNAME` / `DVR_PASSWORD` | `admin` / set at install | Login. An empty password disables the login |
| `RCLONE_CONFIG` | `/var/lib/dvr/rclone.conf` | rclone config file |
| `DVR_FFMPEG` / `DVR_RCLONE` | `ffmpeg` / `rclone` | Binary paths |

## How it works

```
Tapo stream1 ──RTSP──▶ ffmpeg (copy, no re-encode) ──▶ /var/lib/dvr/recordings/<cam>/20260927_140000.mp4
Tapo stream2 ──RTSP──▶ ffmpeg → JPEG frames ──▶ live view                 │
                                                                          │ 01:00 daily
                                                    rclone copyto ◀───────┘
                                                        │
                                              gdrive:CCTV/<camera>/<date>/…  (older than 30 days deleted)
```

- `app/recorder.py` runs one ffmpeg per camera and restarts crashed or stalled streams with backoff.
- Clips are fragmented MP4, so a power cut loses only a few seconds and the file stays playable.
- `app/uploader.py` uploads each finished clip once, then removes footage older than the keep period
  from the destination. Only files matching its own `<camera>/<date>/<file>.mp4` layout are ever
  deleted there.
- `app/storage.py` deletes local clips a set time after upload, with the disk-full safeguard.
- `app/live.py` runs one live-view decoder per camera shared by all viewers, and stops it when nobody
  is watching.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The end-to-end tests use the real `ffmpeg` and `rclone` when they're installed.

**Planned (phase 2):** motion detection, motion markers in the timeline, alerts.
