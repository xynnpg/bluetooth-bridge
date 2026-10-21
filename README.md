# Xbox Controller Bluetooth Bridge

Stream your Bluetooth Xbox controller from a Linux server (or any Linux machine) to your Windows PC over WiFi — it appears as a real wired Xbox controller with full analog precision.

> **Install Windows side first, then Linux side.** The Windows app broadcasts its IP so Linux can find it automatically, zero-config.

---

## How it works

```
 Linux Server                           Windows PC
──────────────                          ───────────
┌──────────────────┐                   ┌──────────────────┐
│  Docker          │  TCP 54-byte pkts │  Tray + Web UI   │
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

**Windows:** Right-click the tray icon → *Open Web UI* (or visit
<http://127.0.0.1:<port>/> on the Windows PC, where *port* is shown by the
tray's *Open Web UI* action or in `config.ini`). The local dashboard shows the
connection state, controller name/MAC, battery % with bar, live rumble
motors, peer IP, uptime, packet count, a live event feed, a filterable log
viewer, and every setting. The tray tooltip also shows the battery %.

The tray menu is deliberately minimal: **Open Web UI · Reset · Reconnect ·
Open App Folder · Exit**.

The dashboard binds to a random free localhost port on first start (persisted
in `config.ini` as `[webui] port`; `0` means "pick one automatically"). The
tray's *Open Web UI* item, and the **About** tab, show the real URL.

**Linux:**
```bash
docker logs -f xbox-bridge
```

The Windows dashboard's **Linux container** tab uses port `9988` to show the
Linux process status and logs, update the Linux `.env`, and request a
container restart. Set `MANAGEMENT_TOKEN` to the same value in the Linux
`.env` and Windows Settings if the management port is reachable beyond your
trusted LAN. The Linux log file is stored at
`~/.bluetooth-bridge/xbox-bridge.log` by the installer.

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
auto_discover = true
auto_start    = true
log_level     = INFO

[webui]
enabled         = true
host            = 127.0.0.1
port            = 8764
open_on_launch  = false
auto_refresh_ms = 750

[controller]
rumble_enabled       = true
low_battery_warn_pct = 15
deadzone_stick       = 15
deadzone_trigger     = 1
invert_left_y        = false
invert_right_y       = false

[tray]
show_battery = true

[network]
discovery_port      = 9876
keepalive_timeout_s = 6.0
```

Everything here can also be edited live in the web UI (*Settings* tab), which
writes this file back and applies what it can without a restart.

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `app`        | `listen_port`   | `9999`    | TCP port Windows listens on |
| `app`        | `listen_host`   | `0.0.0.0` | Bind address |
| `app`        | `auto_discover` | `true`    | Broadcast IP for Linux auto-discovery (UDP 9876) |
| `app`        | `auto_start`    | `true`    | Start with Windows |
| `app`        | `log_level`     | `INFO`    | Log verbosity (applies immediately) |
| `webui`      | `enabled`       | `true`    | Serve the local dashboard |
| `webui`      | `host`          | `127.0.0.1` | Dashboard bind address (keep local-only) |
| `webui`      | `port`          | `8764`    | Dashboard port |
| `webui`      | `open_on_launch`| `false`   | Open the dashboard on start |
| `webui`      | `auto_refresh_ms`| `750`    | Dashboard poll interval |
| `controller` | `rumble_enabled`| `true`    | Forward game rumble back to the controller |
| `controller` | `low_battery_warn_pct` | `15` | Highlight battery at/below this % |
| `controller` | `deadzone_stick`| `15`      | Thumbstick deadzone (%) |
| `controller` | `deadzone_trigger`| `1`     | Trigger deadzone (%) |
| `controller` | `invert_left_y` | `false`   | Invert left stick Y |
| `controller` | `invert_right_y`| `false`   | Invert right stick Y |
| `tray`       | `show_battery`  | `true`    | Show battery % in tray tooltip |
| `network`    | `discovery_port`| `9876`    | UDP auto-discovery port |
| `network`    | `keepalive_timeout_s` | `6.0` | Seconds of silence before "disconnected" |

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

Fixed 54-byte v2 binary frames — no JSON overhead. The connection is
full-duplex: state flows Linux → Windows, while game rumble flows
Windows → Linux (same socket, same packet format).

A frame is sent for every controller event and, while the controller is
idle, a heartbeat frame is sent every 50 ms. Every frame is a **complete,
authoritative snapshot** of the controller, so a frame that is lost in
transit is corrected by the next one instead of latching a button forever.

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
| 15    | 1 | `flags` | bit0 = charging, bit1 = battery-valid, bit2 = "the sender streams idle heartbeats", bit3 = "byte 52 holds a valid checksum" |
| 16    | 1 | `rumble_left`  | 0–255 (large motor) |
| 17    | 1 | `rumble_right` | 0–255 (small motor) |
| 18–23 | 6 | `mac` | 6 raw bytes, e.g. `AA BB CC DD EE FF` (zeros if unknown) |
| 24–39 | 16 | `name` | ASCII, NUL-padded, truncated to 16 chars |
| 40–51 | 12 | `reserved` | zeros |
| 52    | 1 | `checksum` | `sum(bytes 0..51) & 0xFF` (announced by flag bit 3) |
| 53    | 1 | `reserved` | zeros |

Ping frame (54 × `\xff`) sent every second as a keepalive.

### Why the frames are validated

The wire format is a sequence of fixed-size frames, so a frame that is cut
short on the wire would shift every frame behind it — and a shifted window
can look exactly like a real frame. The receiver therefore validates the
proto byte, the D-pad mask, the battery range, the flag bits, the reserved
bytes and the checksum before it hands anything to the virtual pad.

The checksum is announced with **flag bit 3**, not by its value, so a frame
whose checksum happens to be `0` is protected as well.

How the receiver reacts to a broken stream depends on the peer:

* **A peer that announces checksums** is verified on every frame, so the
  receiver can hunt for the next frame boundary byte by byte — a shifted
  window can never pass the validation. If the stream stays broken, or the
  peer stops announcing the checksum, the connection is dropped and the
  Linux side reconnects with a fresh, aligned stream.
* **A legacy peer** (an older Linux side, no flag bit 3) has no such
  protection: nothing tells a shifted window from a real frame. For those
  the connection is dropped on a misalignment instead of re-syncing.

Either way the game never sees a press that nobody made.

### Why the "idle heartbeat" flag matters

Flag bit 2 tells the PC that this sender keeps streaming the current state
even when the controller does nothing. Only such a peer is watched with the
input watchdog on the PC side — with an older Linux side (which only
reports when the controller is used) a button that is held for a long time
must not be released by the PC while it is still being held.

### Why buttons are released explicitly

The Windows side does not rely on "reset the pad and press what is held"
to release a button: it keeps track of the buttons it reported and issues
an explicit `release_button()` for every button that was let go of. If the
frame carrying a release is lost, the next frame still says "not pressed"
and the button is released then. In addition the pad releases everything
when the input stream goes quiet (a watchdog on the *state* frames — the
keepalive pings do not keep a dead input pipeline alive).

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

### A button stays held after I let go of it

The bridge releases a button as soon as the frame that says "not pressed"
arrives, and it re-sends the full controller state every 50 ms, so a lost
frame heals itself. If a button is *still* held:

1. Open the **Live input** panel on the dashboard — it shows the button
   bits the Windows side currently receives.
2. Compare it with the Linux log (`docker logs -f xbox-bridge`): the lines
   `input: … bl=0x… bh=0x…` show what the Linux side believes is pressed.
   - The **Linux** log still shows the button pressed → the release event
     never arrived from the controller. The bridge re-reads the button
     state from the kernel when the kernel reports dropped events
     (`SYN_DROPPED`); a Bluetooth adapter with a busy/dropping link is the
     usual cause.
   - The **Linux** log shows it released but the **Windows** side does not
     → the frames are not arriving. The Windows log then contains
     `Invalid frame … stream misaligned` or `No controller state for … s`;
     both mean the connection had to be re-established.
3. The Windows watchdog releases every button when no state frame arrives
   for `network.keepalive_timeout_s` seconds, so a dead input pipeline can
   no longer keep a button latched in the game.

### The game crashes when I play

- Check the Windows log for `Invalid frame … stream misaligned`: the
  receiver refuses to decode a shifted frame, so a corrupted stream can no
  longer turn into phantom button presses.
- Check for `Send timed out … dropping the connection` in the Linux log:
  the link stalled mid-frame, the connection was dropped on purpose and
  the frames start again on a fresh, aligned stream.

---

## Diagnostics

Both sides keep an **input trace**: a separate log with millisecond
precision that follows every button from the physical controller to the
virtual gamepad. It is the log to look at when a button gets stuck.

| Side | File |
|------|------|
| Linux | `~/.bluetooth-bridge/input-trace.log` (written by the container) |
| Windows | `%LOCALAPPDATA%\bluetooth_bridge\input-trace.log` |

```
21:14:23.481 LINUX  BTN A        press          <- the controller reports it
21:14:23.482 LINUX  TX buttons=01/00 dpad=0 lt=0 rt=0
21:14:23.512 WIN    RX buttons=01/00 dpad=0 lt=0 rt=0
21:14:23.513 WIN    PAD A        press          <- the game sees it
21:14:27.902 LINUX  BTN A        release        <- the release is reported …
21:14:27.918 WIN    PAD A        release        <- … and reaches the game
```

Every line is `H:M:S.mmm SOURCE message`, so the two files can simply be
merged and sorted by time.

### Collect and merge

On the Windows PC (it pulls the Linux trace through the management API
and merges both files):

```powershell
python tools\collect_input_trace.py --linux 192.168.0.10
```

On the Linux host:

```bash
python3 tools/collect_input_trace.py
```

The script writes `input-trace-<date>.txt` and reports the interesting
part:

```
!! suspicious events:
   21:14:23.513  WIN    A was pressed and never released — this button is still held
   21:14:27.902  LINUX  the release of A never reached the Windows side — the button stays held in the game
```

That tells you where the problem is:

| What the trace shows | Where the problem is |
|----------------------|----------------------|
| No `BTN … release` on the **Linux** side | The controller/driver never reported the release (Bluetooth link, evdev) |
| `BTN … release` on Linux but no `PAD … release` on Windows | The release was lost on the way (network) |
| `PAD … release` on Windows but the button is still held in the game | The virtual pad did not let go (ViGEmBus) |

The regular logs (`docker logs -f xbox-bridge` and
`%LOCALAPPDATA%\bluetooth_bridge\bluetooth-bridge.log`) use the same
`H:M:S` timestamps and contain the rest (connections, resyncs,
watchdogs).

---

## Development

### Tests

The regression suite covers the whole chain — evdev → packet → TCP →
receiver → virtual pad — with in-process fakes for `evdev` and `vgamepad`
(neither can be installed on a test machine: one needs a real input
device, the other a real ViGEmBus driver).

```bash
uv venv .venv --python python3.14      # or: python3 -m venv .venv
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest tests/ -q
```

The suite is the executable specification of the input pipeline:

| File | What it guards |
|------|----------------|
| `tests/test_linux_input_pipeline.py` | evdev → state → packet (press/release, D-pad, triggers, `SYN_DROPPED` resync) |
| `tests/test_linux_transport.py` | frame atomicity on the wire, reconnect, idle heartbeat |
| `tests/test_windows_receiver.py` | frame validation, keepalive, corrupted stream |
| `tests/test_windows_emitter.py` | explicit press/release, input watchdog |
| `tests/test_end_to_end.py` | the full chain, including dropped frames and a corrupted stream |
| `tests/test_emitter_stuck_buttons.py` | the original "button stays held" regression |

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
        ├── config.py       # config.ini schema, load/save/validate
        ├── webui.py        # Local Flask dashboard (random localhost port)
        ├── tray.py         # Minimal system tray
        ├── state.py        # Thread-safe live state shared by tray/web UI
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