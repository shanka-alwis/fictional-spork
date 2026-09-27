#!/usr/bin/env bash
# Installs (or updates) Big Eye as a system service on Linux Mint / Ubuntu / Debian.
#
#   sudo ./scripts/install.sh [--keep-sleep] [--no-power-restore]
#
# Re-running it updates the app and keeps your settings, password and footage.
set -euo pipefail

APP_DIR=/opt/bigeye
DATA_DIR=${BIGEYE_DATA_DIR:-/var/lib/bigeye}
ENV_FILE=/etc/bigeye.env
PORT=${BIGEYE_PORT:-8090}
KEEP_SLEEP=0
POWER_RESTORE=1
for arg in "$@"; do
  case "$arg" in
    --keep-sleep) KEEP_SLEEP=1 ;;
    --no-power-restore) POWER_RESTORE=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 $*" >&2
  exit 1
fi
RUN_USER=${SUDO_USER:-}
if [[ -z "$RUN_USER" || "$RUN_USER" == root ]]; then
  echo "Run this with sudo from your normal user account (the service runs as that user)." >&2
  exit 1
fi
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

step "Installing packages (ffmpeg, Python, smartmontools)"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg python3 python3-venv smartmontools curl unzip pciutils

step "Installing rclone"
# The distro package is often years old; the official installer gets a current release.
# It exits with code 3 when the latest version is already installed.
curl -fsSL https://rclone.org/install.sh | bash || [[ $? -eq 3 ]]
rclone version | head -1

step "Copying the app to $APP_DIR"
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/app"
cp -r "$SRC_DIR/app" "$SRC_DIR/requirements.txt" "$APP_DIR/"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

step "Preparing data folder $DATA_DIR"
mkdir -p "$DATA_DIR"
chown "$RUN_USER": "$DATA_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  step "Choosing a web login password"
  read -r -s -p "Password for the web page (user 'admin', leave empty to generate one): " PASSWORD </dev/tty || true
  echo
  GENERATED=0
  if [[ -z "${PASSWORD:-}" ]]; then
    PASSWORD=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)
    GENERATED=1
  fi
  umask 077
  cat > "$ENV_FILE" <<ENV
BIGEYE_DATA_DIR=$DATA_DIR
BIGEYE_PORT=$PORT
BIGEYE_USERNAME=admin
BIGEYE_PASSWORD=$PASSWORD
RCLONE_CONFIG=$DATA_DIR/rclone.conf
ENV
  umask 022
  [[ $GENERATED -eq 1 ]] && echo "Generated password: $PASSWORD  (stored in $ENV_FILE)"
else
  echo "Keeping existing settings in $ENV_FILE"
fi

step "Creating the bigeye service"
cat > /etc/systemd/system/bigeye.service <<UNIT
[Unit]
Description=Big Eye camera recorder
Wants=network-online.target
After=network-online.target local-fs.target

[Service]
User=$RUN_USER
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python -m app.main
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
UNIT

step "Setting up a daily disk health check"
cat > "$APP_DIR/smart-check.sh" <<'SH'
#!/bin/sh
# Writes the SMART health of every disk where the Big Eye web page can show it.
out="$1/smart-status.txt"
{
  for d in $(lsblk -dno NAME,TYPE | awk '$2 == "disk" {print $1}'); do
    echo "== /dev/$d"
    smartctl -H "/dev/$d" | sed -n '/=== START/,$p' | grep -v '^=== START' || echo "SMART not supported"
  done
} > "$out.tmp" 2>&1
mv "$out.tmp" "$out"
chmod 644 "$out"
SH
chmod 755 "$APP_DIR/smart-check.sh"
cat > /etc/systemd/system/bigeye-smart.service <<UNIT
[Unit]
Description=Big Eye disk health check

[Service]
Type=oneshot
ExecStart=$APP_DIR/smart-check.sh $DATA_DIR
UNIT
cat > /etc/systemd/system/bigeye-smart.timer <<'UNIT'
[Unit]
Description=Daily Big Eye disk health check

[Timer]
OnBootSec=5min
OnUnitActiveSec=1d
Persistent=true

[Install]
WantedBy=timers.target
UNIT

if [[ $KEEP_SLEEP -eq 0 ]]; then
  step "Disabling sleep/suspend so recording never stops (undo: systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target)"
  systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null
fi

PRODUCT=$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)
if [[ $POWER_RESTORE -eq 1 && "$PRODUCT" == Macmini* ]]; then
  step "Enabling 'power on after power failure' ($PRODUCT)"
  # Mac minis ignore the BIOS-style setting under Linux; this clears the Intel PCH's
  # AFTERG3_EN bit so the machine boots itself when power returns. Needs setting every boot.
  cat > /etc/systemd/system/bigeye-power-restore.service <<'UNIT'
[Unit]
Description=Mac mini: power on automatically after a power failure

[Service]
Type=oneshot
ExecStart=/usr/bin/setpci -s 0:1f.0 0xa4.b=0

[Install]
WantedBy=multi-user.target
UNIT
  systemctl enable --now bigeye-power-restore.service >/dev/null
fi

step "Starting services"
systemctl daemon-reload
systemctl enable --now bigeye-smart.timer >/dev/null
systemctl start bigeye-smart.service || true
systemctl enable bigeye.service >/dev/null
systemctl restart bigeye.service
sleep 2
systemctl --no-pager --lines=0 status bigeye.service || true

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<DONE

$(printf '\033[1;32m')Big Eye is running.$(printf '\033[0m')
  Web page:  http://${IP:-localhost}:$PORT   (user: admin)
  Logs:      journalctl -u bigeye -f
  Footage:   $DATA_DIR/recordings

Next: connect Google Drive (run as yourself, NOT with sudo):
  rclone config --config $DATA_DIR/rclone.conf
  then add an upload destination on the Uploads page with target  gdrive:CCTV
DONE
