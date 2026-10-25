#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  Bluetooth Bridge — Linux Installer
#
#  Typical first install:
#    curl -fsSL https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/linux/install.sh | bash
#
#  Non-interactive first install:
#    curl -fsSL .../install.sh | bash -s -- --yes --windows-ip 192.168.1.101
#
#  Update an existing install (keeps .env and settings):
#    bash linux/install.sh --update
#
#  Run `bash linux/install.sh --help` for every option.
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

VERSION="1.2.0"
INSTALL_DIR="${BLUETOOTH_BRIDGE_DIR:-$HOME/.bluetooth-bridge}"
REPO_BASE="https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main"
DEFAULT_EXTERNAL_PORT=9999
EXTERNAL_PORT="$DEFAULT_EXTERNAL_PORT"
COMPOSE_FILE="$INSTALL_DIR/docker-compose.yml"
ENV_FILE="$INSTALL_DIR/.env"
HELPER="$INSTALL_DIR/bluetooth-bridge"

RED='\033[0;31m';  GREEN='\033[0;32m';  YELLOW='\033[1;33m'
CYAN='\033[0;36m';  BOLD='\033[1m';    DIM='\033[2m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[*]${RESET} $*"; }
success() { echo -e "${GREEN}[✓]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[!]${RESET} $*"; }
err()     { echo -e "${RED}[✗]${RESET} $*" >&2; }
step()    { echo; echo -e "${BOLD}$1${RESET}"; }

usage() {
    cat <<'EOF'
Bluetooth Bridge — Linux installer

Usage:
  bash install.sh [options] [WINDOWS_IP] [CONTROLLER_MAC]

Options:
  -h, --help                 Show this help and exit
      --version              Print the installer version and exit
  -y, --yes                  Accept safe defaults; do not ask questions
      --non-interactive      Same as --yes --no-pair
      --update               Update an existing install, keeping .env
      --reconfigure          Ask for settings again on an existing install
      --pair                 Scan/pair the controller during install
      --no-pair              Skip controller pairing (default on updates)
      --dry-run              Check settings and prerequisites; change nothing
      --windows-ip ADDRESS   Windows PC IP, hostname, or "auto"
      --windows-port PORT    TCP port the Windows app listens on (default: 9999)
      --controller-mac MAC   Controller Bluetooth MAC (AA:BB:CC:DD:EE:FF)

Environment:
  PC_HOST, PC_PORT           Windows address and listen port
  CONTROLLER_MAC             Controller Bluetooth MAC
  BLUETOOTH_BRIDGE_DIR       Install directory (default: ~/.bluetooth-bridge)

Examples:
  curl -fsSL <installer> | bash
  curl -fsSL <installer> | bash -s -- --yes --windows-ip 192.168.1.101
  bash install.sh --update
  bash install.sh --reconfigure --pair
EOF
}

MODE="install"
RECONFIGURE=false
ASSUME_YES=false
PAIR_CHOICE="ask"
DRY_RUN=false
PC_ARG="${PC_HOST:-}"
MAC_ARG="${CONTROLLER_MAC:-}"
PORT_ARG="${PC_PORT:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)          usage; exit 0 ;;
        --version)          echo "$VERSION"; exit 0 ;;
        -y|--yes)           ASSUME_YES=true; shift ;;
        --non-interactive)  ASSUME_YES=true; PAIR_CHOICE="no"; shift ;;
        --update)           MODE="update"; shift ;;
        --reconfigure)      RECONFIGURE=true; shift ;;
        --pair)             PAIR_CHOICE="yes"; shift ;;
        --no-pair)          PAIR_CHOICE="no"; shift ;;
        --dry-run)          DRY_RUN=true; shift ;;
        --windows-ip)
            [[ $# -ge 2 ]] || { err "--windows-ip needs an address."; exit 2; }
            PC_ARG="$2"; shift 2 ;;
        --windows-port)
            [[ $# -ge 2 ]] || { err "--windows-port needs a number."; exit 2; }
            PORT_ARG="$2"; shift 2 ;;
        --controller-mac)
            [[ $# -ge 2 ]] || { err "--controller-mac needs a MAC address."; exit 2; }
            MAC_ARG="$2"; shift 2 ;;
        --) shift; break ;;
        -*) err "Unknown option: $1"; echo; usage; exit 2 ;;
        *)
            if [[ -z "$PC_ARG" ]]; then
                PC_ARG="$1"
            elif [[ -z "$MAC_ARG" ]]; then
                MAC_ARG="$1"
            else
                err "Unexpected argument: $1"; usage; exit 2
            fi
            shift ;;
    esac
done

# Remaining values after `--` are positional.
for arg in "$@"; do
    if [[ -z "$PC_ARG" ]]; then PC_ARG="$arg"
    elif [[ -z "$MAC_ARG" ]]; then MAC_ARG="$arg"
    else err "Unexpected argument: $arg"; exit 2
    fi
done

# ── Prompt safety ─────────────────────────────────────────────────────────────
# `curl | bash` consumes stdin as the script.  Read from the terminal directly
# when one is available, and fall back to defaults in headless installs.

PROMPT_FD=""
if [[ -t 0 ]]; then
    PROMPT_FD=0
elif { exec 3</dev/tty; } 2>/dev/null; then
    PROMPT_FD=3
fi

ask() {
    local prompt="$1" default="${2:-}"
    REPLY="$default"
    [[ "$ASSUME_YES" == true ]] && return 0
    [[ -z "$PROMPT_FD" ]] && return 0
    printf '%s' "$prompt" >&2
    local answer=""
    IFS= read -r -u "$PROMPT_FD" answer || answer=""
    [[ -n "$answer" ]] && REPLY="$answer"
    return 0
}

confirm() {
    local prompt="$1" default="${2:-n}"
    if [[ "$ASSUME_YES" == true && "$default" == "y" ]]; then
        return 0
    fi
    ask "$prompt" "$default"
    case "${REPLY,,}" in
        y|yes) return 0 ;;
        *) return 1 ;;
    esac
}

valid_host() {
    local value="$1"
    [[ "$value" == "auto" ]] && return 0
    [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]
}

valid_mac() {
    [[ "$1" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]]
}

install_helper() {
    # Replace atomically: `bluetooth-bridge update` may be running this
    # very file while the installer copies a new one over it.
    local source="$1"
    [[ -f "$source" ]] || return 0
    cp "$source" "$HELPER.new"
    chmod +x "$HELPER.new"
    mv -f "$HELPER.new" "$HELPER"
}

echo ""
echo -e "  ${BOLD}Bluetooth Bridge${RESET}  ${DIM}Linux installer v$VERSION${RESET}"
echo -e "  ${DIM}Your controller stays on Linux. Windows sees a real Xbox pad.${RESET}"
echo ""

# ── 1. Preflight ──────────────────────────────────────────────────────────────

step "1/7  Checking this machine"

REPO_LOCAL=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    _candidate="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
    if [[ -d "$_candidate/linux/src" ]]; then
        REPO_LOCAL="$_candidate"
    fi
fi

if ! command -v docker &>/dev/null; then
    err "Docker is not installed."
    info "Install Docker Engine and the Compose plugin:"
    info "  https://docs.docker.com/engine/install/"
    exit 1
fi

DOCKER_CMD=(docker)
if docker info &>/dev/null 2>&1; then
    DOCKER_LABEL="docker"
elif sudo -n docker info &>/dev/null 2>&1; then
    DOCKER_CMD=(sudo docker)
    DOCKER_LABEL="sudo docker"
    warn "Docker needs sudo. This installer will use 'sudo docker'."
    info "To avoid sudo later: sudo usermod -aG docker \$USER, then log out and back in."
else
    err "Cannot reach the Docker daemon as this user."
    info "Add yourself to the docker group, then log out and back in:"
    info "  sudo usermod -aG docker \$USER"
    info "Or run the installer with sudo."
    exit 1
fi

if ! "${DOCKER_CMD[@]}" compose version &>/dev/null 2>&1; then
    err "The Docker Compose plugin is missing."
    info "Install it from: https://docs.docker.com/compose/install/linux/"
    exit 1
fi
success "Docker: $("${DOCKER_CMD[@]}" --version | awk '{print $3}')  (compose plugin available)"

command -v curl &>/dev/null || { err "curl is required for the management API and updates."; info "Install it with: sudo apt install curl"; exit 1; }

if [[ -z "$REPO_LOCAL" ]]; then
    command -v unzip &>/dev/null || { err "unzip is required to unpack the bridge source."; info "Install it with: sudo apt install unzip"; exit 1; }
    success "Download tools: curl, unzip"
else
    success "Source: local checkout at $REPO_LOCAL"
fi

if command -v bluetoothctl &>/dev/null; then
    success "BlueZ: $(bluetoothctl --version 2>/dev/null | head -1)"
else
    warn "bluez/bluetoothctl was not found. Install it with: sudo apt install bluez"
fi

if compgen -G "/dev/input/event*" >/dev/null; then
    success "Input devices: /dev/input/event* nodes are present"
else
    warn "/dev/input has no event nodes. The container will start, but it cannot read the controller."
    info "Pair the controller first, or run the repair helper after install."
fi

# ── 2. Settings ───────────────────────────────────────────────────────────────

step "2/7  Choosing settings"

get_env() {
    local key="$1"
    [[ -f "$ENV_FILE" ]] || return 0
    sed -n "s/^${key}=//p" "$ENV_FILE" | tail -1
}

INSTALL_EXISTS=false
[[ -f "$ENV_FILE" && -f "$COMPOSE_FILE" ]] && INSTALL_EXISTS=true
if [[ "$MODE" == "update" && "$INSTALL_EXISTS" != true ]]; then
    warn "No existing install found at $INSTALL_DIR. Running a fresh install instead."
    MODE="install"
fi

EXISTING_PC="$(get_env PC_HOST)"
EXISTING_PC_PORT="$(get_env PC_PORT)"
EXISTING_MAC="$(get_env CONTROLLER_MAC)"
EXISTING_LEVEL="$(get_env LOG_LEVEL)"
EXISTING_TOKEN="$(get_env MANAGEMENT_TOKEN)"
EXISTING_MGMT_PORT="$(get_env MANAGEMENT_PORT)"

KEEP_SETTINGS=false
if [[ "$INSTALL_EXISTS" == true && "$RECONFIGURE" != true && "$MODE" == "update" && -z "$PC_ARG" && -z "$MAC_ARG" && -z "$PORT_ARG" ]]; then
    KEEP_SETTINGS=true
    success "Existing install found. Keeping .env settings."
    info "Use --reconfigure if you want to change the Windows address or controller."
elif [[ "$INSTALL_EXISTS" == true && "$RECONFIGURE" != true && -z "$PC_ARG" && -z "$MAC_ARG" && -z "$PORT_ARG" ]]; then
    # A plain re-install should be safe too: keep what already works.
    KEEP_SETTINGS=true
    success "Existing settings found. Keeping them (use --reconfigure to change)."
fi

if [[ "$KEEP_SETTINGS" == true ]]; then
    PC_HOST="${PC_ARG:-$EXISTING_PC}"
    CONTROLLER_MAC="${MAC_ARG:-$EXISTING_MAC}"
else
    PC_HOST="${PC_ARG:-$EXISTING_PC}"
    CONTROLLER_MAC="${MAC_ARG:-$EXISTING_MAC}"

    if [[ -z "$PC_ARG" && -n "$PROMPT_FD" ]]; then
        echo ""
        info "The Windows PC is where the virtual Xbox controller appears."
        info "Choose 'auto' to let the app find it over the network."
        ask "Windows PC address [auto] (default: ${PC_HOST:-auto}): " "${PC_HOST:-auto}"
        PC_HOST="$REPLY"
    fi
    PC_HOST="${PC_HOST:-auto}"

    if ! valid_host "$PC_HOST"; then
        warn "That does not look like a valid IP or hostname: $PC_HOST"
        PC_HOST="auto"
        info "Falling back to auto-discovery."
    fi

    DETECTED_MAC=""
    if command -v bluetoothctl &>/dev/null; then
        DETECTED_MAC="$(bluetoothctl devices 2>/dev/null | grep -i "xbox" | awk '{print $2}' | head -1 || true)"
    fi

    if [[ -z "$CONTROLLER_MAC" && -n "$DETECTED_MAC" ]]; then
        CONTROLLER_MAC="$DETECTED_MAC"
    fi
    if [[ -z "$MAC_ARG" && -n "$PROMPT_FD" ]]; then
        local_default="$CONTROLLER_MAC"
        echo ""
        if [[ -n "$DETECTED_MAC" ]]; then
            info "Found a paired Xbox controller: $DETECTED_MAC"
        else
            info "Controller MAC is optional. Leave it blank to auto-detect at runtime."
        fi
        ask "Controller MAC [${local_default:-auto}] (default: ${local_default:-auto}): " "${local_default:-}"
        CONTROLLER_MAC="$REPLY"
    fi

    if [[ -n "$CONTROLLER_MAC" ]] && ! valid_mac "$CONTROLLER_MAC"; then
        warn "Invalid controller MAC: $CONTROLLER_MAC"
        CONTROLLER_MAC=""
        info "Falling back to controller auto-discovery."
    fi
fi

PC_HOST="${PC_HOST:-auto}"
CONTROLLER_MAC="${CONTROLLER_MAC:-}"
LOG_LEVEL="${LOG_LEVEL:-${EXISTING_LEVEL:-INFO}}"
MANAGEMENT_PORT="${MANAGEMENT_PORT:-${EXISTING_MGMT_PORT:-9988}}"
MANAGEMENT_TOKEN="${MANAGEMENT_TOKEN:-$EXISTING_TOKEN}"

if [[ "$KEEP_SETTINGS" == true ]]; then
    EXTERNAL_PORT="${EXISTING_PC_PORT:-$DEFAULT_EXTERNAL_PORT}"
else
    EXTERNAL_PORT="${PORT_ARG:-${EXISTING_PC_PORT:-$DEFAULT_EXTERNAL_PORT}}"
    if ! [[ "$EXTERNAL_PORT" =~ ^[0-9]+$ ]] || (( EXTERNAL_PORT < 1 || EXTERNAL_PORT > 65535 )); then
        warn "Invalid Windows port: $EXTERNAL_PORT. Using $DEFAULT_EXTERNAL_PORT."
        EXTERNAL_PORT="$DEFAULT_EXTERNAL_PORT"
    fi
fi

if [[ "$PC_HOST" == "auto" ]]; then
    success "Windows PC: auto-discovery"
    info "Start the Windows app before using the bridge; it broadcasts its address."
elif command -v ping &>/dev/null && ! ping -c 1 -W 1 "$PC_HOST" &>/dev/null; then
    warn "Windows PC: $PC_HOST (no ping reply)"
    info "That is fine if ICMP is blocked. The bridge will keep retrying."
else
    success "Windows PC: $PC_HOST:$EXTERNAL_PORT"
fi

if [[ -n "$CONTROLLER_MAC" ]]; then
    success "Controller: $CONTROLLER_MAC"
else
    success "Controller: auto-detect"
fi

if [[ "$DRY_RUN" == true ]]; then
    echo ""
    success "Dry run complete. No files or containers were changed."
    echo "    Remove --dry-run to install with these settings."
    exit 0
fi

# ── 3. Create install directory ───────────────────────────────────────────────

step "3/7  Configuration and source"

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

CMD ["python", "-m", "src.main"]
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
    # /dev/input and /dev/hidraw are mounted both as directory binds
    # (so any new device nodes created by udev after start are visible)
    # and as explicit device binds (so the controller's hidraw node is
    # always present, even before the device shows up). They must be
    # writable: EV_FF rumble is played by writing to /dev/input/eventN.
    # Rumble will silently no-op if no node exists for the controller.
    volumes:
      - .:/config:rw
      - /var/run/dbus:/var/run/dbus:ro
      - /run/dbus:/run/dbus:ro
      - /dev/input:/dev/input:rw
      - /dev/hidraw:/dev/hidraw:rw
    devices:
      - /dev/hidraw0:/dev/hidraw0:rwm
      - /dev/hidraw1:/dev/hidraw1:rwm
      - /dev/hidraw2:/dev/hidraw2:rwm
      - /dev/hidraw3:/dev/hidraw3:rwm
      - /dev/hidraw4:/dev/hidraw4:rwm
      - /dev/hidraw5:/dev/hidraw5:rwm
      - /dev/hidraw6:/dev/hidraw6:rwm
      - /dev/hidraw7:/dev/hidraw7:rwm
    env_file:
      - .env
    environment:
      - LOG_FILE=/config/xbox-bridge.log
      - MANAGEMENT_PORT=${MANAGEMENT_PORT:-9988}
      - MANAGEMENT_CONFIG_DIR=/config
      - MANAGEMENT_TOKEN=${MANAGEMENT_TOKEN:-}
YML

# .env
if [[ "$KEEP_SETTINGS" == true ]]; then
    info "Keeping the existing .env (custom values included)."
    chmod 600 "$ENV_FILE" 2>/dev/null || true
else
if [[ -f "$ENV_FILE" ]]; then
    backup="$ENV_FILE.bak-$(date +%Y%m%d-%H%M%S)"
    cp "$ENV_FILE" "$backup"
    info "Previous settings saved to $backup"
fi
cat > "$ENV_FILE" <<EOF
# Bluetooth Bridge Linux settings
#
# The Windows PC address. Use "auto" to follow the Windows app's
# UDP discovery broadcast (start Windows first), or set an IP/hostname.
PC_HOST=$PC_HOST
PC_PORT=$EXTERNAL_PORT

# Optional controller Bluetooth MAC (AA:BB:CC:DD:EE:FF).
# Blank means "find the first paired Xbox controller at runtime".
CONTROLLER_MAC=$CONTROLLER_MAC

# INFO is the normal choice. Use DEBUG when diagnosing input/rumble.
LOG_LEVEL=$LOG_LEVEL
LOG_FILE=/config/xbox-bridge.log

# Local management API used by the Windows dashboard diagnostics.
MANAGEMENT_PORT=$MANAGEMENT_PORT
MANAGEMENT_TOKEN=$MANAGEMENT_TOKEN
EOF
chmod 600 "$ENV_FILE" 2>/dev/null || true
fi

if [[ "$KEEP_SETTINGS" == true ]]; then
    success "Existing .env kept."
else
    success ".env written to $ENV_FILE"
fi

info "Preparing requirements.txt …"
if [[ -n "$REPO_LOCAL" && -f "$REPO_LOCAL/linux/requirements.txt" ]]; then
    cp "$REPO_LOCAL/linux/requirements.txt" "$INSTALL_DIR/requirements.txt"
else
    curl -fsSL "$REPO_BASE/linux/requirements.txt" -o "$INSTALL_DIR/requirements.txt" 2>/dev/null || \
        cat > "$INSTALL_DIR/requirements.txt" <<'REQ'
evdev>=1.7.1 ; sys_platform != 'win32'
REQ
fi

# Copy source files
info "Deploying source files …"
mkdir -p "$INSTALL_DIR/src"

# The old sources are removed first so that a re-install cannot leave
# stale files behind.
rm -rf "$INSTALL_DIR/src"
mkdir -p "$INSTALL_DIR/src" "$INSTALL_DIR/tools"

if [[ -n "$REPO_LOCAL" ]]; then
    # Running from a checkout: deploy exactly these files.  Downloading
    # `main` here would silently overwrite local fixes with an older
    # release.
    info "Using local checkout: $REPO_LOCAL"
    cp -r "$REPO_LOCAL/linux/src/"* "$INSTALL_DIR/src/"
    cp "$REPO_LOCAL/linux/requirements.txt" "$INSTALL_DIR/requirements.txt"
    if [[ -d "$REPO_LOCAL/tools" ]]; then
        cp -r "$REPO_LOCAL/tools/"* "$INSTALL_DIR/tools/"
    fi
    if [[ -f "$REPO_LOCAL/linux/repair_controller.sh" ]]; then
        cp "$REPO_LOCAL/linux/repair_controller.sh" "$INSTALL_DIR/"
        chmod +x "$INSTALL_DIR/repair_controller.sh"
    fi
    install_helper "$REPO_LOCAL/linux/bluetooth-bridge"
else
    # curl | bash mode: fetch the released source tree.
    info "Downloading source from GitHub …"
    TARBALL="https://github.com/xynnpg/bluetooth-bridge/archive/refs/heads/main.zip"
    TMPZIP=$(mktemp /tmp/bridge-src.XXXXX.zip)
    if curl -fsSL "$TARBALL" -o "$TMPZIP" 2>/dev/null; then
        unzip -q "$TMPZIP" -d /tmp/
        cp -r /tmp/bluetooth-bridge-main/linux/src/* "$INSTALL_DIR/src/"
        # The diagnostic collector (merges the Linux and the Windows trace).
        if [[ -d /tmp/bluetooth-bridge-main/tools ]]; then
            cp -r /tmp/bluetooth-bridge-main/tools/* "$INSTALL_DIR/tools/"
        fi
        # The controller repair helper.
        if [[ -f /tmp/bluetooth-bridge-main/linux/repair_controller.sh ]]; then
            cp /tmp/bluetooth-bridge-main/linux/repair_controller.sh "$INSTALL_DIR/"
            chmod +x "$INSTALL_DIR/repair_controller.sh"
        fi
        install_helper /tmp/bluetooth-bridge-main/linux/bluetooth-bridge
        rm -rf /tmp/bluetooth-bridge-main
    else
        err "Could not download the source tree. Re-run from a checkout or fix network access."
        rm -f "$TMPZIP"
        exit 1
    fi
    rm -f "$TMPZIP"
fi

# ── 7. Controller pairing (optional, interactive) ────────────────────────────

step "4/7  Controller"

if [[ "$PAIR_CHOICE" == "ask" ]]; then
    if [[ "$MODE" == "update" || "$INSTALL_EXISTS" == true && "$RECONFIGURE" != true ]]; then
        PAIR_CHOICE="no"
    elif [[ -n "$PROMPT_FD" ]]; then
        if confirm "Pair/connect the controller now? [y/N] " n; then
            PAIR_CHOICE="yes"
        else
            PAIR_CHOICE="no"
        fi
    else
        PAIR_CHOICE="no"
    fi
fi

if [[ "$PAIR_CHOICE" == "yes" ]]; then
    if ! command -v bluetoothctl &>/dev/null; then
        warn "bluetoothctl is not installed; skipping pairing."
        PAIR_CHOICE="no"
    fi
fi

if [[ "$PAIR_CHOICE" == "yes" ]]; then
    BTCTL="bluetoothctl"
    if [[ $EUID -ne 0 ]] && ! "$BTCTL" show &>/dev/null 2>&1; then
        warn "Bluetooth is not available to this user. Try: sudo systemctl start bluetooth"
    fi

    if [[ -n "$CONTROLLER_MAC" ]]; then
        info "Connecting to $CONTROLLER_MAC …"
        if command -v timeout &>/dev/null; then
            timeout 20 "$BTCTL" pair "$CONTROLLER_MAC"     &>/dev/null || true
            timeout 10 "$BTCTL" trust "$CONTROLLER_MAC"    &>/dev/null || true
            timeout 20 "$BTCTL" connect "$CONTROLLER_MAC"  &>/dev/null || true
        else
            "$BTCTL" pair "$CONTROLLER_MAC"    &>/dev/null || true
            "$BTCTL" trust "$CONTROLLER_MAC"   &>/dev/null || true
            "$BTCTL" connect "$CONTROLLER_MAC" &>/dev/null || true
        fi
        success "Pairing request sent for $CONTROLLER_MAC."
    else
        info "Put the controller in pairing mode (hold the sync button until it flashes)."
        info "Scanning for 12 seconds …"
        { "$BTCTL" scan on &>/dev/null & } 2>/dev/null || true
        sleep 12
        "$BTCTL" scan off &>/dev/null || true

        CONTROLLERS="$("$BTCTL" devices 2>/dev/null | grep -i "xbox\|microsoft\|controller" || true)"
        if [[ -z "$CONTROLLERS" ]]; then
            warn "No Xbox controller found during the scan."
            info "You can pair it later from the tray/dashboard, or with:"
            info "  bluetoothctl scan on"
        else
            CONTROLLERS_COUNT="$(printf '%s\n' "$CONTROLLERS" | wc -l | tr -d ' ')"
            echo ""
            printf '%s\n' "$CONTROLLERS" | nl -v 0
            echo ""
            ask "Select a controller number [0-${CONTROLLERS_COUNT:-1}] (default: 0): " "0"
            sel="$REPLY"
            if ! [[ "$sel" =~ ^[0-9]+$ ]] || (( sel >= CONTROLLERS_COUNT )); then
                warn "Invalid selection; skipping pairing."
            else
                MAC="$(printf '%s\n' "$CONTROLLERS" | sed -n "$((sel+1))p" | awk '{print $2}')"
                if valid_mac "$MAC"; then
                    info "Pairing with $MAC …"
                    if command -v timeout &>/dev/null; then
                        timeout 20 "$BTCTL" pair "$MAC"    &>/dev/null || true
                        timeout 10 "$BTCTL" trust "$MAC"   &>/dev/null || true
                        timeout 20 "$BTCTL" connect "$MAC" &>/dev/null || true
                    else
                        "$BTCTL" pair "$MAC"    &>/dev/null || true
                        "$BTCTL" trust "$MAC"   &>/dev/null || true
                        "$BTCTL" connect "$MAC" &>/dev/null || true
                    fi
                    CONTROLLER_MAC="$MAC"
                    sed -i "s|^CONTROLLER_MAC=.*|CONTROLLER_MAC=$MAC|" "$ENV_FILE"
                    success "Controller $MAC saved."
                fi
            fi
        fi
    fi
else
    info "Skipping pairing. The bridge will connect to an already-paired controller,"
    info "or you can pair it later with: bluetoothctl"
fi

# Re-check the input device after the pairing attempt.
if grep -qi "xbox" /proc/bus/input/devices 2>/dev/null; then
    success "Xbox controller is visible as an input device."
else
    warn "No Xbox input device is visible yet."
    info "The container will keep retrying. If it never appears, re-pair it with:"
    info "  bash $INSTALL_DIR/repair_controller.sh${CONTROLLER_MAC:+ $CONTROLLER_MAC}"
fi

# ── 5/7 Build ─────────────────────────────────────────────────────────────────

step "5/7  Building the bridge"
info "This is the slow step: Docker compiles the image (usually 1–3 minutes)."

compose() {
    "${DOCKER_CMD[@]}" compose -f "$COMPOSE_FILE" "$@"
}

cd "$INSTALL_DIR"
BUILD_OK=false
for attempt in 1 2 3; do
    if compose build; then
        BUILD_OK=true
        break
    fi
    if (( attempt < 3 )); then
        warn "Build attempt $attempt failed. Retrying in 10 seconds …"
        sleep 10
    fi
done

if [[ "$BUILD_OK" != true ]]; then
    err "The Docker image could not be built after 3 attempts."
    info "Scroll up for the first error. Common causes are a failed package download"
    info "or a temporary network problem. Then run this installer again."
    exit 1
fi
success "Image built."

# ── 6/7 Start and verify ─────────────────────────────────────────────────────

step "6/7  Starting and checking the bridge"
info "Starting the container in the background …"
compose up -d --no-build --force-recreate --remove-orphans

CONTAINER_OK=false
for _ in $(seq 1 30); do
    state="$("${DOCKER_CMD[@]}" ps --filter "name=xbox-bridge" --format '{{.State}}' 2>/dev/null | head -1 || true)"
    if [[ "$state" == "running" ]]; then
        CONTAINER_OK=true
        break
    fi
    sleep 1
done

if [[ "$CONTAINER_OK" != true ]]; then
    err "The container did not stay running."
    info "Last log lines:"
    "${DOCKER_CMD[@]}" logs --tail 40 xbox-bridge 2>&1 | sed 's/^/    /' || true
    info ""
    info "Fix the first error above, then run:"
    info "  cd $INSTALL_DIR && ${DOCKER_LABEL} compose logs -f"
    exit 1
fi
success "Container is running."

# Wait for the local management API. It may take a few seconds after the
# container starts, especially on the first boot.
API_OK=false
for _ in $(seq 1 30); do
    if [[ -n "$MANAGEMENT_TOKEN" ]]; then
        curl -fsS --max-time 2 -H "X-Bridge-Token: $MANAGEMENT_TOKEN" \
            "http://127.0.0.1:$MANAGEMENT_PORT/api/status" >/dev/null 2>&1 && API_OK=true && break
    else
        curl -fsS --max-time 2 "http://127.0.0.1:$MANAGEMENT_PORT/api/status" >/dev/null 2>&1 && API_OK=true && break
    fi
    sleep 1
done
if [[ "$API_OK" == true ]]; then
    success "Management API is responding on port $MANAGEMENT_PORT."
else
    warn "The management API is not responding yet."
    info "Input may still work. Run '${BRIDGE_CMD:-$HELPER} doctor' after a few seconds."
fi

# Show only the useful startup lines instead of dumping the whole log.
SUMMARY="$("${DOCKER_CMD[@]}" logs --tail 160 xbox-bridge 2>&1 \
    | grep -E "Controller device|Rumble backend|Bridge running|Battery|No rumble backend|No Xbox controller|Discovery timeout|ERROR" \
    | tail -10 || true)"
if [[ -n "$SUMMARY" ]]; then
    echo ""
    echo -e "${DIM}  Startup summary${RESET}"
    printf '%s\n' "$SUMMARY" | sed 's/^/    /'
fi

if grep -q "No rumble backend" <<<"$SUMMARY"; then
    warn "No rumble backend was attached. Vibration will not work."
    info "Run '${BRIDGE_CMD:-$HELPER} doctor' for the exact reason."
elif grep -q "Rumble backend" <<<"$SUMMARY"; then
    success "Rumble backend attached."
fi

# ── 7/7 Auto-start and helper ────────────────────────────────────────────────

step "7/7  Auto-start and everyday commands"

# Install the helper and put it on PATH when possible.
if [[ -x "$HELPER" ]]; then
    mkdir -p "$HOME/.local/bin"
    ln -sf "$HELPER" "$HOME/.local/bin/bluetooth-bridge"
    if [[ ":$PATH:" == *":$HOME/.local/bin:"* ]]; then
        BRIDGE_CMD="bluetooth-bridge"
    else
        BRIDGE_CMD="$HOME/.local/bin/bluetooth-bridge"
    fi
    chmod +x "$HELPER"
else
    BRIDGE_CMD="$HELPER"
    warn "The helper command was not copied. Use $HELPER directly."
fi

AUTOSTART="not configured"
if command -v systemctl &>/dev/null; then
    SERVICE_DOCKER="$(command -v docker)"
    if [[ $EUID -eq 0 ]]; then
        info "Creating a systemd system service …"
        cat > /etc/systemd/system/xbox-bridge.service <<EOF
[Unit]
Description=Bluetooth Bridge (Xbox controller relay)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$INSTALL_DIR
ExecStart=$SERVICE_DOCKER compose -f $COMPOSE_FILE up -d
ExecStop=$SERVICE_DOCKER compose -f $COMPOSE_FILE down
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        if systemctl enable --now xbox-bridge; then
            AUTOSTART="systemd system service"
            success "Auto-start enabled (system service)."
        else
            warn "Could not enable the system service. Check: systemctl status xbox-bridge"
        fi
    elif [[ "${DOCKER_CMD[0]}" == "sudo" ]]; then
        warn "Docker needs sudo, so a user auto-start service is not safe to install."
        info "Fix: sudo usermod -aG docker \$USER, log out/in, then re-run with --update."
    else
        info "Creating a systemd user service …"
        if sudo -n loginctl enable-linger "$USER" &>/dev/null; then
            success "Linger enabled: the bridge starts without you logging in."
        else
            warn "Could not enable linger. The bridge will start when you log in."
        fi
        mkdir -p "$HOME/.config/systemd/user"
        cat > "$HOME/.config/systemd/user/xbox-bridge.service" <<EOF
[Unit]
Description=Bluetooth Bridge (Xbox controller relay)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$INSTALL_DIR
ExecStart=$SERVICE_DOCKER compose -f $COMPOSE_FILE up -d
ExecStop=$SERVICE_DOCKER compose -f $COMPOSE_FILE down
TimeoutStartSec=180

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        if systemctl --user enable --now xbox-bridge; then
            AUTOSTART="systemd user service"
            success "Auto-start enabled (user service)."
        else
            warn "Could not enable the user service. Check: systemctl --user status xbox-bridge"
        fi
    fi
else
    warn "systemd is not available. The container runs now, but will not start at boot."
fi

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}  Bluetooth Bridge is installed.${RESET}"
echo ""
echo -e "  ${DIM}Install${RESET}      $INSTALL_DIR"
if [[ "$PC_HOST" == "auto" ]]; then
    echo -e "  ${DIM}Windows${RESET}      auto-discovery (start the Windows app first)"
else
    echo -e "  ${DIM}Windows${RESET}      $PC_HOST:$EXTERNAL_PORT"
fi
if [[ -n "$CONTROLLER_MAC" ]]; then
    echo -e "  ${DIM}Controller${RESET}   $CONTROLLER_MAC"
else
    echo -e "  ${DIM}Controller${RESET}   auto-detect"
fi
echo -e "  ${DIM}Auto-start${RESET}   $AUTOSTART"
echo ""
echo -e "  ${BOLD}Next steps${RESET}"
echo "    1. Start the Windows app (if it is not already running)."
echo "    2. Turn on the controller and wait for the LED to stay solid."
echo "    3. Check the link and vibration:"
echo "         $BRIDGE_CMD status"
echo "         $BRIDGE_CMD test-rumble"
echo "    4. Open the dashboard on Windows: tray icon → Open Web UI."
echo ""
echo -e "  ${BOLD}Useful commands${RESET}"
echo "    $BRIDGE_CMD status       container, controller and rumble backend"
echo "    $BRIDGE_CMD doctor       diagnose a missing controller or silent rumble"
echo "    $BRIDGE_CMD logs         follow the live log"
echo "    $BRIDGE_CMD restart      restart the container"
echo "    $BRIDGE_CMD dashboard    where to open the Windows dashboard"
echo "    $BRIDGE_CMD update       update from GitHub (keeps settings)"
echo "    $BRIDGE_CMD uninstall    remove this install"
echo ""
echo -e "  ${DIM}Log${RESET}         $INSTALL_DIR/xbox-bridge.log"
echo -e "  ${DIM}Input trace${RESET} $INSTALL_DIR/input-trace.log"
echo ""
if [[ "$PC_HOST" == "auto" ]]; then
    echo -e "${YELLOW}  Auto-discovery is on. If the bridge cannot find Windows, start the${RESET}"
    echo -e "${YELLOW}  Windows app, then run: $BRIDGE_CMD restart${RESET}"
    echo ""
fi
