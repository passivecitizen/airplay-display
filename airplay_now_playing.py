#!/usr/bin/env python3
"""
AirPlay Now Playing Display for Shairport-Sync
Lightweight HTTP server that reads the shairport-sync metadata pipe
and serves a Now Playing page. Designed for Raspberry Pi 3 with
limited RAM — no tkinter, no Pillow in the main process.
"""

import base64
import json
import os
import re
import signal
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

# --- Configuration ---
METADATA_PIPE = os.getenv(
    "SHAIRPORT_METADATA_PIPE", "/tmp/shairport-sync-metadata"
)
HTTP_PORT = int(os.getenv("AIRPLAY_DISPLAY_PORT", "8080"))

# Hex-encoded 4-char type/code identifiers from shairport-sync
TYPE_CORE = "636f7265"  # "core"
TYPE_SSNC = "73736e63"  # "ssnc"
CODE_TITLE = "6d696e6d"  # "minm"
CODE_ARTIST = "61736172"  # "asar"
CODE_ALBUM = "6173616c"  # "asal"
CODE_PICT = "50494354"  # "PICT"
CODE_PBEG = "70626567"  # "pbeg" - play begin
CODE_PEND = "70656e64"  # "pend" - play end
CODE_PFLS = "70666c73"  # "pfls" - play flush

# Shared state
_state = {
    "playing": False,
    "title": "",
    "artist": "",
    "album": "",
    "cover_b64": "",
    "updated": 0,
}
_state_lock = threading.Lock()

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Now Playing</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0a0a0a;
    color: #f0f0f0;
    font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif;
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100vh;
    overflow: hidden;
    cursor: none;
  }
  .container {
    text-align: center;
    max-width: 90vw;
    transition: opacity 0.5s ease;
  }
  .cover {
    width: min(60vh, 90vw);
    height: min(60vh, 90vw);
    border-radius: 12px;
    margin: 0 auto 28px;
    image-rendering: high-quality;
    background: #1a1a1a;
    object-fit: cover;
    display: none;
    box-shadow: 0 8px 32px rgba(0,0,0,0.6);
  }
  .cover.visible { display: block; }
  .title {
    font-size: 2.6rem;
    font-weight: 700;
    margin-bottom: 8px;
    line-height: 1.2;
  }
  .artist {
    font-size: 1.6rem;
    font-weight: 400;
    margin-bottom: 6px;
    opacity: 0.9;
  }
  .album {
    font-size: 1.2rem;
    font-weight: 300;
    color: #999;
  }
  .idle .title {
    font-size: 2rem;
    font-weight: 300;
    color: #666;
  }
</style>
</head>
<body>
<div class="container" id="main">
  <img class="cover" id="cover" src="" alt="">
  <div class="title" id="title">AirPlay Ready</div>
  <div class="artist" id="artist"></div>
  <div class="album" id="album"></div>
</div>
<script>
let lastUpdated = 0;
async function poll() {
  try {
    const r = await fetch('/api/now-playing');
    const d = await r.json();
    if (d.updated === lastUpdated) return;
    lastUpdated = d.updated;
    const main = document.getElementById('main');
    const cover = document.getElementById('cover');
    if (d.playing && d.title) {
      main.classList.remove('idle');
      document.getElementById('title').textContent = d.title;
      document.getElementById('artist').textContent = d.artist;
      document.getElementById('album').textContent = d.album;
      if (d.cover_b64) {
        cover.src = 'data:image/jpeg;base64,' + d.cover_b64;
        cover.classList.add('visible');
      } else {
        cover.classList.remove('visible');
      }
    } else {
      main.classList.add('idle');
      document.getElementById('title').textContent = 'AirPlay Ready';
      document.getElementById('artist').textContent = '';
      document.getElementById('album').textContent = '';
      cover.classList.remove('visible');
    }
  } catch(e) {}
}
setInterval(poll, 1000);
poll();
</script>
</body>
</html>"""


class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/now-playing":
            with _state_lock:
                payload = json.dumps(_state)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload.encode())
        elif self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Silence request logs


def read_metadata_pipe(stop_event: threading.Event):
    """Read and parse the shairport-sync metadata pipe."""
    import select

    ITEM_START = "<item>"
    ITEM_END = "</item>"
    # Regex to parse a single complete item
    item_re = re.compile(
        r"<type>([\da-fA-F]+)</type>"
        r"<code>([\da-fA-F]+)</code>"
        r"<length>(\d+)</length>"
        r"(?:\s*<data encoding=\"base64\">\s*(.*?)\s*</data>)?",
        re.DOTALL,
    )

    while not stop_event.is_set():
        if not os.path.exists(METADATA_PIPE):
            print(
                f"Waiting for metadata pipe: {METADATA_PIPE}",
                file=sys.stderr,
            )
            stop_event.wait(5)
            continue

        fd = -1
        try:
            fd = os.open(METADATA_PIPE, os.O_RDONLY)
            print("Metadata pipe opened", file=sys.stderr)
            buffer = ""
            while not stop_event.is_set():
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    continue
                raw = os.read(fd, 65536)
                if not raw:
                    print("Metadata pipe writer closed", file=sys.stderr)
                    time.sleep(1)
                    break
                buffer += raw.decode("utf-8", errors="replace")

                # Extract complete <item>...</item> blocks
                while True:
                    start = buffer.find(ITEM_START)
                    if start == -1:
                        # No item start — keep only last 256 chars
                        if len(buffer) > 256:
                            buffer = buffer[-256:]
                        break
                    end = buffer.find(ITEM_END, start)
                    if end == -1:
                        # Incomplete item — wait for more data
                        # Discard anything before the item start
                        buffer = buffer[start:]
                        break

                    item_str = buffer[start + len(ITEM_START):end]
                    buffer = buffer[end + len(ITEM_END):]

                    match = item_re.search(item_str)
                    if not match:
                        continue

                    item_type = match.group(1)
                    item_code = match.group(2)
                    b64_data = match.group(4) or ""

                    data = b""
                    if b64_data:
                        try:
                            data = base64.b64decode(b64_data)
                        except Exception:
                            pass

                    handle_item(item_type, item_code, data, b64_data)

        except OSError as e:
            print(f"Pipe read error: {e}", file=sys.stderr)
            stop_event.wait(5)
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def handle_item(item_type: str, item_code: str, data: bytes, raw_b64: str):
    with _state_lock:
        # Handle PICT from any type (core or ssnc)
        if item_code == CODE_PICT and raw_b64:
            clean_b64 = raw_b64.replace("\n", "").replace("\r", "").replace(" ", "")
            _state["cover_b64"] = clean_b64
            _state["updated"] = time.time()
            return

        if item_type == TYPE_CORE:
            text = data.decode("utf-8", errors="replace") if data else ""
            if item_code == CODE_TITLE:
                _state["title"] = text
                _state["playing"] = True
                _state["updated"] = time.time()
            elif item_code == CODE_ARTIST:
                _state["artist"] = text
                _state["playing"] = True
                _state["updated"] = time.time()
            elif item_code == CODE_ALBUM:
                _state["album"] = text
                _state["playing"] = True
                _state["updated"] = time.time()

        elif item_type == TYPE_SSNC:
            if item_code == CODE_PBEG:
                _state["playing"] = True
                _state["updated"] = time.time()
            elif item_code in (CODE_PEND, CODE_PFLS):
                _state["playing"] = False
                _state["title"] = ""
                _state["artist"] = ""
                _state["album"] = ""
                _state["cover_b64"] = ""
                _state["updated"] = time.time()


def main():
    stop_event = threading.Event()

    def _shutdown(*_):
        stop_event.set()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    reader = threading.Thread(
        target=read_metadata_pipe, args=(stop_event,), daemon=True
    )
    reader.start()

    server = HTTPServer(("127.0.0.1", HTTP_PORT), RequestHandler)
    print(f"Serving on http://127.0.0.1:{HTTP_PORT}", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
