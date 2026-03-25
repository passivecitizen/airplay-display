# AirPlay Now Playing Display

A fullscreen TV display for [Shairport Sync](https://github.com/mikebrady/shairport-sync) that shows the currently playing track info and album art when streaming via AirPlay.

## How It Works

```
iPhone/Mac → AirPlay → shairport-sync → metadata pipe → Python HTTP server → Chromium kiosk → TV
```

1. **Shairport Sync** receives AirPlay audio and writes track metadata (title, artist, album, cover art) to a named pipe (`/tmp/shairport-sync-metadata`)
2. **`airplay_now_playing.py`** reads the pipe, parses metadata items, and serves a JSON API + HTML page on `http://127.0.0.1:8080`
3. **Chromium** runs in kiosk mode (fullscreen, no UI chrome) pointed at the local server
4. The HTML page polls `/api/now-playing` every second and updates the display

## Setup Guide

This documents every step taken to get shairport-sync and the display working together.

### Prerequisites

- Raspberry Pi running Raspberry Pi OS with desktop (LXDE + LightDM + Xorg)
- HiFiBerry DAC+ Pro (or any ALSA-compatible audio output)
- HDMI connected to a TV
- Python 3.11+ with `python3-pil.imagetk` (for the original tkinter approach, not needed for the current HTTP approach)

### 1. Shairport Sync Installation

Shairport Sync was compiled from source with metadata support:

```bash
# Install build dependencies
sudo apt-get install -y build-essential git autoconf automake libtool \
    libpopt-dev libconfig-dev libssl-dev libavahi-client-dev \
    libasound2-dev

# Clone and build
git clone https://github.com/mikebrady/shairport-sync.git
cd shairport-sync
autoreconf -fi
./configure --sysconfdir=/usr/local/etc --with-alsa --with-avahi \
            --with-ssl=openssl --with-metadata
make
sudo make install

# Create service user
sudo useradd -r -M -G audio shairport-sync
```

The installed version: `4.3.3-OpenSSL-Avahi-ALSA-metadata-sysconfdir:/usr/local/etc`

### 2. Shairport Sync Configuration

Edit `/usr/local/etc/shairport-sync.conf`:

```
general = {
    name = "The Great Machine";
};

metadata = {
    enabled = "yes";
    include_cover_art = "yes";
    pipe_name = "/tmp/shairport-sync-metadata";
    pipe_timeout = 5000;
};
```

Key config changes made:
- **`name`** — the AirPlay name visible on iPhones/Macs
- **`metadata.enabled`** — turns on the metadata pipe so track info is emitted
- **`metadata.include_cover_art`** — includes album art (base64-encoded JPEG, ~600x600) in the pipe
- **`metadata.pipe_name`** — path to the FIFO that shairport-sync writes to
- **`metadata.pipe_timeout`** — milliseconds to wait for a pipe reader before discarding data

### 3. Shairport Sync Systemd Service

The service file at `/lib/systemd/system/shairport-sync.service`:

```ini
[Unit]
Description=Shairport Sync - AirPlay Audio Receiver
After=sound.target
Requires=avahi-daemon.service
After=avahi-daemon.service
Wants=network-online.target
After=network.target network-online.target

[Service]
ExecStart=/usr/local/bin/shairport-sync --log-to-syslog
User=shairport-sync
Group=shairport-sync

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable shairport-sync
sudo systemctl start shairport-sync
```

### 4. Install Display Dependencies

```bash
# Only standard library needed for the HTTP server approach
# Pillow ImageTk only needed if using the tkinter approach
sudo apt-get install -y python3-pil.imagetk
```

### 5. Deploy the Display Script

```bash
mkdir -p ~/airplay-display
# Copy airplay_now_playing.py to ~/airplay-display/
```

### 6. Create the HTTP Server Service

```bash
sudo tee /etc/systemd/system/airplay-display.service > /dev/null << 'EOF'
[Unit]
Description=AirPlay Now Playing Display - HTTP Server
After=shairport-sync.service
Wants=shairport-sync.service

[Service]
Type=simple
User=passivecitizen
ExecStart=/usr/bin/python3 /home/passivecitizen/airplay-display/airplay_now_playing.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

### 7. Create the Chromium Kiosk Service

```bash
sudo tee /etc/systemd/system/airplay-chromium.service > /dev/null << 'EOF'
[Unit]
Description=AirPlay Now Playing Display - Chromium Kiosk
After=airplay-display.service graphical.target
Wants=airplay-display.service
Requires=graphical.target

[Service]
Type=simple
User=passivecitizen
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/passivecitizen/.Xauthority
ExecStartPre=/bin/sleep 3
ExecStart=/usr/bin/chromium-browser --noerrdialogs --disable-infobars --disable-gpu --disable-software-rasterizer --kiosk --incognito --no-first-run http://127.0.0.1:8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical.target
EOF
```

Chromium flags explained:
- **`--kiosk`** — fullscreen, no address bar or window controls
- **`--incognito`** — no session restore dialogs on crash
- **`--disable-gpu`** / **`--disable-software-rasterizer`** — reduce memory usage on Pi 3
- **`--noerrdialogs`** / **`--disable-infobars`** — suppress popup dialogs
- **`--no-first-run`** — skip welcome/setup screens
- **`ExecStartPre=/bin/sleep 3`** — wait for the HTTP server to be ready before launching the browser

### 8. Enable and Start Services

```bash
sudo systemctl daemon-reload
sudo systemctl enable airplay-display.service airplay-chromium.service
sudo systemctl start airplay-display.service airplay-chromium.service
```

### Technical Notes on the Metadata Pipe

The shairport-sync metadata pipe emits XML-formatted items:

```xml
<item><type>636f7265</type><code>6d696e6d</code><length>10</length>
<data encoding="base64">U29uZyBUaXRsZQ==</data></item>
```

- **type** and **code** are hex-encoded 4-char ASCII identifiers
- **data** is base64-encoded

Key codes:
| Type | Code | Meaning |
|------|------|---------|
| `636f7265` (core) | `6d696e6d` (minm) | Track title |
| `636f7265` (core) | `61736172` (asar) | Artist |
| `636f7265` (core) | `6173616c` (asal) | Album |
| `636f7265` (core) | `50494354` (PICT) | Cover art (JPEG) |
| `73736e63` (ssnc) | `70626567` (pbeg) | Playback started |
| `73736e63` (ssnc) | `70656e64` (pend) | Playback ended |
| `73736e63` (ssnc) | `70666c73` (pfls) | Playback flushed (pause/skip) |

**Important implementation details:**
- The FIFO must be opened with `os.open(path, os.O_RDONLY)` and read with `os.read()` + `select.select()`. Python's buffered `open()` / `read()` does not work reliably with FIFOs.
- The pipe only has data when a reader is connected AND an AirPlay session is active. The `os.open()` call blocks until shairport-sync opens the write end (which happens when a client connects).
- Cover art PICT items can be 100KB+ of base64 data split across multiple read chunks. The parser must accumulate data and only process complete `<item>...</item>` blocks.
- The `playing` state cannot rely solely on `pbeg`/`pend` signals — they don't always fire. Instead, set `playing = True` whenever track metadata (title/artist/album) arrives.

### Troubleshooting

**AirPlay receiver not visible on iPhone:**
- Check avahi-daemon is running: `systemctl is-active avahi-daemon`
- Check shairport-sync is running: `systemctl is-active shairport-sync`
- Ensure both devices are on the same network/VLAN

**Display shows "AirPlay Ready" but no track info:**
- Verify the metadata pipe exists: `ls -la /tmp/shairport-sync-metadata` (should be `prw-rw-rw-`)
- Test pipe directly: `timeout 10 dd if=/tmp/shairport-sync-metadata bs=1 count=4096 | od -c | head`
- Must stop airplay-display service first (only one pipe reader at a time)
- If pipe is empty, try skipping to the next track — metadata is only sent on track changes

**Pi freezes after starting display:**
- The Pi 3 has only 906 MB RAM. Do NOT use tkinter + Pillow for the display — use the HTTP server + Chromium approach instead
- If Chromium consumes too much memory, add `--js-flags="--max-old-space-size=128"` to limit its heap

**Cover art not showing:**
- Verify `include_cover_art = "yes"` in shairport-sync.conf
- Restart shairport-sync after config changes: `sudo systemctl restart shairport-sync`
- The PICT data is large (~180KB base64). Ensure the parser waits for complete `<item>...</item>` blocks before parsing

## Current Setup

**Target hardware:** Raspberry Pi 3 Model B (906 MB RAM)
**Audio output:** HiFiBerry DAC+ Pro
**Display:** HDMI to TV
**Desktop:** LXDE + LightDM + Xorg

### Files on the Pi

| File | Path |
|------|------|
| Python script | `/home/passivecitizen/airplay-display/airplay_now_playing.py` |
| HTTP server service | `/etc/systemd/system/airplay-display.service` |
| Chromium kiosk service | `/etc/systemd/system/airplay-chromium.service` |
| Shairport Sync config | `/usr/local/etc/shairport-sync.conf` |

### Systemd Services

```bash
# Start both services
sudo systemctl start airplay-display.service airplay-chromium.service

# Stop both services
sudo systemctl stop airplay-chromium.service airplay-display.service

# Restart just the browser (after HTML/CSS changes)
sudo systemctl restart airplay-chromium.service

# View logs
sudo journalctl -u airplay-display -f
sudo journalctl -u airplay-chromium -f
```

### Shairport Sync Metadata Config

In `/usr/local/etc/shairport-sync.conf`:

```
metadata = {
    enabled = "yes";
    include_cover_art = "yes";
    pipe_name = "/tmp/shairport-sync-metadata";
    pipe_timeout = 5000;
};
```

### API

- `GET /` — HTML now playing page
- `GET /api/now-playing` — JSON with current track state:
  ```json
  {
    "playing": true,
    "title": "Song Title",
    "artist": "Artist Name",
    "album": "Album Name",
    "cover_b64": "<base64 JPEG>",
    "updated": 1774397674.20
  }
  ```

## Limitations

- **AirPlay 1 only** — shairport-sync 4.3.3 runs in classic AirPlay mode; cover art is ~600x600 JPEG
- **Metadata on track change** — info only arrives when a new track starts or you skip; no real-time progress bar
- **Single reader pipe** — only one process can read the metadata FIFO at a time

## Recommendations for Raspberry Pi 5

The Pi 3 has limited RAM (906 MB) which constrained the architecture. A Pi 5 (4–8 GB RAM) opens up significantly better options:

### 1. Replace Chromium with a Native GUI

Chromium in kiosk mode uses ~200 MB RAM. On a Pi 5 this is fine, but a native approach would be smoother:

- **PyQt6 / PySide6** — build the display as a native Qt app with hardware-accelerated rendering. Supports smooth animations, blur effects, and anti-aliased text without a browser.
- **Pygame / Pygame CE** — lightweight, direct framebuffer rendering. Good for simple layouts.
- **GTK4** — native GNOME toolkit, integrates well with Wayland (Pi 5 default).

```python
# Example: PyQt6 fullscreen window
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
from PyQt6.QtCore import QTimer
# ... fetch /api/now-playing with QTimer, update QLabels
```

### 2. Use AirPlay 2 via shairport-sync 4.x with NQPTP

Pi 5 has enough horsepower for AirPlay 2:

```bash
# Build shairport-sync with AirPlay 2 support
git clone https://github.com/mikebrady/nqptp.git
cd nqptp && autoreconf -fi && ./configure && make && sudo make install

git clone https://github.com/mikebrady/shairport-sync.git
cd shairport-sync
autoreconf -fi
./configure --with-airplay-2 --with-metadata --with-dbus-interface \
            --with-ssl=openssl --with-avahi --with-alsa
make && sudo make install
```

AirPlay 2 benefits:
- Higher quality audio (ALAC lossless)
- Multi-room sync
- Potentially higher-res cover art

### 3. Use D-Bus Instead of Metadata Pipe

With `--with-dbus-interface`, shairport-sync exposes track metadata via D-Bus — more reliable than the FIFO pipe:

```python
import dbus

bus = dbus.SystemBus()
proxy = bus.get_object(
    'org.gnome.ShairportSync',
    '/org/gnome/ShairportSync'
)
props = dbus.Interface(proxy, 'org.freedesktop.DBus.Properties')
metadata = props.Get('org.gnome.ShairportSync', 'Metadata')
```

Or use the **MPRIS interface** (`--with-mpris-interface`) which is the standard Linux media player protocol — compatible with KDE Connect, playerctl, etc.

### 4. Use MQTT for Home Assistant Integration

Build with `--with-mqtt-client` and publish track info to an MQTT broker. This lets you:

- Display now playing in Home Assistant dashboards
- Create automations (e.g., dim lights when music starts)
- Show track info on multiple displays simultaneously

```
mqtt = {
    enabled = "yes";
    hostname = "homeassistant.local";
    port = 1883;
    topic = "airplay/the-great-machine";
    publish_parsed = "yes";
    publish_cover = "yes";
};
```

### 5. Wayland + wlroots Compositor

Pi 5 defaults to Wayland. Instead of Xorg + LXDE:

- Use **labwc** or **sway** as a minimal Wayland compositor
- Run the display app directly as a Wayland client
- Lower resource usage, better performance, no screen tearing

### 6. Enhanced Display Features

With more RAM and CPU:

- **Background blur** — use the album art as a blurred fullscreen background behind the centered cover
- **Smooth transitions** — crossfade between tracks with CSS or native animations
- **Progress bar** — use shairport-sync's `progress_interval` metadata for real-time playback position
- **Visualizer** — FFT audio visualizer alongside the track info (similar to the Unicorn pHAT visualizer on pimoroni)
- **Lyrics** — fetch lyrics from an API and display them synced to playback

## SSH Access

```bash
ssh -i ~/.ssh/thegreatmachine_ed25519 passivecitizen@the-great-machine.local
```

## Deploy from Mac

```bash
# Copy script to Pi
scp -i ~/.ssh/thegreatmachine_ed25519 airplay_now_playing.py \
    passivecitizen@the-great-machine.local:~/airplay-display/

# Restart services
ssh -i ~/.ssh/thegreatmachine_ed25519 passivecitizen@the-great-machine.local \
    "sudo systemctl restart airplay-display.service airplay-chromium.service"
```
