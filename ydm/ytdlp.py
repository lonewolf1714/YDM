"""Sites that hide their media behind a player (YouTube, Vimeo, X, ...) go through yt-dlp.

yt-dlp is optional; without it those sites fall back to whatever direct media
the browser extension sniffed.
"""
import json
import shutil
import subprocess
import time
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROGRESS_PREFIX = "[ydm]"


def available(engine) -> bool:
    return shutil.which(engine.settings["ytdlp_path"]) is not None


def _base_cmd(engine, headers):
    cmd = [shutil.which(engine.settings["ytdlp_path"]) or "yt-dlp", "--no-warnings", "--no-playlist"]
    ff = shutil.which(engine.settings["ffmpeg_path"])
    if ff:
        cmd += ["--ffmpeg-location", ff]
    for k, v in (headers or {}).items():
        if v and k.lower() in ("referer", "user-agent", "cookie"):
            cmd += ["--add-header", f"{k}:{v}"]
    if engine.settings["proxy"]:
        cmd += ["--proxy", engine.settings["proxy"]]
    return cmd


def formats(engine, url, headers=None):
    """Quality choices for the popup menu, best first."""
    cmd = _base_cmd(engine, headers) + ["-J", url]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60,
                       creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:] or "yt-dlp failed")
    info = json.loads(r.stdout)
    title = info.get("title") or "video"
    duration = info.get("duration") or 0
    best_audio = max((f for f in info.get("formats", []) if f.get("vcodec") == "none" and f.get("acodec") != "none"),
                     key=lambda f: f.get("abr") or 0, default=None)
    audio_size = (best_audio or {}).get("filesize") or (best_audio or {}).get("filesize_approx") or 0
    by_height = {}
    for f in info.get("formats", []):
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if f.get("acodec") in (None, "none"):
            size += audio_size
        cur = by_height.get(h)
        if not cur or (f.get("tbr") or 0) > cur["tbr"]:
            by_height[h] = {"tbr": f.get("tbr") or 0, "size": size, "fps": f.get("fps")}
    out = []
    for h in sorted(by_height, reverse=True):
        v = by_height[h]
        out.append({"label": f"{h}p" + (f"{int(v['fps'])}" if v["fps"] and v["fps"] > 30 else ""),
                    "format": f"bv*[height<={h}]+ba/b[height<={h}]", "ext": "mp4", "size": v["size"]})
    if best_audio:
        out.append({"label": "Audio only (M4A/best)", "format": "ba[ext=m4a]/ba", "ext": "m4a", "size": audio_size})
    return {"title": title, "duration": duration, "formats": out}


def download(engine, dl):
    if not available(engine):
        raise RuntimeError("yt-dlp is not installed (pip install yt-dlp)")
    stem = Path(dl.filename).stem
    out_tmpl = str(Path(dl.directory) / (stem + ".%(ext)s"))
    fmt = dl.format or "bv*+ba/b"
    cmd = _base_cmd(engine, dl.headers) + [
        "-f", fmt, "-o", out_tmpl, "--newline", "--no-part", "--continue",
        "-N", str(max(1, engine.settings["connections"])),
        "--progress-template",
        "download:" + PROGRESS_PREFIX + " %(progress.downloaded_bytes)s %(progress.total_bytes)s "
                                        "%(progress.total_bytes_estimate)s %(info.format_id)s",
        "--print", "after_move:filepath",
    ]
    if Path(dl.filename).suffix.lower() in (".mp4", ".mkv", ".webm") and "ba" in fmt and "+" in fmt:
        cmd += ["--merge-output-format", Path(dl.filename).suffix.lstrip(".").lower()]
    if engine.settings["speed_limit_kbps"]:
        cmd += ["-r", f"{engine.settings['speed_limit_kbps']}K"]
    cmd.append(dl.url)

    dl.status = "downloading"
    dl.active_connections = 1
    engine._emit("changed", dl.id)
    dl.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    final_path, done_parts, last_fmt, last_bytes = None, 0, None, 0
    for line in dl.process.stdout:
        line = line.strip()
        if line.startswith(PROGRESS_PREFIX):
            parts = line.split()
            got = _num(parts[1]) if len(parts) > 1 else 0
            total = _num(parts[2]) or _num(parts[3]) if len(parts) > 3 else 0
            fmt_id = parts[4] if len(parts) > 4 else None
            if last_fmt and fmt_id != last_fmt:   # video finished, audio stream started
                done_parts += last_bytes
            last_fmt, last_bytes = fmt_id, got
            dl.meter.add(max(0, done_parts + got - dl.downloaded))
            dl.downloaded = done_parts + got
            if total:
                dl.size = max(dl.size, done_parts + int(total))
        elif line and Path(line).suffix and Path(line).is_absolute():
            final_path = Path(line)
            if dl.status != "merging":
                dl.status = "merging"
    dl.process.wait()
    if dl.stop_event.is_set():
        return
    if dl.process.returncode != 0 or not final_path or not final_path.exists():
        raise RuntimeError("yt-dlp: " + (dl.process.stderr.read().strip().splitlines() or ["failed"])[-1])
    dl.filename = final_path.name
    dl.status, dl.size = "complete", final_path.stat().st_size
    dl.downloaded = dl.size
    dl.finished = time.time()


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0
