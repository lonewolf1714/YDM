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


def formats(engine, url, headers=None):
    """Every download choice for the menus: video qualities (each resolution in MP4 and WebM,
    high frame rate and HDR kept apart), audio-only options, and the raw format list."""
    cmd = _base_cmd(engine, headers) + ["-J", url]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=90,
                       creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:] or "yt-dlp failed")
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
    stem = Path(dl.filename).stem
    out_tmpl = str(Path(dl.directory) / (stem + ".%(ext)s"))
    fmt = dl.format or "bv*+ba/b"
    fmt, _, convert = fmt.partition(CONVERT_MARK)
    cmd = _base_cmd(engine, dl.headers) + [
        "-f", fmt, "-o", out_tmpl, "--newline", "--no-part", "--continue",
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
