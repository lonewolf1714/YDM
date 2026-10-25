"""HTTP helpers: opener, probing, filename detection, global speed limiter."""
import mimetypes
import re
import threading
import time
import urllib.request
from email.message import Message
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    name = INVALID_CHARS.sub("_", name).strip(" .")
    return name[:200] or "download"


def build_opener(proxy: str = ""):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def make_request(url: str, headers: dict, range_from=None, range_to=None, method="GET"):
    req = urllib.request.Request(url, method=method)
    for k, v in (headers or {}).items():
        if v:
            req.add_header(k, v)
    if range_from is not None:
        req.add_header("Range", f"bytes={range_from}-{'' if range_to is None else range_to}")
    req.add_header("Accept-Encoding", "identity")  # byte offsets must match the file
    return req


def filename_from_headers(content_disposition: str) -> str:
    if not content_disposition:
        return ""
    msg = Message()
    msg["content-disposition"] = content_disposition
    name = msg.get_filename()  # handles filename*=UTF-8''... too
    return unquote(name) if name else ""


def filename_from_url(url: str) -> str:
    return unquote(PurePosixPath(urlparse(url).path).name)


def probe(url: str, headers: dict, timeout=30, proxy="") -> dict:
    """Ask for byte 0 only: a 206 reply tells us the size and that ranges work."""
    opener = build_opener(proxy)
    info = {"url": url, "final_url": url, "size": -1, "resumable": False,
            "filename": "", "content_type": ""}
    with opener.open(make_request(url, headers, 0, 0), timeout=timeout) as resp:
        info["final_url"] = resp.geturl()
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        info["content_type"] = ctype
        if resp.status == 206:
            m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", ""))
            if m:
                info["size"] = int(m.group(1))
                info["resumable"] = True
        else:
            cl = resp.headers.get("Content-Length")
            info["size"] = int(cl) if cl and cl.isdigit() else -1
        name = filename_from_headers(resp.headers.get("Content-Disposition", ""))
        if not name:
            name = filename_from_url(info["final_url"]) or filename_from_url(url)
        if name and "." not in name and ctype:
            name += mimetypes.guess_extension(ctype) or ""
        if not name:
            name = "download" + (mimetypes.guess_extension(ctype) or "")
        if ctype in ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl") \
                or name.lower().endswith(".m3u8"):
            info["kind"] = "hls"
        info["filename"] = sanitize_filename(name)
    return info


class SpeedLimiter:
    """Token bucket shared by every connection. rate=0 means unlimited."""

    def __init__(self, rate_bps: int = 0):
        self.rate = rate_bps
        self.tokens = 0.0
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def set_rate(self, rate_bps: int):
        with self.lock:
            self.rate = max(0, int(rate_bps))
            self.tokens = 0.0

    def consume(self, n: int):
        if self.rate <= 0:
            return
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
            self.last = now
            self.tokens -= n
            wait = -self.tokens / self.rate if self.tokens < 0 else 0
        if wait > 0:
            time.sleep(wait)


class RateMeter:
    """Bytes/sec over a sliding window."""

    def __init__(self, window=3.0):
        self.window = window
        self.samples = []
        self.lock = threading.Lock()

    def add(self, n):
        now = time.monotonic()
        with self.lock:
            self.samples.append((now, n))

    def rate(self):
        now = time.monotonic()
        with self.lock:
            self.samples = [(t, n) for t, n in self.samples if now - t <= self.window]
            if not self.samples:
                return 0.0
            span = max(now - self.samples[0][0], 0.5)
            return sum(n for _, n in self.samples) / span
