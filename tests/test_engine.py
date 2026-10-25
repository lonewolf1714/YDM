"""End-to-end engine tests against a local server with Range support.

Run:  python -m unittest discover -s tests -v
"""
import hashlib
import os
import random
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["YDM_HOME"] = tempfile.mkdtemp(prefix="ydm-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ydm import config, engine  # noqa: E402

PAYLOAD = random.Random(1).randbytes(6 * 1024 * 1024 + 123)
SEGMENTS = [random.Random(i).randbytes(40_000 + i) for i in range(12)]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    delay = 0.0  # seconds per 64 KiB, to make downloads slow enough to interrupt

    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/octet-stream", ranges=True):
        rng = self.headers.get("Range")
        if ranges and rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else len(body) - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            chunk = body[start:end + 1]
        else:
            self.send_response(200)
            chunk = body
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(chunk)))
        if self.path.startswith("/named"):
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.bin")
        self.end_headers()
        try:
            for i in range(0, len(chunk), 65536):
                self.wfile.write(chunk[i:i + 65536])
                if Handler.delay:
                    time.sleep(Handler.delay)
        except (ConnectionError, OSError):
            pass

    def do_GET(self):
        if self.path.startswith("/file.bin") or self.path.startswith("/named"):
            self._send(PAYLOAD)
        elif self.path.startswith("/noranges.bin"):
            self._send(PAYLOAD, ranges=False)
        elif self.path == "/master.m3u8":
            self._send(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100,RESOLUTION=320x240\nlow.m3u8\n"
                       b"#EXT-X-STREAM-INF:BANDWIDTH=900,RESOLUTION=1280x720\nhigh.m3u8\n",
                       "application/vnd.apple.mpegurl")
        elif self.path == "/high.m3u8":
            body = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n" + "".join(
                f"#EXTINF:4.0,\nseg{i}.ts\n" for i in range(len(SEGMENTS))) + "#EXT-X-ENDLIST\n"
            self._send(body.encode(), "application/vnd.apple.mpegurl")
        elif m := re.match(r"/seg(\d+)\.ts", self.path):
            self._send(SEGMENTS[int(m.group(1))], "video/mp2t")
        else:
            self.send_error(404)


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.out = Path(tempfile.mkdtemp(prefix="ydm-out-"))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Handler.delay = 0
        s = config.Settings()
        s["download_dir"] = str(self.out)
        s["min_split_size_kb"] = 256
        s["ffmpeg_path"] = "no-such-ffmpeg"  # keep HLS output as raw .ts so we can compare bytes
        self.eng = engine.Engine(s)

    def tearDown(self):
        self.eng.shutdown()

    def wait(self, dl, statuses=("complete", "error"), timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if dl.status in statuses:
                return
            time.sleep(0.05)
        self.fail(f"timed out in status {dl.status}")

    def test_probe(self):
        info = self.eng.probe(self.base + "/named", {})
        self.assertEqual(info["size"], len(PAYLOAD))
        self.assertTrue(info["resumable"])
        self.assertEqual(info["filename"], "résumé.bin")

    def test_segmented_download(self):
        dl = self.eng.add(self.base + "/file.bin")
        self.wait(dl)
        self.assertEqual(dl.status, "complete", dl.error)
        self.assertEqual(hashlib.md5(dl.path.read_bytes()).digest(), hashlib.md5(PAYLOAD).digest())
        self.assertFalse(dl.part_path.exists())

    def test_dynamic_split_keeps_connections_busy(self):
        Handler.delay = 0.01
        self.eng.settings["connections"] = 4
        dl = self.eng.add(self.base + "/file.bin?split")
        self.wait(dl)
        self.assertEqual(dl.status, "complete", dl.error)
        self.assertEqual(dl.path.read_bytes(), PAYLOAD)

    def test_stop_and_resume(self):
        Handler.delay = 0.02
        dl = self.eng.add(self.base + "/file.bin?resume")
        while dl.progress_bytes() < 1_000_000:
            time.sleep(0.05)
        self.eng.stop(dl.id)
        dl.thread.join(10)
        self.assertEqual(dl.status, "stopped")
        got = dl.progress_bytes()
        self.assertGreater(got, 0)
        # Simulate an app restart: a new engine loads the saved segment map.
        self.eng.save()
        self.eng.shutdown()
        Handler.delay = 0
        self.eng = engine.Engine(self.eng.settings)
        dl2 = self.eng.downloads[dl.id]
        self.assertGreaterEqual(dl2.progress_bytes(), got)
        self.eng.start(dl2.id)
        self.wait(dl2)
        self.assertEqual(dl2.status, "complete", dl2.error)
        self.assertEqual(dl2.path.read_bytes(), PAYLOAD)

    def test_server_without_ranges(self):
        dl = self.eng.add(self.base + "/noranges.bin")
        self.assertFalse(dl.resumable)
        self.wait(dl)
        self.assertEqual(dl.status, "complete", dl.error)
        self.assertEqual(dl.path.read_bytes(), PAYLOAD)

    def test_duplicate_names_get_suffix(self):
        a = self.eng.add(self.base + "/file.bin?dup", filename="same.bin", start=False)
        b = self.eng.add(self.base + "/file.bin?dup", filename="same.bin", start=False)
        self.assertEqual(a.filename, "same.bin")
        self.assertEqual(b.filename, "same_2.bin")

    def test_hls_master_picks_best_variant(self):
        dl = self.eng.add(self.base + "/master.m3u8", filename="clip.ts")
        self.assertEqual(dl.kind, "hls")
        self.wait(dl)
        self.assertEqual(dl.status, "complete", dl.error)
        self.assertEqual(dl.path.read_bytes(), b"".join(SEGMENTS))

    def test_speed_limit(self):
        self.eng.set_speed_limit(1024)  # 1 MiB/s for 6 MiB -> at least ~5 s
        t = time.time()
        dl = self.eng.add(self.base + "/file.bin?limited")
        self.wait(dl, timeout=30)
        self.assertGreater(time.time() - t, 4)
        self.assertEqual(dl.path.read_bytes(), PAYLOAD)

    def test_queue_respects_concurrency(self):
        Handler.delay = 0.01
        self.eng.settings["max_concurrent"] = 2
        dls = [self.eng.add(self.base + f"/file.bin?q{i}", start=False, in_queue=True) for i in range(4)]
        self.eng.start_queue()
        peak = 0
        while not all(d.status == "complete" for d in dls):
            peak = max(peak, sum(1 for d in dls if d.status in ("connecting", "downloading")))
            self.assertFalse(any(d.status == "error" for d in dls))
            time.sleep(0.05)
        self.assertLessEqual(peak, 2)
        self.assertGreaterEqual(peak, 1)


if __name__ == "__main__":
    unittest.main()
