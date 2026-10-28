"""Download engine.

HTTP downloads use several connections on one pre-allocated file. Whenever a
connection finishes its part and others are still running, the largest
remaining part is cut in half and a new connection takes the second half
("dynamic segmentation"), so all connections stay busy until the very end.
Segment state is saved, so stopped or crashed downloads resume where they were.
"""
import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path

from . import config, net

CHUNK = 64 * 1024


class Segment:
    __slots__ = ("start", "end", "pos", "busy")

    def __init__(self, start, end, pos=None):
        self.start = start
        self.end = end          # inclusive; None when the size is unknown
        self.pos = start if pos is None else pos
        self.busy = False

    @property
    def done(self):
        return self.end is not None and self.pos > self.end

    @property
    def remaining(self):
        return 0 if self.end is None else max(0, self.end - self.pos + 1)

    def to_list(self):
        return [self.start, self.end, self.pos]


class Download:
    FIELDS = ("id", "url", "filename", "directory", "size", "resumable", "status",
              "headers", "description", "category", "added", "finished", "error",
              "kind", "page_url", "connections", "in_queue", "downloaded", "format",
              "hls_done", "media_title")

    def __init__(self, **kw):
        self.id = kw.get("id") or uuid.uuid4().hex[:12]
        self.url = kw["url"]
        self.filename = kw.get("filename") or "download"
        self.directory = kw.get("directory") or ""
        self.size = kw.get("size", -1)
        self.resumable = kw.get("resumable", False)
        self.status = kw.get("status", "stopped")
        self.headers = kw.get("headers") or {}
        self.description = kw.get("description", "")
        self.category = kw.get("category") or config.category_for(self.filename)
        self.added = kw.get("added") or time.time()
        self.finished = kw.get("finished")
        self.error = kw.get("error", "")
        self.kind = kw.get("kind", "http")       # http | hls | ytdlp
        self.page_url = kw.get("page_url", "")
        self.connections = kw.get("connections", 0)
        self.in_queue = kw.get("in_queue", False)
        self.downloaded = kw.get("downloaded", 0)
        self.format = kw.get("format", "")
        self.hls_done = kw.get("hls_done") or []
        self.media_title = kw.get("media_title", "")
        self.segments = [Segment(*s) for s in kw.get("segments", [])]
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.meter = net.RateMeter()
        self.speed_limiter = net.SpeedLimiter(0)  # per-download limit, on top of the global one
        self.active_connections = 0
        self.process = None   # yt-dlp / ffmpeg subprocess
        self.cookie_jar = kw.get("cookie_jar")  # browser cookies for yt-dlp; kept in memory, never saved

    # ---- paths ----
    @property
    def path(self) -> Path:
        return Path(self.directory) / self.filename

    @property
    def part_path(self) -> Path:
        return Path(self.directory) / (self.filename + ".ydm")

    # ---- progress ----
    def progress_bytes(self):
        if self.kind == "http" and self.segments:
            return sum(s.pos - s.start for s in self.segments)
        return self.downloaded

    def percent(self):
        if self.status == "complete":
            return 100.0
        if self.size and self.size > 0:
            return min(100.0, 100.0 * self.progress_bytes() / self.size)
        return 0.0

    def speed(self):
        return self.meter.rate() if self.status == "downloading" else 0.0

    def eta(self):
        sp = self.speed()
        if sp <= 0 or self.size <= 0:
            return None
        return max(0, (self.size - self.progress_bytes()) / sp)

    def to_dict(self):
        d = {f: getattr(self, f) for f in self.FIELDS}
        d["segments"] = [s.to_list() for s in self.segments]
        d["downloaded"] = self.progress_bytes()
        return d


class Engine:
    def __init__(self, settings: config.Settings = None):
        self.settings = settings or config.Settings()
        self.downloads: dict[str, Download] = {}
        self.order: list[str] = []
        self.lock = threading.RLock()
        self.events: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.limiter = net.SpeedLimiter(self.settings["speed_limit_kbps"] * 1024)
        self.queue_running = False
        self._stop = threading.Event()
        self._load()
        self._ticker = threading.Thread(target=self._tick_loop, daemon=True, name="engine-tick")
        self._ticker.start()

    # ---------- persistence ----------
    def _load(self):
        if not config.DOWNLOADS_FILE.exists():
            return
        try:
            items = json.loads(config.DOWNLOADS_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            return
        for it in items:
            dl = Download(**it)
            if dl.status not in ("complete", "error"):
                dl.status = "stopped"
            self.downloads[dl.id] = dl
            self.order.append(dl.id)

    def save(self):
        with self.lock:
            data = [self.downloads[i].to_dict() for i in self.order if i in self.downloads]
        tmp = config.DOWNLOADS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), "utf-8")
        os.replace(tmp, config.DOWNLOADS_FILE)

    def shutdown(self):
        self._stop.set()
        for dl in list(self.downloads.values()):
            self.stop(dl.id)
        for dl in list(self.downloads.values()):
            if dl.thread:
                dl.thread.join(timeout=5)
        self.save()

    def _emit(self, event, dl_id):
        self.events.put((event, dl_id))

    # ---------- public API ----------
    def headers_for(self, headers=None, referer="", cookies="", user_agent=""):
        h = {"User-Agent": user_agent or self.settings["user_agent"]}
        if referer:
            h["Referer"] = referer
        if cookies:
            h["Cookie"] = cookies
        h.update(headers or {})
        return h

    def probe(self, url, headers):
        return net.probe(url, headers, timeout=self.settings["timeout"], proxy=self.settings["proxy"])

    def unique_name(self, directory: Path, name: str) -> str:
        """Avoid clobbering existing files or other queued downloads (IDM appends _2, _3...)."""
        if self.settings["duplicate_action"] == "overwrite":
            return name
        taken = {(Path(d.directory) / d.filename).as_posix().lower()
                 for d in self.downloads.values() if d.status != "complete"}
        stem, suffix = Path(name).stem, Path(name).suffix
        candidate, n = name, 1
        while (directory / candidate).exists() or (directory / candidate).as_posix().lower() in taken:
            n += 1
            candidate = f"{stem}_{n}{suffix}"
        return candidate

    def add(self, url, filename="", directory="", headers=None, start=True, kind="http",
            size=-1, resumable=None, description="", page_url="", fmt="", in_queue=False,
            probe_info=None, media_title="", cookie_jar=None):
        headers = headers or self.headers_for()
        info = probe_info
        if info is None and kind == "http":
            try:
                info = self.probe(url, headers)
            except Exception as e:  # still add it; the start will retry and report
                info = {"size": -1, "resumable": False, "filename": "", "error": str(e)}
        info = info or {}
        if info.get("kind") == "hls":
            kind = "hls"
        name = net.sanitize_filename(filename or info.get("filename") or net.filename_from_url(url) or "download")
        if kind == "hls" and name.lower().endswith(".m3u8"):
            name = name[:-5] + ".mp4"
        if kind == "ytdlp" and not Path(name).suffix:
            name += ".mp4"
        category = config.category_for(name)
        directory = Path(directory) if directory else self.settings.target_dir(category)
        directory.mkdir(parents=True, exist_ok=True)
        dl = Download(url=url, filename=self.unique_name(directory, name), directory=str(directory),
                      size=size if size and size > 0 else info.get("size", -1),
                      resumable=info.get("resumable", False) if resumable is None else resumable,
                      headers=headers, kind=kind, description=description, page_url=page_url,
                      format=fmt, in_queue=in_queue, category=category, media_title=media_title,
                      cookie_jar=cookie_jar)
        with self.lock:
            self.downloads[dl.id] = dl
            self.order.append(dl.id)
        self._emit("added", dl.id)
        self.save()
        if start:
            self.start(dl.id)
        elif in_queue:
            dl.status = "queued" if self.queue_running else "stopped"
        return dl

    def start(self, dl_id):
        dl = self.downloads.get(dl_id)
        if not dl or (dl.thread and dl.thread.is_alive()) or dl.status == "complete":
            return
        dl.stop_event.clear()
        dl.error = ""
        dl.status = "connecting"
        target = {"http": self._run_http, "hls": self._run_hls, "ytdlp": self._run_ytdlp}[dl.kind]
        dl.thread = threading.Thread(target=self._guard, args=(dl, target), daemon=True, name=f"dl-{dl.id}")
        dl.thread.start()
        self._emit("changed", dl.id)

    def stop(self, dl_id):
        dl = self.downloads.get(dl_id)
        if not dl:
            return
        dl.stop_event.set()
        if dl.process and dl.process.poll() is None:
            dl.process.terminate()
        if dl.status in ("queued", "connecting", "downloading"):
            dl.status = "stopped"
        self._emit("changed", dl.id)

    def remove(self, dl_id, delete_file=False):
        dl = self.downloads.get(dl_id)
        if not dl:
            return
        self.stop(dl_id)
        if dl.thread:
            dl.thread.join(timeout=5)
        for p in (dl.part_path, Path(str(dl.part_path) + ".d")):
            try:
                if p.is_dir():
                    for f in p.iterdir():
                        f.unlink()
                    p.rmdir()
                elif p.exists():
                    p.unlink()
            except OSError:
                pass
        if delete_file and dl.path.exists():
            try:
                dl.path.unlink()
            except OSError:
                pass
        with self.lock:
            self.downloads.pop(dl_id, None)
            if dl_id in self.order:
                self.order.remove(dl_id)
        self._emit("removed", dl_id)
        self.save()

    def redownload(self, dl_id):
        dl = self.downloads[dl_id]
        dl.segments, dl.hls_done, dl.downloaded = [], [], 0
        dl.status, dl.finished = "stopped", None
        self.start(dl_id)

    def set_speed_limit(self, kbps):
        self.settings["speed_limit_kbps"] = int(kbps)
        self.limiter.set_rate(int(kbps) * 1024)

    def start_queue(self):
        self.queue_running = True
        for dl in self.downloads.values():
            if dl.in_queue and dl.status in ("stopped", "error"):
                dl.status = "queued"
        self._emit("queue", "")

    def stop_queue(self):
        self.queue_running = False
        for dl in list(self.downloads.values()):
            if dl.in_queue and dl.status in ("queued", "connecting", "downloading"):
                self.stop(dl.id)
        self._emit("queue", "")

    def running(self):
        return [d for d in self.downloads.values() if d.status in ("connecting", "downloading", "merging")]

    # ---------- background loop ----------
    def _tick_loop(self):
        last_save = time.monotonic()
        while not self._stop.wait(0.5):
            if self.queue_running:
                active = [d for d in self.running() if d.in_queue]
                slots = self.settings["max_concurrent"] - len(active)
                for i in list(self.order):
                    if slots <= 0:
                        break
                    d = self.downloads.get(i)
                    if d and d.in_queue and d.status == "queued":
                        self.start(d.id)
                        slots -= 1
                if not any(d.in_queue and d.status in ("queued", "connecting", "downloading", "merging")
                           for d in self.downloads.values()):
                    self.queue_running = False
                    self._emit("queue_done", "")
            if time.monotonic() - last_save > 5 and self.running():
                self.save()
                last_save = time.monotonic()

    def _guard(self, dl, target):
        try:
            target(dl)
        except Exception as e:
            if not dl.stop_event.is_set():
                dl.status, dl.error = "error", str(e) or e.__class__.__name__
        finally:
            dl.active_connections = 0
            if dl.status in ("connecting", "downloading", "merging"):
                dl.status = "stopped"
            self.save()
            self._emit("complete" if dl.status == "complete" else "changed", dl.id)

    def _finish(self, dl, produced: Path):
        final = dl.path
        if final.exists() and final != produced:
            final.unlink()
        os.replace(produced, final)
        dl.status, dl.finished = "complete", time.time()
        if dl.size <= 0:
            dl.size = final.stat().st_size
        dl.downloaded = dl.size

    # ---------- HTTP (segmented) ----------
    def _run_http(self, dl: Download):
        if not dl.segments or (dl.size <= 0 and not dl.resumable):
            # (Re)probe: on a first start, or for streams we could not range before.
            info = self.probe(dl.url, dl.headers)
            dl.size, dl.resumable = info["size"], info["resumable"]
            dl.segments = []
        part = dl.part_path
        if not dl.segments:
            n = max(1, int(self.settings["connections"])) if dl.resumable and dl.size > 0 else 1
            if dl.size > 0:
                min_split = self.settings["min_split_size_kb"] * 1024
                n = max(1, min(n, dl.size // max(min_split, 1) or 1))
                step = dl.size // n
                bounds = [i * step for i in range(n)] + [dl.size]
                dl.segments = [Segment(bounds[i], bounds[i + 1] - 1) for i in range(n)]
            else:
                dl.segments = [Segment(0, None)]
            with open(part, "wb") as f:
                if dl.size > 0:
                    f.truncate(dl.size)  # sparse pre-allocation
        elif not part.exists():
            raise RuntimeError("Temporary file is missing; use Redownload")
        if not dl.resumable:
            # A server without ranges cannot continue: start over with one connection.
            dl.segments = [Segment(0, dl.size - 1 if dl.size > 0 else None)]
            open(part, "wb").close()

        dl.status = "downloading"
        self._emit("changed", dl.id)
        max_conn = max(1, int(self.settings["connections"])) if dl.resumable else 1
        min_split = max(64 * 1024, self.settings["min_split_size_kb"] * 1024)
        errors = {"count": 0, "last": ""}
        threads = []

        def launch(seg):
            seg.busy = True
            t = threading.Thread(target=self._segment_worker, args=(dl, seg, errors), daemon=True)
            threads.append(t)
            t.start()

        while not dl.stop_event.is_set():
            if errors["count"] > self.settings["retries"]:
                dl.stop_event.set()
                for t in threads:
                    t.join(timeout=5)
                raise RuntimeError(errors["last"] or "Too many connection errors")
            with dl.lock:
                if all(s.done for s in dl.segments):
                    break
                busy = sum(1 for s in dl.segments if s.busy)
                for s in dl.segments:
                    if busy >= max_conn:
                        break
                    if not s.busy and not s.done and errors["count"] <= self.settings["retries"]:
                        launch(s)
                        busy += 1
                # dynamic segmentation: split the biggest remaining range for an idle slot
                while busy < max_conn and dl.resumable:
                    cand = max((s for s in dl.segments if s.busy and s.end is not None),
                               key=lambda s: s.remaining, default=None)
                    if not cand or cand.remaining < 2 * min_split:
                        break
                    mid = cand.pos + cand.remaining // 2
                    new = Segment(mid, cand.end)
                    cand.end = mid - 1
                    dl.segments.append(new)
                    dl.segments.sort(key=lambda s: s.start)
                    launch(new)
                    busy += 1
                dl.active_connections = busy
            time.sleep(0.2)

        for t in threads:
            t.join(timeout=10)
        if dl.stop_event.is_set() and not all(s.done for s in dl.segments if s.end is not None):
            return
        if dl.size <= 0:
            dl.size = part.stat().st_size
        self._finish(dl, part)
        dl.segments = []

    def _segment_worker(self, dl: Download, seg: Segment, errors):
        opener = net.build_opener(self.settings["proxy"])
        attempt = 0
        try:
            while not dl.stop_event.is_set() and not seg.done:
                try:
                    use_range = dl.resumable and (seg.pos > 0 or seg.end is not None and seg.end < dl.size - 1)
                    req = net.make_request(dl.url, dl.headers, seg.pos if use_range else None,
                                           seg.end if use_range else None)
                    with opener.open(req, timeout=self.settings["timeout"]) as resp:
                        if use_range and resp.status != 206:
                            raise RuntimeError("Server stopped honouring byte ranges")
                        with open(dl.part_path, "r+b") as f:
                            f.seek(seg.pos)
                            while not dl.stop_event.is_set():
                                with dl.lock:
                                    want = CHUNK if seg.end is None else min(CHUNK, seg.end - seg.pos + 1)
                                if want <= 0:
                                    break
                                data = resp.read(want)
                                if not data:
                                    if seg.end is None:
                                        seg.end = seg.pos - 1  # stream of unknown length ended
                                    break
                                self.limiter.consume(len(data))
                                dl.speed_limiter.consume(len(data))
                                with dl.lock:
                                    # The splitter may have shortened this segment meanwhile.
                                    if seg.end is not None:
                                        data = data[:max(0, seg.end - seg.pos + 1)]
                                    f.seek(seg.pos)
                                    f.write(data)
                                    seg.pos += len(data)
                                dl.meter.add(len(data))
                                attempt = 0
                    if not seg.done and seg.end is not None and not dl.stop_event.is_set():
                        raise RuntimeError("Connection closed early")
                except Exception as e:
                    if dl.stop_event.is_set():
                        break
                    errors["count"] += 1
                    errors["last"] = str(e)
                    attempt += 1
                    if not dl.resumable:
                        errors["count"] = 1 << 30  # nothing to resume from: fail the download
                        break
                    time.sleep(min(10, 0.5 * 2 ** attempt))
        finally:
            seg.busy = False

    # ---------- other kinds ----------
    def _run_hls(self, dl):
        from . import hls
        hls.download(self, dl)

    def _run_ytdlp(self, dl):
        from . import ytdlp
        ytdlp.download(self, dl)
