"""Sites that hide their media behind a player (YouTube, Vimeo, X, ...) go through yt-dlp.

yt-dlp is optional; without it those sites fall back to whatever direct media
the browser extension sniffed.
"""
import contextlib
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROGRESS_PREFIX = "[ydm]"


def available(engine) -> bool:
    return shutil.which(engine.settings["ytdlp_path"]) is not None


@contextlib.contextmanager
def cookie_file(jar):
    """The browser's cookies for the site as a temporary Netscape cookies.txt for --cookies.

    Lives next to YDM's own settings (readable only by this user) and is deleted right after use."""
    if not jar:
        yield None
        return
    path = config.DATA_DIR / f"cookies-{uuid.uuid4().hex}.txt"
    lines = ["# Netscape HTTP Cookie File"]
    for c in jar:
        domain = c.get("domain", "")
        if not domain or "\t" in str(c.get("value", "")) or "\n" in str(c.get("value", "")):
            continue
        sub = "FALSE" if c.get("hostOnly") else "TRUE"
        if sub == "TRUE" and not domain.startswith("."):
            domain = "." + domain
        lines.append("\t".join([domain, sub, c.get("path") or "/", "TRUE" if c.get("secure") else "FALSE",
                                str(int(c.get("expires") or 0)), str(c.get("name", "")), str(c.get("value", ""))]))
    path.write_text("\n".join(lines) + "\n", "utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _base_cmd(engine, headers, cookies_path=None):
    cmd = [shutil.which(engine.settings["ytdlp_path"]) or "yt-dlp", "--no-warnings", "--no-playlist"]
    ff = shutil.which(engine.settings["ffmpeg_path"])
    if ff:
        cmd += ["--ffmpeg-location", ff]
    if cookies_path:
        cmd += ["--cookies", str(cookies_path)]
    for k, v in (headers or {}).items():
        if v and (k.lower() in ("referer", "user-agent") or (k.lower() == "cookie" and not cookies_path)):
            cmd += ["--add-header", f"{k}:{v}"]
    if engine.settings["proxy"]:
        cmd += ["--proxy", engine.settings["proxy"]]
    return cmd


TIERS = [(4320, "8K"), (2160, "4K"), (1440, "2K"), (1080, "Full HD"), (720, "HD"), (480, "SD")]
CODECS = {"avc1": "H.264", "av01": "AV1", "vp09": "VP9", "vp9": "VP9", "hev1": "H.265", "hvc1": "H.265",
          "mp4a": "AAC", "opus": "Opus", "vorbis": "Vorbis", "mp3": "MP3", "ac-3": "AC-3", "ec-3": "E-AC-3"}
CONVERT_MARK = "|x:"   # appended to a format string: extract audio and convert, e.g. "ba/b|x:mp3"


def _codec(c):
    if not c or c == "none":
        return ""
    return CODECS.get(c.split(".")[0].lower(), c.split(".")[0])


def _has(f, kind):
    return f.get(kind) not in (None, "none")


def _size(f, duration):
    s = f.get("filesize") or f.get("filesize_approx")
    if not s and f.get("tbr") and duration:
        s = f["tbr"] * 125 * duration   # kbit/s -> bytes
    return int(s or 0)


def _tier(p):
    """'2K' for 1440, '4K' for 2160 ... based on the short side, so vertical videos read right."""
    for limit, name in TIERS:
        if p >= limit * 0.9:
            return name
    return f"{p}p"


CACHE_SECONDS = 20 * 60
_cache = {}      # key -> (time, result)
_pending = {}    # key -> Event, so a prefetch and a click share one yt-dlp run
_cache_lock = threading.Lock()


def _cache_key(url):
    """Same video, same key: drop timestamps, playlist and tracking parameters."""
    u = urlparse(url)
    q = parse_qs(u.query)
    if "youtube" in u.netloc and "v" in q:
        return "yt:" + q["v"][0]
    if u.netloc.endswith("youtu.be"):
        return "yt:" + u.path.strip("/")
    keep = [(k, v[0]) for k, v in sorted(q.items()) if not k.startswith("utm_") and k not in ("t", "si", "feature", "pp")]
    return u._replace(fragment="", query="&".join(f"{k}={v}" for k, v in keep)).geturl()


def formats(engine, url, headers=None, cookie_jar=None):
    """Quality list for a page, from a 20-minute cache when possible. A lookup takes a few
    seconds (yt-dlp has to start and query the site), so the browser asks ahead of time."""
    key = _cache_key(url)
    while True:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and time.time() - hit[0] < CACHE_SECONDS:
                return hit[1]
            waiting = _pending.get(key)
            if not waiting:
                _pending[key] = threading.Event()
                break
        waiting.wait(120)  # someone else is fetching this one; use their result
    try:
        with cookie_file(cookie_jar) as cookies_path:
            result = _fetch_formats(engine, url, headers, cookies_path)
        with _cache_lock:
            _cache[key] = (time.time(), result)
        return result
    finally:
        with _cache_lock:
            _pending.pop(key).set()


def _fetch_formats(engine, url, headers=None, cookies_path=None):
    """Every download choice for the menus: video qualities (each resolution in MP4 and WebM,
    high frame rate and HDR kept apart), audio-only options, and the raw format list."""
    cmd = _base_cmd(engine, headers, cookies_path) + ["-J", url]
    for attempt in range(2):
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=90,
                           creationflags=_NO_WINDOW)
        # Instagram & co. sometimes answer with a block page ("Failed to parse JSON"); one retry
        # usually gets through.
        if r.returncode == 0 or not re.search(r"parse JSON|rate.?limit|HTTP Error 429", r.stderr, re.I):
            break
        time.sleep(1.5)
    if r.returncode != 0:
        raise RuntimeError(_short_error(r.stderr) or "yt-dlp failed")
    info = json.loads(r.stdout)
    title = info.get("title") or "video"
    duration = info.get("duration") or 0
    fmts = [f for f in info.get("formats", []) if f.get("ext") != "mhtml" and (_has(f, "vcodec") or _has(f, "acodec"))]
    audios = [f for f in fmts if _has(f, "acodec") and not _has(f, "vcodec")]
    # YouTube's "-drc" tracks are loudness-compressed copies; keep the originals when present.
    audios = [a for a in audios if "drc" not in str(a.get("format_id"))] or audios

    def best_audio(prefer_ext):
        pool = [a for a in audios if a.get("ext") == prefer_ext] or audios
        return max(pool, key=lambda a: (a.get("abr") or a.get("tbr") or 0), default=None)

    out = [{"group": "video", "label": "Best available", "detail": "highest quality, auto",
            "format": "bv*+ba/b", "ext": "mp4", "size": 0}]

    # One entry per (resolution, frame rate, HDR, codec): keep the highest bitrate of each.
    codec_order = {"H.264": 0, "AV1": 1, "VP9": 2, "H.265": 3}
    best = {}
    for f in fmts:
        if not _has(f, "vcodec") or not f.get("height"):
            continue
        p = min(f["height"], f.get("width") or f["height"])
        fps = int(f.get("fps") or 0)
        hdr = (f.get("dynamic_range") or "SDR") != "SDR"
        key = (p, fps > 30, hdr, _codec(f.get("vcodec")))
        if key not in best or (f.get("tbr") or 0) > (best[key].get("tbr") or 0):
            best[key] = f
    for (p, hfr, hdr, codec), f in sorted(best.items(), key=lambda kv: (-kv[0][0], not kv[0][1], kv[0][2],
                                                                        codec_order.get(kv[0][3], 9))):
        family = "mp4" if f.get("ext") == "mp4" else "webm"
        size = _size(f, duration)
        if _has(f, "acodec"):
            selector = f["format_id"]
        else:
            a = best_audio("m4a" if family == "mp4" else "webm")
            selector = f"{f['format_id']}+{a['format_id']}/{f['format_id']}+ba/b" if a else f"{f['format_id']}+ba/b"
            size += _size(a, duration) if a else 0
        fps = int(f.get("fps") or 0)
        res = f"{p}p{fps if hfr else ''}{' HDR' if hdr else ''}"
        tier = _tier(p)
        out.append({"group": "video", "label": res if tier.endswith("p") else f"{tier} ({res})",
                    "detail": f"{family.upper()} · {codec}", "format": selector,
                    "ext": family, "size": size, "height": p})

    seen = set()
    for a in sorted(audios, key=lambda a: -(a.get("abr") or a.get("tbr") or 0)):
        abr = int(a.get("abr") or a.get("tbr") or 0)
        ext = "m4a" if a.get("ext") == "m4a" else a.get("ext") or "webm"
        if (ext, abr // 16) in seen:
            continue
        seen.add((ext, abr // 16))
        out.append({"group": "audio", "label": f"Audio {abr} kbps" if abr else "Audio",
                    "detail": f"{ext.upper()} · {_codec(a.get('acodec'))}", "format": a["format_id"],
                    "ext": ext, "size": _size(a, duration)})
    if audios and shutil.which(engine.settings["ffmpeg_path"]):
        top = best_audio("m4a")
        out.append({"group": "audio", "label": "MP3 320 kbps", "detail": "converted from best audio",
                    "format": "ba/b" + CONVERT_MARK + "mp3", "ext": "mp3",
                    "size": int(320 * 125 * duration) if duration else _size(top, duration)})

    for f in sorted(fmts, key=lambda f: (-(f.get("height") or 0), -(f.get("tbr") or 0))):
        v, a = _has(f, "vcodec"), _has(f, "acodec")
        res = f.get("resolution") or ("audio only" if not v else "")
        codecs = " + ".join(c for c in (_codec(f.get("vcodec")), _codec(f.get("acodec"))) if c)
        note = f.get("format_note") or ""
        out.append({"group": "all", "label": f"{f['format_id']} · {res} · {f.get('ext')}",
                    "detail": f"{codecs}{' · ' + note if note else ''}{'' if a or not v else ' (+ best audio)'}",
                    "format": f["format_id"] if a or not v else f"{f['format_id']}+ba",
                    "ext": f.get("ext") if a or not v else ("mp4" if f.get("ext") == "mp4" else "webm"),
                    "size": _size(f, duration)})
    return {"title": title, "duration": duration, "formats": out}


def download(engine, dl):
    if not available(engine):
        raise RuntimeError("yt-dlp is not installed (pip install yt-dlp)")
    with cookie_file(dl.cookie_jar) as cookies_path:
        _download(engine, dl, cookies_path)


def _download(engine, dl, cookies_path):
    stem = Path(dl.filename).stem
    out_tmpl = str(Path(dl.directory) / (stem + ".%(ext)s"))
    fmt = dl.format or "bv*+ba/b"
    fmt, _, convert = fmt.partition(CONVERT_MARK)
    cmd = _base_cmd(engine, dl.headers, cookies_path) + [
        # --print below implies --quiet, which hides progress unless --progress is given.
        "-f", fmt, "-o", out_tmpl, "--newline", "--no-part", "--continue", "--progress",
        "-N", str(max(1, engine.settings["connections"])),
        "--progress-template",
        "download:" + PROGRESS_PREFIX + " %(progress.downloaded_bytes)s %(progress.total_bytes)s "
                                        "%(progress.total_bytes_estimate)s %(info.format_id)s",
        "--print", "after_move:filepath",
    ]
    if convert:
        cmd += ["-x", "--audio-format", convert, "--audio-quality", "0"]
    elif Path(dl.filename).suffix.lower() in (".mp4", ".mkv", ".webm") and "+" in fmt:
        cmd += ["--merge-output-format", Path(dl.filename).suffix.lstrip(".").lower()]
    if engine.settings["speed_limit_kbps"]:
        cmd += ["-r", f"{engine.settings['speed_limit_kbps']}K"]
    cmd.append(dl.url)

    dl.status = "downloading"
    dl.active_connections = 1
    engine._emit("changed", dl.id)
    # One merged stream: a full stderr pipe would otherwise stall yt-dlp, and errors still reach us.
    dl.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    # yt-dlp reports each stream (video, then audio) separately, and only learns a stream's size
    # when it starts. Track them apart; until every stream has reported, keep the menu's estimate.
    estimate = dl.size if dl.size and dl.size > 0 else 0
    expected = 1 if convert else fmt.split("/")[0].count("+") + 1
    streams = {}  # format id -> [downloaded, total]
    final_path, messages = None, []
    for line in dl.process.stdout:
        line = line.strip()
        if line.startswith(PROGRESS_PREFIX):
            parts = line.split()
            got = _num(parts[1]) if len(parts) > 1 else 0
            total = (_num(parts[2]) or _num(parts[3])) if len(parts) > 3 else 0
            streams[parts[4] if len(parts) > 4 else "?"] = [got, total]
            now = int(sum(g for g, _ in streams.values()))
            dl.meter.add(max(0, now - dl.downloaded))
            dl.downloaded = now
            known = int(sum(t for _, t in streams.values()))
            if len(streams) >= expected and all(t for _, t in streams.values()):
                dl.size = known
            else:
                dl.size = max(estimate, known) if estimate else -1
        elif line and Path(line).suffix and Path(line).is_absolute():
            final_path = Path(line)
            if dl.status != "merging":
                dl.status = "merging"
        elif line:
            messages = (messages + [line])[-20:]
    dl.process.wait()
    if dl.stop_event.is_set():
        return
    if dl.process.returncode != 0 or not final_path or not final_path.exists():
        errors = [m for m in messages if m.startswith("ERROR")] or messages or ["failed"]
        raise RuntimeError("yt-dlp: " + (_short_error("\n".join(errors)) or "failed"))
    dl.filename = final_path.name
    dl.status, dl.size = "complete", final_path.stat().st_size
    dl.downloaded = dl.size
    dl.finished = time.time()


def _short_error(text):
    """yt-dlp's last ERROR line, without its 'please report this issue…' boilerplate."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    msg = next((ln for ln in reversed(lines) if ln.startswith("ERROR")), lines[-1] if lines else "")
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.split(r";\s*please report this issue", msg, flags=re.I)[0]
    if re.search(r"parse JSON|login|log in|rate.?limit|429", msg, re.I):
        msg += " (the site may need you to be signed in; refresh the page and try again)"
    return msg[:300]


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0
