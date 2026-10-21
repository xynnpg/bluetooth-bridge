#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  Bluetooth Bridge — Controller repair
#
#  Use this when the controller is paired and "Connected: yes" but the
#  bridge still does not see it.  That happens when the kernel could not
#  read the controller's HID report descriptor over Bluetooth: no
#  /dev/input node is created, so no driver can bind and the bridge has
#  nothing to read.  The kernel log shows it:
#
#      microsoft 0005:045E:0B20: item fetching failed at offset 40/44
#      microsoft 0005:045E:0B20: probe with driver microsoft failed with error -22
#
#  The fix is to remove the stale pairing and pair the controller again:
#  the HID descriptor is then fetched from scratch.
#
#  Usage:
#      bash repair_controller.sh [MAC]
#      bash repair_controller.sh            # uses CONTROLLER_MAC from ~/.bluetooth-bridge/.env
#
#  You will be asked to press the SYNC button on the controller.
# ═══════════════════════════════════════════════════════════════════

set -uo pipefail

INSTALL_DIR="$HOME/.bluetooth-bridge"
MAC="${1:-}"

if [[ -z "$MAC" && -f "$INSTALL_DIR/.env" ]]; then
    MAC=$(grep -E '^CONTROLLER_MAC=' "$INSTALL_DIR/.env" | head -1 | cut -d= -f2 | tr -d '"' | tr -d "'" | xargs)
fi

info()    { echo -e "\033[0;36m[*]\033[0m $*"; }
success() { echo -e "\033[0;32m[✓]\033[0m $*"; }
warn()    { echo -e "\033[1;33m[!]\033[0m $*"; }
err()     { echo -e "\033[0;31m[✗]\033[0m $*" >&2; }

if [[ -z "$MAC" ]]; then
    err "No controller MAC given and none found in $INSTALL_DIR/.env"
    err "Usage: bash repair_controller.sh <MAC>"
    exit 1
fi

if ! command -v bluetoothctl &>/dev/null; then
    err "bluetoothctl not found (install the bluez package)."
    exit 1
fi

# ── 1. Show the current state ───────────────────────────────────────────────
info "Controller: $MAC"
bluetoothctl info "$MAC" 2>/dev/null | grep -E "^\s+(Connected|Paired|Trusted)" || true

if grep -qi "xbox" /proc/bus/input/devices 2>/dev/null; then
    success "An Xbox controller input device exists — nothing to repair."
    grep -i -A 3 "xbox" /proc/bus/input/devices | head -8
    exit 0
fi

# ── 2. Show why the kernel refused the device ──────────────────────────────
if command -v dmesg &>/dev/null; then
    warn "Kernel messages about the controller:"
    dmesg 2>/dev/null | grep -iE "0B20|parse failed|item fetching" | tail -5 | sed 's/^/    /' || true
fi

# ── 3. Remove the stale pairing ────────────────────────────────────────────
info "Removing the stale pairing …"
bluetoothctl remove "$MAC" 2>&1 | sed 's/^/    /'

# ── 4. Scan, pair, trust, connect ──────────────────────────────────────────
cat <<'EOF'

    ┌────────────────────────────────────────────────────────────┐
    │  Press and hold the SYNC button on the controller          │
    │  (top edge, next to the Xbox logo) until it flashes fast. │
    └────────────────────────────────────────────────────────────┘
EOF

info "Scanning for the controller (60 s) …"
bluetoothctl --timeout 60 scan on >/dev/null 2>&1 &
SCAN_PID=$!

FOUND=""
for _ in $(seq 1 60); do
    if bluetoothctl devices 2>/dev/null | grep -qi "${MAC}"; then
        FOUND="yes"
        break
    fi
    sleep 1
done
kill $SCAN_PID 2>/dev/null || true
bluetoothctl scan off >/dev/null 2>&1 || true

if [[ -z "$FOUND" ]]; then
    err "The controller did not show up. Make sure it is in pairing mode"
    err "(fast flashing) and try again."
    exit 1
fi
success "The controller is visible."

info "Pairing …"
bluetoothctl pair "$MAC" 2>&1 | sed 's/^/    /'
sleep 2
bluetoothctl trust "$MAC" 2>&1 | sed 's/^/    /'
sleep 1
bluetoothctl connect "$MAC" 2>&1 | sed 's/^/    /'
sleep 5

# ── 5. Verify ───────────────────────────────────────────────────────────────
echo ""
if grep -qi "xbox" /proc/bus/input/devices 2>/dev/null; then
    success "The controller is now an input device:"
    grep -i -A 3 "xbox" /proc/bus/input/devices | head -6 | sed 's/^/    /'
    echo ""
    success "The bridge picks it up within a couple of seconds."
    info "Check:  docker logs -f xbox-bridge"
    exit 0
fi

err "Still no input device. The kernel log will say why:"
if command -v dmesg &>/dev/null; then
    dmesg 2>/dev/null | grep -iE "0B20|parse failed|item fetching" | tail -5 | sed 's/^/    /'
fi
cat <<'EOF'

    If the descriptor is still incomplete, the controller's firmware or
    the Bluetooth firmware on this host is the cause.  Try, in this order:

      1.  sudo apt update && sudo apt install --reinstall linux-firmware
      2.  Update the controller's firmware (needs a Windows PC or Xbox)
      3.  Use the controller over USB (a cable) — the standard xpad
          driver then works and the bridge sees it right away
EOF
exit 1
