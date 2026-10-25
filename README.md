# YDM: download manager for Windows

A clean-room download manager with the features people use IDM for. It is written from scratch: no IDM code or binaries are used.

## Features

| IDM feature | YDM |
|---|---|
| Multi-connection download acceleration | Up to 32 connections per file on one pre-allocated file |
| Dynamic segmentation | When a connection finishes, the biggest remaining part is split in half, so every connection stays busy until the end |
| Pause / resume, survives crashes and restarts | Segment map is saved every 5 s (`%APPDATA%\YDM\downloads.json`) |
| "Download this video" button over videos | The browser extension shows a button over any hovered or playing video, with a quality list |
| Browser download takeover | Extension pauses the browser download, hands it to YDM (with cookies and referer), and falls back to the browser if YDM is unreachable |
| Video grabbing (YouTube etc.) | Uses yt-dlp, with the qualities shown in the popup menu and audio/video merged by ffmpeg |
| HLS (.m3u8) streams | Picks the best variant, fetches segments in parallel, remuxes to MP4. Encrypted streams go through ffmpeg |
| Download File Info dialog, category folders | Compressed / Documents / Music / Programs / Video / General |
| Progress window with connection map | Per-download progress window, plus a per-download speed limiter |
| Download complete dialog | Open / Open with / Open folder |
| Queue + Scheduler | Start and stop times, days of the week, then exit / sleep / hibernate / shut down when the queue finishes |
| Speed limiter | Global and per download |
| Clipboard monitoring | Copying a link to a matching file type opens the download dialog |
| Batch download | `http://site/img*.jpg` from 1 to 120 with zero padding, or letters; also from the clipboard or a text file |
| Context menus | Download link / media / all links on the page / page video |
| Command line | `ydm_app.py /d URL /p DIR /f NAME /n /a /s` (IDM-style switches) |
| Run at startup, proxy, retries, timeouts, duplicate-name handling | In Options |

## Install

Run **`YDM-Setup-<version>.exe`**. It installs per-user into `%LOCALAPPDATA%\Programs\YDM` (no admin rights, no Python needed) and:

- connects YDM to Chrome, Edge, Brave, Chromium, Vivaldi and Opera,
- adds Start Menu and Desktop shortcuts, and optionally starts YDM with Windows,
- adds YDM to Windows **Settings → Apps → Installed apps**, from where it can be uninstalled.

For YouTube and similar sites, install yt-dlp and ffmpeg (`winget install yt-dlp.yt-dlp Gyan.FFmpeg`).

Then load the browser extension (browsers only let you do this yourself):

1. Open `chrome://extensions` (or `edge://extensions`, `brave://extensions`).
2. Turn on **Developer mode**.
3. Click **Load unpacked** and choose `%LOCALAPPDATA%\Programs\YDM\extension`.

The extension ID is always `jimfaknamlepkebnaemlonbhcemcoimc`, because the manifest has a fixed key; the installer registers the native host for that ID.

Silent install: `YDM-Setup-<version>.exe /S [/D=folder] [--no-shortcuts] [--no-autostart]`.

### Building the installer

```bash
pip install pyinstaller
python tools/build.py
```

This produces `dist/YDM` (the app folder, with `YDM.exe` and `ydm-host.exe`) and `dist/YDM-Setup-<version>.exe`.

### Running from source

`python ydm_app.py`; `install.ps1` registers a source checkout with the browsers instead of the installed app.

## How the parts fit

```
browser page ──content.js (video button)──┐
                                          ▼
                              background.js (sniffs media, takes over downloads)
                                          │ native messaging (stdin/stdout JSON)
                                          ▼
                              ydm/native_host.py ──HTTP 127.0.0.1:9614 + token──► YDM app (ydm/gui.py)
                                                                                     │
                                                                                     ▼
                                                                    ydm/engine.py (+ hls.py, ytdlp.py)
```

The loopback API only accepts requests carrying the secret in `%APPDATA%\YDM\api_token`, so web pages cannot push downloads into the app.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests run a local server with Range support and cover segmented download, dynamic splitting, stop and resume across restarts, servers without ranges, HLS, the speed limiter, queue concurrency and the native-messaging round trip. `tools/gui_smoke.py` opens every dialog during a live download.

## Limits

- DRM-protected streams (Netflix, Prime, Disney+ and similar) are not supported, and won't be.
- Firefox is not wired up yet: its extension manifest and native-host registry key differ from Chromium's.
- POST-only downloads, which some file hosts use, are left to the browser.
