"""Settings, paths and file-type categories."""
import json
import os
import secrets
import threading
import time
from pathlib import Path

APP_NAME = "YDM"
API_HOST = "127.0.0.1"
API_PORT = int(os.environ.get("YDM_PORT", 9614))  # tests use their own port

DATA_DIR = Path(os.environ.get("YDM_HOME") or Path(os.environ.get("APPDATA", Path.home())) / APP_NAME)
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
DOWNLOADS_FILE = DATA_DIR / "downloads.json"
TOKEN_FILE = DATA_DIR / "api_token"

DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / APP_NAME

# Same category split IDM uses; folder names are created under the download dir.
CATEGORIES = {
    "Compressed": "zip rar 7z gz tar bz2 xz tgz iso arj lzh cab z".split(),
    "Documents": "pdf doc docx xls xlsx ppt pptx txt rtf odt ods csv epub".split(),
    "Music": "mp3 wav wma aac flac ogg m4a opus aif aiff mid".split(),
    "Programs": "exe msi msix apk dmg pkg deb rpm bin appx".split(),
    "Video": "mp4 mkv avi mov wmv flv webm m4v 3gp mpg mpeg ts m3u8 mpd ogv".split(),
}

DEFAULTS = {
    "download_dir": str(DEFAULT_DOWNLOAD_DIR),
    "use_category_folders": True,
    "connections": 8,              # max connections per download (IDM default is 8)
    "max_concurrent": 3,           # downloads running at once in the main queue
    "speed_limit_kbps": 0,         # 0 = unlimited
    "min_split_size_kb": 1024,     # don't split a segment smaller than this
    "retries": 20,
    "timeout": 30,
    "clipboard_monitor": True,
    "browser_integration": True,
    "show_complete_dialog": True,
    "show_progress_dialog": True,
    "duplicate_action": "rename",  # rename | overwrite
    "intercept_extensions": sorted({e for v in CATEGORIES.values() for e in v} - {"txt", "csv", "m3u8", "mpd", "ts"}),
    "min_intercept_size_kb": 0,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "proxy": "",
    "ffmpeg_path": "ffmpeg",
    "ytdlp_path": "yt-dlp",
}


def category_for(filename: str) -> str:
    ext = Path(filename).suffix.lower().lstrip(".")
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "General"


class Settings:
    def __init__(self):
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        if SETTINGS_FILE.exists():
            try:
                self.data.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
            except (OSError, ValueError):
                pass

    def __getitem__(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self):
        with self._lock:
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), "utf-8")
            os.replace(tmp, SETTINGS_FILE)

    def target_dir(self, category: str) -> Path:
        base = Path(self["download_dir"])
        d = base / category if self["use_category_folders"] else base
        d.mkdir(parents=True, exist_ok=True)
        return d


def api_token() -> str:
    """Shared secret between the app and the native messaging host."""
    try:  # exclusive create: the app and the native host may both get here on first run
        with open(TOKEN_FILE, "x", encoding="utf-8") as f:
            f.write(secrets.token_hex(16))
    except FileExistsError:
        pass
    for _ in range(40):  # the other process may not have written its token yet
        token = TOKEN_FILE.read_text("utf-8").strip()
        if token:
            return token
        time.sleep(0.05)
    return token
