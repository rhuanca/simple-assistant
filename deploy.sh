#!/usr/bin/env bash
#
# Deploy the grocery bot as a systemd user service on Linux (Raspberry Pi OS, etc).
# Idempotent — safe to re-run after pulling code changes.
#
# What it does:
#   1. Verifies prerequisites (systemd, uv, .env)
#   2. Runs `uv sync`
#   3. Optionally writes a journald drop-in to cap log size at 50M (sudo)
#   4. Writes ~/.config/systemd/user/grocery-bot.service
#   5. Enables user lingering so the service starts at boot (sudo)
#   6. Enables and (re)starts the service, then prints status
#
# What it does NOT do:
#   - Read or modify your .env (you manage secrets yourself)
#   - Pull code from git (run `git pull` separately)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="grocery-bot"
UNIT_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
JOURNALD_DROPIN="/etc/systemd/journald.conf.d/${SERVICE_NAME}.conf"

step() { echo; echo "=== $1 ==="; }
err()  { echo "Error: $1" >&2; exit 1; }

# ---------- 1. preflight ----------
step "Preflight"

command -v systemctl >/dev/null || err "systemctl not found — this script requires systemd."
command -v uv        >/dev/null || err "'uv' not found. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
[[ -f "$PROJECT_DIR/.env" ]]    || err ".env not found at $PROJECT_DIR/.env. Run: cp .env.example .env && \$EDITOR .env"

UV_PATH="$(command -v uv)"
echo "Project:   $PROJECT_DIR"
echo "uv:        $UV_PATH"
echo "User:      $USER"
echo "Unit file: $UNIT_FILE"

# ---------- 2. dependencies ----------
step "Syncing dependencies (uv sync)"
(cd "$PROJECT_DIR" && uv sync)

# ---------- 3. journald drop-in (optional) ----------
step "Journald config (caps logs at 50M to spare the SD card)"
if [[ -f "$JOURNALD_DROPIN" ]]; then
    echo "Already present at $JOURNALD_DROPIN — skipping."
else
    read -rp "Write $JOURNALD_DROPIN (requires sudo)? [Y/n] " ans
    if [[ ! "$ans" =~ ^[Nn]$ ]]; then
        sudo install -d /etc/systemd/journald.conf.d
        sudo tee "$JOURNALD_DROPIN" > /dev/null <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=50M
SystemKeepFree=200M
EOF
        sudo systemctl restart systemd-journald
        echo "Journald configured."
    else
        echo "Skipped."
    fi
fi

# ---------- 4. systemd unit ----------
step "Writing $UNIT_FILE"
mkdir -p "$(dirname "$UNIT_FILE")"
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Grocery Bot Telegram service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$UV_PATH run python -m bot.main
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=default.target
EOF
echo "Wrote unit file."

# ---------- 5. linger ----------
step "User lingering (lets the service start at boot without you logged in)"
if loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
    echo "Already enabled for $USER."
else
    read -rp "Enable lingering for $USER (requires sudo)? [Y/n] " ans
    if [[ ! "$ans" =~ ^[Nn]$ ]]; then
        sudo loginctl enable-linger "$USER"
        echo "Lingering enabled."
    else
        echo "Skipped — service will only run while you're logged in."
        echo "  Run later: sudo loginctl enable-linger $USER"
    fi
fi

# ---------- 6. reload + (re)start ----------
step "Reloading systemd and starting the service"
systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}.service" >/dev/null
systemctl --user restart "${SERVICE_NAME}.service"

# Give it a moment to crash if it's going to
sleep 2

step "Status"
systemctl --user --no-pager status "${SERVICE_NAME}.service" || true

cat <<EOF

Done.

Tail live logs:
  journalctl --user -u $SERVICE_NAME -f

Restart after code changes:
  cd $PROJECT_DIR && git pull && uv sync && systemctl --user restart $SERVICE_NAME

EOF
