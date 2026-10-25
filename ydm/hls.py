"""HLS (.m3u8) streams: fetch segments in parallel, join them, remux to MP4 with ffmpeg.

Encrypted playlists (#EXT-X-KEY) and fMP4/byte-range playlists are handed to
ffmpeg directly, since it already decrypts and joins those correctly.
"""
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

from . import net


def fetch_text(engine, url, headers):
    opener = net.build_opener(engine.settings["proxy"])
    with opener.open(net.make_request(url, headers), timeout=engine.settings["timeout"]) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def parse_master(base, text):
    """Return [(bandwidth, resolution, url)] for a master playlist, best first."""
    variants, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            attrs = line.split(":", 1)[1]
            bw = res = ""
            for part in attrs.split(","):
                if part.startswith("BANDWIDTH="):
                    bw = part.split("=", 1)[1]
                elif part.startswith("RESOLUTION="):
                    res = part.split("=", 1)[1]
            for nxt in lines[i + 1:]:
                if nxt and not nxt.startswith("#"):
                    variants.append((int(bw or 0), res, urljoin(base, nxt.strip())))
                    break
    return sorted(variants, reverse=True)


def parse_media(base, text):
    segs, needs_ffmpeg, duration = [], False, 0.0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line:
            needs_ffmpeg = True
        elif line.startswith(("#EXT-X-MAP", "#EXT-X-BYTERANGE")):
            needs_ffmpeg = True
        elif line.startswith("#EXTINF:"):
            try:
                duration += float(line[8:].split(",")[0])
            except ValueError:
                pass
        elif line and not line.startswith("#"):
            segs.append(urljoin(base, line))
    return segs, needs_ffmpeg, duration


def resolve(engine, url, headers):
    """Follow a master playlist to its best variant. Returns (media_url, segments, needs_ffmpeg, duration)."""
    base, text = fetch_text(engine, url, headers)
    if "#EXT-X-STREAM-INF" in text:
        variants = parse_master(base, text)
        if not variants:
            raise RuntimeError("Empty master playlist")
        base, text = fetch_text(engine, variants[0][2], headers)
    segs, needs_ffmpeg, duration = parse_media(base, text)
    if not segs:
        raise RuntimeError("Playlist has no segments (live stream?)")
    return base, segs, needs_ffmpeg, duration


def ffmpeg_headers(headers):
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items() if v)


def download(engine, dl):
    ffmpeg = shutil.which(engine.settings["ffmpeg_path"]) or None
    media_url, segs, needs_ffmpeg, _ = resolve(engine, dl.url, dl.headers)
    dl.status = "downloading"
    engine._emit("changed", dl.id)
    if needs_ffmpeg:
        if not ffmpeg:
            raise RuntimeError("This stream is encrypted or fragmented MP4; install ffmpeg to download it")
        return _ffmpeg_direct(engine, dl, ffmpeg, media_url)

    work = Path(str(dl.part_path) + ".d")
    work.mkdir(parents=True, exist_ok=True)
    done = set(dl.hls_done)
    total = len(segs)
    sizes = {}
    lock = threading.Lock()

    def fetch(idx):
        if dl.stop_event.is_set():
            return
        out = work / f"{idx:06d}.ts"
        if idx in done and out.exists():
            return
        opener = net.build_opener(engine.settings["proxy"])
        last = None
        for attempt in range(engine.settings["retries"]):
            if dl.stop_event.is_set():
                return
            try:
                with opener.open(net.make_request(segs[idx], dl.headers), timeout=engine.settings["timeout"]) as r, \
                        open(out, "wb") as f:
                    while chunk := r.read(64 * 1024):
                        engine.limiter.consume(len(chunk))
                        dl.speed_limiter.consume(len(chunk))
                        f.write(chunk)
                        dl.meter.add(len(chunk))
                        with lock:
                            dl.downloaded += len(chunk)
                with lock:
                    done.add(idx)
                    dl.hls_done = sorted(done)
                    sizes[idx] = out.stat().st_size
                    # Estimate total size from the average segment so far.
                    avg = sum(sizes.values()) / len(sizes)
                    dl.size = int(avg * total)
                return
            except Exception as e:
                last = e
        raise RuntimeError(f"Segment {idx} failed: {last}")

    dl.downloaded = sum((work / f"{i:06d}.ts").stat().st_size for i in done if (work / f"{i:06d}.ts").exists())
    dl.active_connections = min(engine.settings["connections"], total)
    with ThreadPoolExecutor(max_workers=dl.active_connections) as pool:
        for fut in [pool.submit(fetch, i) for i in range(total)]:
            fut.result()
    if dl.stop_event.is_set():
        return

    dl.status = "merging"
    engine._emit("changed", dl.id)
    joined = work / "joined.ts"
    with open(joined, "wb") as out:
        for i in range(total):
            with open(work / f"{i:06d}.ts", "rb") as f:
                shutil.copyfileobj(f, out)
    produced = joined
    if ffmpeg and dl.filename.lower().endswith((".mp4", ".mkv", ".m4v")):
        remuxed = work / ("out" + Path(dl.filename).suffix)
        r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(joined), "-c", "copy",
                            "-bsf:a", "aac_adtstoasc", str(remuxed)],
                           capture_output=True, creationflags=_NO_WINDOW)
        if r.returncode == 0:
            produced = remuxed
    if produced is joined and not dl.filename.lower().endswith(".ts"):
        dl.filename = Path(dl.filename).stem + ".ts"
    engine._finish(dl, produced)
    shutil.rmtree(work, ignore_errors=True)
    dl.hls_done = []


def _ffmpeg_direct(engine, dl, ffmpeg, url):
    out = Path(str(dl.part_path) + Path(dl.filename).suffix)
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
    hdr = ffmpeg_headers(dl.headers)
    if hdr:
        cmd += ["-headers", hdr]
    cmd += ["-i", url, "-c", "copy", str(out)]
    dl.active_connections = 1
    dl.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  creationflags=_NO_WINDOW)
    last = 0
    for line in dl.process.stdout:
        if line.startswith("total_size="):
            try:
                n = int(line.split("=")[1])
            except ValueError:
                continue
            dl.meter.add(max(0, n - last))
            dl.downloaded = last = n
    dl.process.wait()
    if dl.stop_event.is_set():
        out.unlink(missing_ok=True)
        return
    if dl.process.returncode != 0:
        raise RuntimeError("ffmpeg: " + (dl.process.stderr.read().strip()[-300:] or "failed"))
    engine._finish(dl, out)


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
