# Xbox Controller Bluetooth Bridge

Stream your Bluetooth Xbox controller from a Linux server (or any Linux machine) to your Windows PC over WiFi — it appears as a real wired Xbox controller with full analog precision.

> **Install Windows side first, then Linux side.** The Windows app broadcasts its IP so Linux can find it automatically, zero-config.

---

## How it works

```
 Linux Server                           Windows PC
──────────────                          ───────────
┌──────────────────┐                   ┌──────────────────┐
│  Docker          │  TCP 24-byte pkts │  Tray App        │
│  container       │ ─────────────────►│  Receives state  │
│                  │   ~60 Hz          │        ↓         │
│  evdev reads     │  127.0.0.1:9999   │  ViGEmBus        │
│  BT controller   │                   │  (vgamepad)      │
│                  │ ◄── UDP broadcast │        ↓         │
│  bluetoothctl    │    port 9876      │  Virtual Xbox    │
│                  │    (auto-find IP) │  controller      │
└──────────────────┘                   └──────────────────┘
  systemd service                        Start Menu shortcut
  (auto-starts on boot)                  System tray
```

Auto-discovery means you don't type IPs anywhere — Windows announces itself and Linux finds it automatically.

---

## Features

- **Plug-and-play virtual Xbox 360 controller** — no game-side configuration
- **Battery level** — Linux reads the controller's battery/charging status
  over `EV_MSC` (or `ABS_MISC` as a fallback) and surfaces it on the
  Windows dashboard and tray tooltip in real time
- **Vibration / rumble** — game rumble is forwarded from the Windows host
  back to the physical controller over the same TCP connection, so games
  that vibrate the virtual pad also vibrate the real one
- **Controller identity** — the controller's name and MAC address are sent
  with every packet and shown on the dashboard
- **Live event log** — recent log lines stream into the dashboard for quick
  at-a-glance debugging without opening the log file

---

## Prerequisites

| | Linux | Windows |
|---|---|---|
| **OS** | Any distro with Docker | Windows 10/11 |
| **Bluetooth** | BlueZ, `bluetoothctl` | — |
| **Docker** | Docker + Compose | — |
| **Other** | — | ViGEmBus driver (auto-installed) |

---

## 1 — Windows (install first)

Open **PowerShell as Administrator** and run:

```powershell
irm https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/windows/installer/install.ps1 | iex
```

> Replace `user` with your GitHub username before running.

The installer will:
- Check for Python 3.8+ and install it if missing
- Download and install ViGEmBus automatically
- Install the app to `%USERPROFILE%\bluetooth_bridge`
- Register itself in **Start Menu → Startup**
- Show your local IP address and the Linux install command

You should see a **green** tray icon appear. You're ready for Linux.

---

## 2 — Linux (install second)

On your Linux server, run:

```bash
curl -fsSL https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/linux/install.sh | bash
```

Or pass your **Windows IP** and **Controller MAC** directly as arguments:

```bash
curl -fsSL https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/linux/install.sh | bash -s <WINDOWS_IP> [CONTROLLER_MAC]
# Example:
curl -fsSL https://raw.githubusercontent.com/xynnpg/bluetooth-bridge/main/linux/install.sh | bash -s 192.168.0.132 44:16:22:15:A1:31
```

The installer will:
- Check for Docker and Bluetooth
- Prompt for your **Windows PC IP address** (e.g. `192.168.0.132`)
- Prompt for your **Xbox Controller MAC address** (e.g. `44:16:22:15:A1:31` or auto-detect paired Xbox devices)
- Generate `~/.bluetooth-bridge/.env` and start the Docker container
- Auto-recreate the container whenever `.env` settings are updated

**The container auto-starts on boot** via systemd.

---

## Usage

Once both sides are installed, just:

1. Turn on your Xbox controller
2. Press the sync button to connect to the Linux server
3. Play on Windows — the controller is detected as a real Xbox gamepad

### Checking status

**Windows:** Right-click the tray icon → *Open App* for the live dashboard
(controller name, MAC, battery % with bar, current rumble motors, peer IP,
uptime, packet count, and a scrolling list of recent events). The tray
icon's hover tooltip also shows the current battery %.

**Linux:**
```bash
docker logs -f xbox-bridge
```

### Uninstalling

**Windows:** Start Menu → *Bluetooth Bridge* → *Uninstall*

**Linux:**
```bash
cd ~/.bluetooth-bridge
docker compose down
sudo rm /etc/systemd/system/xbox-bridge.service
systemctl daemon-reload
```

---

## Configuration

### Windows config (`%USERPROFILE%\bluetooth_bridge\config.ini`)

```ini
[app]
listen_port   = 9999
listen_host   = 0.0.0.0
auto_start    = true
auto_discover = true

[controller]
rumble_enabled      = true
low_battery_warn_pct = 15

[tray]
show_battery         = true
open_app_on_launch   = false
```

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `app`        | `listen_port`   | `9999`    | TCP port Windows listens on |
| `app`        | `listen_host`   | `0.0.0.0` | Bind address |
| `app`        | `auto_start`    | `true`    | Start with Windows |
| `app`        | `auto_discover` | `true`    | Broadcast IP for Linux auto-discovery (UDP 9876) |
| `controller` | `rumble_enabled`| `true`    | Forward game rumble back to the controller |
| `controller` | `low_battery_warn_pct` | `15` | Low-battery threshold (for future use) |
| `tray`       | `show_battery`  | `true`    | Show battery % in tray tooltip |
| `tray`       | `open_app_on_launch` | `false` | Pop the dashboard when the app starts |

### Linux config (`~/.bluetooth-bridge/.env`)

```env
# Windows PC IP address
PC_HOST=192.168.0.132

# TCP port — must match Windows config
PC_PORT=9999

# Pre-known controller MAC (e.g. 44:16:22:15:A1:31), or leave blank for auto-discovery
CONTROLLER_MAC=44:16:22:15:A1:31
```

> **Note:** After editing `~/.bluetooth-bridge/.env` manually, run `cd ~/.bluetooth-bridge && sudo docker compose up -d --force-recreate` so Docker re-reads the new settings.

### Manual IP override (Linux)

If auto-discovery doesn't work, set the IP manually:
```
PC_HOST=192.168.1.101
```

---

## Network Packets

Fixed 54-byte v2 binary frames at ~60 Hz — no JSON overhead. The connection is
full-duplex: state flows Linux → Windows, while game rumble flows
Windows → Linux (same socket, same packet format).

| Offset | Size | Field | Range / Notes |
|--------|------|-------|---------------|
| 0–1   | 2 | `lthumb_x` | 0–65535 (centre=32768) |
| 2–3   | 2 | `lthumb_y` | 0–65535 |
| 4–5   | 2 | `rthumb_x` | 0–65535 |
| 6–7   | 2 | `rthumb_y` | 0–65535 |
| 8     | 1 | `lt` | 0–255 |
| 9     | 1 | `rt` | 0–255 |
| 10    | 1 | `buttons_low` | A=1 B=2 X=4 Y=8 LB=16 RB=32 Back=64 Start=128 |
| 11    | 1 | `buttons_high` | L3=1 R3=2 Guide=4 |
| 12    | 1 | `dpad` | UP=1 RIGHT=2 DOWN=4 LEFT=8 (independent bits) |
| 13    | 1 | `proto` | `0x02` for v2. Receiver uses this to pick the parser. |
| 14    | 1 | `battery` | 0–100, `0xFF` = unknown |
| 15    | 1 | `flags` | bit0 = charging, bit1 = battery-valid |
| 16    | 1 | `rumble_left`  | 0–255 (large motor) |
| 17    | 1 | `rumble_right` | 0–255 (small motor) |
| 18–23 | 6 | `mac` | 6 raw bytes, e.g. `AA BB CC DD EE FF` (zeros if unknown) |
| 24–39 | 16 | `name` | ASCII, NUL-padded, truncated to 16 chars |
| 40–53 | 14 | `padding` | zeros |

Ping frame (54 × `\xff`) sent every second as a keepalive.

### Backward compatibility

If byte 13 is not `0x02`, the receiver falls back to a legacy v1 parser that
reads the first 14 bytes and treats the new fields as "unknown" — so a v1
sender will appear with battery=unknown and rumble=0 on a v2 receiver.

### Reverse channel — game rumble

When a Windows game calls `XInputSetState`, ViGEmBus notifies the bridge.
The new motor speeds are encoded as a v2 packet and sent back to the Linux
side over the same TCP socket. The Linux side opens the controller's
`/dev/hidraw*` node and writes a 13-byte Xbox One rumble output report
to make the controller physically vibrate.

`/dev/hidraw` is mounted into the Linux container by the bundled
`docker-compose.yml` — no extra configuration needed.

### Latency

| Network | Typical latency |
|---------|----------------|
| Wired 1 Gbps LAN | < 5 ms |
| WiFi | 5–15 ms |

---

## Troubleshooting

### Tray icon is red / "No controller"

1. Press the sync button on the Xbox controller
2. Wait 10 seconds for it to connect to the Linux server
3. Right-click the tray icon → *Reconnect*

### Game doesn't detect the controller

- Most PC games use **XInput** — ViGEmBus handles this.
- Some older games use DirectInput. Install `x360ce` from [x360ce.com](https://www.x360ce.com/).
- Open **Game Controllers** (`joy.cpl`) to verify.

### Linux container won't start

```bash
# Check Docker
docker --version

# Check logs
docker logs -f xbox-bridge

# If Bluetooth error:
sudo systemctl status bluetooth
sudo hciconfig  # show adapters
```

### Auto-discovery failed (Linux logs show timeout)

1. Make sure Windows is **running the app first** (before starting the Linux container)
2. Check your router supports broadcast forwarding (255.255.255.255)
3. Set the IP manually: edit `~/.bluetooth-bridge/.env` → `PC_HOST=192.168.1.101`

### Controller disconnects frequently

- Move the Linux server closer to the controller
- Some USB Bluetooth adapters have limited range — a dedicated BT 5.0 adapter helps

---

## Project Structure

```
bluetooth-bridge/
├── LICENSE
├── README.md
├── linux/
│   ├── install.sh          # curl | bash installer
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── xbox-bridge.service
│   └── src/
│       ├── main.py         # Entry point
│       ├── controller.py   # evdev → 54-byte v2 packets (battery, identity)
│       ├── network.py      # TCP client
│       ├── bluetooth.py   # bluetoothctl pairing
│       ├── rumble.py      # hidraw rumble output reports
│       └── discovery.py    # UDP auto-discovery
└── windows/
    ├── installer/
    │   ├── install.ps1      # IRM installer
    │   ├── uninstall.ps1
    │   └── assets/icon.ico
    ├── requirements.txt
    └── src/
        ├── main.py         # Entry point
        ├── receiver.py     # TCP server (v2 + v1 fallback)
        ├── emitter.py      # vgamepad → ViGEmBus (with rumble notifications)
        ├── tray.py         # System tray
        ├── ui.py           # Dashboard, Settings, Log Viewer (Tk)
        ├── state.py        # Thread-safe live state shared by tray/UI
        └── discovery.py    # UDP broadcaster
```

---

## Security

- The `.env` file contains your PC's IP — keep it private.
- No authentication on the TCP stream — **use only on a trusted LAN**.
- For exposure outside your LAN, add a VPN tunnel.

---

## License

MIT — see [LICENSE](LICENSE).