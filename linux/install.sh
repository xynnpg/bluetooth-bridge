#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  Bluetooth Bridge — Linux Installer
#  Usage:
#    curl -fsSL https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/linux/install.sh | bash
#    curl -fsSL https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/linux/install.sh | bash -s 192.168.1.101
#
#  Replace 'user' with your GitHub username before running.
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────

RED='\033[0;31m';  GREEN='\033[0;32m';  YELLOW='\033[1;33m'
CYAN='\033[0;36m';  BOLD='\033[1m';    RESET='\033[0m'

info()    { echo -e "${CYAN}[*]${RESET} $*"; }
success() { echo -e "${GREEN}[✓]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[!]${RESET} $*"; }
err()     { echo -e "${RED}[✗]${RESET} $*" >&2; }

# ── Constants ─────────────────────────────────────────────────────────────────

INSTALL_DIR="$HOME/.bluetooth-bridge"
REPO_BASE="https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main"
APP_NAME="bluetooth-bridge"
CONTROLLER_NAME="xbox-bridge"
EXTERNAL_PORT=9999   # port on Windows PC

# ── Banner ────────────────────────────────────────────────────────────────────

cat <<'EOF'

  ╔════════════════════════════════════════════════╗
  ║    Bluetooth Bridge — Linux Installer         ║
  ╚════════════════════════════════════════════════╝
  Stream your Xbox controller from Linux → Windows
EOF
echo ""

# ── 1. Prerequisites ──────────────────────────────────────────────────────────

info "Checking prerequisites …"

if ! command -v docker &>/dev/null; then
    err "Docker is not installed."
    info "Install Docker:  https://docs.docker.com/engine/install/"
    info "Then re-run this installer."
    exit 1
fi
success "Docker: $(docker --version | cut -d' ' -f3)"

# Work out whether we need to prefix docker commands with sudo.
# Order of preference: already root → plain docker → sudo docker
if docker info &>/dev/null 2>&1; then
    DOCKER="docker"
elif sudo -n docker info &>/dev/null 2>&1; then
    DOCKER="sudo docker"
    warn "Docker requires sudo — using 'sudo docker' for all Docker commands."
else
    err "Cannot reach the Docker daemon as this user."
    info "Fix option A (recommended): add yourself to the docker group and log out/in:"
    info "  sudo usermod -aG docker \$USER && newgrp docker"
    info "Fix option B: run the installer as root:"
    info "  curl -fsSL <url> > /tmp/install.sh && sudo bash /tmp/install.sh $1"
    exit 1
fi

if ! command -v curl &>/dev/null; then
    err "curl is not installed."
    exit 1
fi

# ── 2. Windows IP ─────────────────────────────────────────────────────────────

if [[ -n "${1:-}" ]]; then
    PC_HOST="$1"
    success "Using Windows IP from command line: $PC_HOST"
elif [[ -n "${PC_HOST:-}" ]]; then
    PC_HOST="$PC_HOST"
    success "Using Windows IP from environment: $PC_HOST"
else
    # Auto-detect and verify
    DEFAULT_IP=$(ip route | grep default | awk '{print $3}' | head -1)
    info "Default network gateway: $DEFAULT_IP"
    echo ""
    echo -n "Enter your Windows PC's IP address${DEFAULT_IP:+. (default: $DEFAULT_IP): }: "
    read -r PC_HOST
    PC_HOST="${PC_HOST:-$DEFAULT_IP}"
fi

PC_HOST="${PC_HOST:-}"
if [[ -z "$PC_HOST" ]]; then
    err "No Windows IP address set. Run again with:"
    err "  curl -fsSL $REPO_BASE/install.sh | bash -s 192.168.1.101"
    exit 1
fi

success "Windows IP: $PC_HOST:$EXTERNAL_PORT"

# ── 3. Create install directory ───────────────────────────────────────────────

info "Creating install directory: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# ── 4. Download / generate config files ───────────────────────────────────────

info "Generating configuration …"

# Dockerfile
cat > "$INSTALL_DIR/Dockerfile" <<'DOCKERFILE'
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez dbus linux-libc-dev build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY src/ /app/src/
WORKDIR /app

# Discovery broadcast — send once on container start so Linux can find Windows
CMD ["sh", "-c", \
     "python -m src.main & sleep 2 && python -c \"import socket; s=socket.socket(2,2); s.setsockopt(1,10,1); s.sendto(b'BRIDGE_HELLO:$(hostname -I | awk '{print $1}'):9999',('<broadcast>',9876)); s.close()\" & wait"]
DOCKERFILE

# docker-compose.yml
cat > "$INSTALL_DIR/docker-compose.yml" <<YML
services:
  xbox-bridge:
    build: .
    container_name: xbox-bridge
    restart: unless-stopped
    network_mode: host
    privileged: true
    volumes:
      - /var/run/dbus:/var/run/dbus:ro
      - /run/dbus:/run/dbus:ro
      - /dev/input:/dev/input:ro
    env_file:
      - .env
YML

# .env
cat > "$INSTALL_DIR/.env" <<EOF
# Replace 'auto' with your Windows IP if auto-discovery fails
# PC_HOST=auto           # uses UDP broadcast discovery (Windows must be running first)
PC_HOST=$PC_HOST
PC_PORT=$EXTERNAL_PORT

# Leave blank to auto-discover controller, or set MAC (AA:BB:CC:DD:EE:FF)
CONTROLLER_MAC=

LOG_LEVEL=INFO
EOF

success ".env written to $INSTALL_DIR/.env"

# requirements.txt (extract from repo)
info "Downloading requirements.txt …"
curl -fsSL "$REPO_BASE/linux/requirements.txt" -o "$INSTALL_DIR/requirements.txt" 2>/dev/null || \
    cat > "$INSTALL_DIR/requirements.txt" <<'REQ'
evdev>=3.1 ; sys_platform != 'win32'
REQ

# Copy source files
info "Deploying source files …"
mkdir -p "$INSTALL_DIR/src"

# Download source from GitHub
TARBALL="https://github.com/xynnpg/bluetooth-bridge/archive/refs/heads/main.zip"
TMPZIP=$(mktemp /tmp/bridge-src.XXXXX.zip)
if curl -fsSL "$TARBALL" -o "$TMPZIP" 2>/dev/null; then
    unzip -q "$TMPZIP" -d /tmp/
    cp -r /tmp/bluetooth-bridge-main/linux/src/* "$INSTALL_DIR/src/"
    rm -rf /tmp/bluetooth-bridge-main
fi
rm -f "$TMPZIP"

# If download failed, copy from the repo directory
REPO_LOCAL="/home/xynnpg/Projects/bluetooth-bridge"
if [[ -d "$REPO_LOCAL/linux/src" ]] && [[ ! -f "$INSTALL_DIR/src/main.py" ]]; then
    warn "GitHub download failed — using local repo files."
    cp -r "$REPO_LOCAL/linux/src/"* "$INSTALL_DIR/src/"
fi

# ── 5. Bluetooth check ────────────────────────────────────────────────────────

info "Checking Bluetooth …"

if command -v bluetoothctl &>/dev/null; then
    if bluetoothctl show &>/dev/null; then
        success "Bluetooth adapter detected."
    else
        warn "Bluetooth daemon may not be running. Try: sudo systemctl start bluetooth"
    fi
else
    warn "BlueZ not installed. Install with: sudo apt install bluez"
fi

# ── 6. Permissions check ──────────────────────────────────────────────────────

info "Checking /dev/input permissions …"
if [[ -r /dev/input ]] && [[ -d /dev/input ]]; then
    success "Can read /dev/input."
else
    warn "Cannot read /dev/input — you may need to be in the 'input' group."
    info "  sudo usermod -aG input \$USER"
fi

# ── 7. Controller pairing (interactive) ────────────────────────────────────

echo ""
info "Controller pairing — press the sync button on your Xbox controller now."
echo ""
read -p "Press Enter when ready, or skip to auto-pair later (default: skip): " ready

if [[ -n "$ready" ]]; then
    info "Scanning for Xbox controllers (12 seconds) …"
    { bluetoothctl scan on &>/dev/null & } 2>/dev/null || true
    sleep 12
    bluetoothctl scan off &>/dev/null || true

    CONTROLLERS=$(bluetoothctl devices | grep -i "xbox\|microsoft\|controller" || true)
    if [[ -n "$CONTROLLERS" ]]; then
        echo ""
        info "Found controllers:"
        echo "$CONTROLLERS" | nl -v 0
        echo ""
        read -p "Select a controller number (0-${CONTROLLERS_COUNT:-2}, default=0): " sel
        sel="${sel:-0}"
        MAC=$(echo "$CONTROLLERS" | sed -n "$((sel+1))p" | awk '{print $2}')
        if [[ -n "$MAC" ]]; then
            info "Pairing with $MAC …"
            bluetoothctl pair "$MAC"    2>/dev/null || true
            bluetoothctl trust "$MAC"    2>/dev/null || true
            bluetoothctl connect "$MAC"  2>/dev/null || true
            # Persist MAC in .env
            sed -i "s|^CONTROLLER_MAC=.*|CONTROLLER_MAC=$MAC|" "$INSTALL_DIR/.env"
            success "Controller $MAC saved to .env"
        fi
    else
        warn "No Xbox controllers found. The container will scan on first run."
    fi
fi

# ── 8. Build Docker image ─────────────────────────────────────────────────────

info "Building Docker image (first run may take ~2 minutes) …"
cd "$INSTALL_DIR"

BUILD_OK=false
for attempt in 1 2 3; do
    if $DOCKER build -t xbox-bridge . 2>&1; then
        BUILD_OK=true
        break
    fi
    if [[ $attempt -lt 3 ]]; then
        warn "Build attempt $attempt failed — retrying in 10 s …"
        sleep 10
    fi
done

if [[ "$BUILD_OK" != true ]]; then
    err "Docker build failed after 3 attempts. Check the output above."
    exit 1
fi
success "Docker image built."

# ── 9. Stop existing container ────────────────────────────────────────────────

info "Stopping any existing container …"
$DOCKER compose -f "$INSTALL_DIR/docker-compose.yml" down &>/dev/null || true

# ── 10. Start container ───────────────────────────────────────────────────────

info "Starting container in background …"
$DOCKER compose -f "$INSTALL_DIR/docker-compose.yml" up -d

sleep 3

if $DOCKER ps --filter "name=xbox-bridge" --format "{{.Names}}" | grep -q xbox-bridge; then
    success "Container 'xbox-bridge' is running."
    $DOCKER logs --tail 10 xbox-bridge
else
    err "Container failed to start. Check:"
    err "  $DOCKER logs -f xbox-bridge"
    exit 1
fi

# ── 11. Auto-start with systemd (user service) ────────────────────────────────

install_systemd_service() {
    info "Setting up systemd user service …"
    mkdir -p "$HOME/.config/systemd/user/"

    cat > "$HOME/.config/systemd/user/xbox-bridge.service" <<EOF
[Unit]
Description=Xbox Controller Bluetooth Bridge
After=network-online.target
Wants=network-online.target

[Service]
Restart=always
RestartSec=5
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/docker compose -f $INSTALL_DIR/docker-compose.yml up
ExecStop=/usr/bin/docker compose -f $INSTALL_DIR/docker-compose.yml down

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable xbox-bridge
    systemctl --user start xbox-bridge
    success "systemd service: enabled and started."
}

if command -v systemctl &>/dev/null; then
    sudo loginctl enable-linger "$USER" &>/dev/null || true
    if install_systemd_service 2>&1; then
        :
    else
        warn "Could not install systemd service (may need loginctl)."
    fi
else
    info "systemd not available — container will not auto-start on reboot."
fi

# ── Done ───────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}║      Bluetooth Bridge Linux side installed! ✓          ║${RESET}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════╝${RESET}"
echo ""
success "Installed to:     $INSTALL_DIR"
success "Container:        xbox-bridge (Docker)"
success "Windows target:   $PC_HOST:$EXTERNAL_PORT"
echo ""
echo -e "${YELLOW}  On your Windows PC, make sure the app is running, then play!${RESET}"
echo ""
echo "  Check status:   docker logs -f xbox-bridge"
echo "  Restart:        cd $INSTALL_DIR && docker compose restart"
echo "  Uninstall:      docker compose -f $INSTALL_DIR/docker-compose.yml down"
echo "                  rm -rf $INSTALL_DIR"
echo ""