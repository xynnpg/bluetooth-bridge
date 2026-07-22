# Xbox Controller Bluetooth Bridge

Stream your Bluetooth Xbox controller from a Linux server (or any Linux machine) to your Windows PC over WiFi — it appears as a real wired Xbox controller with full analog precision.

> **Install Windows side first, then Linux side.** The Windows app broadcasts its IP so Linux can find it automatically, zero-config.

---

## How it works

```
 Linux Server                           Windows PC
──────────────                          ───────────
┌──────────────────┐                   ┌──────────────────┐
│  Docker          │  TCP 24-byte pkts  │  Tray App        │
│  container       │ ─────────────────►│  Receives state  │
│                  │   ~60 Hz           │        ↓         │
│  evdev reads     │  127.0.0.1:9999   │  ViGEmBus        │
│  BT controller   │                    │  (vgamepad)      │
│                  │ ◄── UDP broadcast │        ↓         │
│  bluetoothctl    │    port 9876       │  Virtual Xbox    │
│                  │    (auto-find IP)  │  controller      │
└──────────────────┘                    └──────────────────┘
  systemd service                         Start Menu shortcut
  (auto-starts on boot)                   System tray
```

Auto-discovery means you don't type IPs anywhere — Windows announces itself and Linux finds it automatically.

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

> Replace `user` with your GitHub username before running.

The installer will:
- Check for Docker and Bluetooth
- Prompt you to select your Xbox controller (pair it if needed)
- Start the Docker container in the background
- Show the final status

**The container auto-starts on boot** via systemd.

---

## Usage

Once both sides are installed, just:

1. Turn on your Xbox controller
2. Press the sync button to connect to the Linux server
3. Play on Windows — the controller is detected as a real Xbox gamepad

### Checking status

**Windows:** Right-click the tray icon → *View Logs* or hover for status.

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
listen_port = 9999
auto_start  = true
```

| Key | Default | Description |
|-----|---------|-------------|
| `listen_port` | `9999` | TCP port Windows listens on |
| `auto_start` | `true` | Start with Windows |

### Linux config (`~/.bluetooth-bridge/.env`)

```env
# Use 'auto' for zero-config discovery, or set the Windows IP manually
PC_HOST=auto

# TCP port — must match Windows config
PC_PORT=9999

# Leave blank to auto-discover the controller, or set MAC (AA:BB:CC:DD:EE:FF)
CONTROLLER_MAC=
```

### Manual IP override (Linux)

If auto-discovery doesn't work, set the IP manually:
```
PC_HOST=192.168.1.101
```

---

## Network Packets

Fixed 24-byte binary frames at ~60 Hz — no JSON overhead.

| Offset | Size | Field | Range |
|--------|------|-------|-------|
| 0–1   | 2 | `lthumb_x` | 0–65535 (centre=32768) |
| 2–3   | 2 | `lthumb_y` | 0–65535 |
| 4–5   | 2 | `rthumb_x` | 0–65535 |
| 6–7   | 2 | `rthumb_y` | 0–65535 |
| 8     | 1 | `lt` | 0–255 |
| 9     | 1 | `rt` | 0–255 |
| 10    | 1 | `buttons_low` | Bitfield (A=1 B=2 X=4 Y=8 LB=16 RB=32 Back=64 Start=128) |
| 11    | 1 | `buttons_high` | Bitfield (L3=1 R3=2 Guide=4) |
| 12    | 1 | `dpad` | UP=1 RIGHT=2 DOWN=4 LEFT=8 (independent bits) |
| 13    | 1 | reserved | — |
| 14–23 | 10 | padding | — |

Ping frame (all `\xff`) sent every second as a keepalive.

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
│       ├── controller.py   # evdev → 24-byte packets
│       ├── network.py      # TCP client
│       ├── bluetooth.py   # bluetoothctl pairing
│       └── discovery.py    # UDP auto-discovery
└── windows/
    ├── installer/
    │   ├── install.ps1      # IRM installer
    │   ├── uninstall.ps1
    │   └── assets/icon.ico
    ├── requirements.txt
    └── src/
        ├── main.py         # Entry point
        ├── receiver.py     # TCP server
        ├── emitter.py      # vgamepad → ViGEmBus
        ├── tray.py         # System tray
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