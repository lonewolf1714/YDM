"""Native messaging host -> loopback API round trip, using the real stdin/stdout protocol."""
import json
import os
import queue
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("YDM_HOME", tempfile.mkdtemp(prefix="ydm-test-"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ydm import api, config, engine  # noqa: E402


def frame(obj):
    data = json.dumps(obj).encode()
    return struct.pack("<I", len(data)) + data


def unframe(raw):
    out = []
    while raw:
        (n,) = struct.unpack("<I", raw[:4])
        out.append(json.loads(raw[4:4 + n]))
        raw = raw[4 + n:]
    return out


class NativeHostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = engine.Engine(config.Settings())
        cls.ui = queue.Queue()
        cls.server = api.ApiServer(cls.eng, cls.ui)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.eng.shutdown()

    def run_host(self, *messages):
        p = subprocess.run([sys.executable, str(ROOT / "ydm" / "native_host.py")],
                           input=b"".join(frame(m) for m in messages), capture_output=True,
                           env=dict(os.environ), timeout=30)
        self.assertEqual(p.stderr, b"")
        return unframe(p.stdout)

    def test_ping_and_add(self):
        replies = self.run_host(
            {"id": 1, "type": "ping"},
            {"id": 2, "type": "add", "payload": {"url": "https://example.com/a.zip", "referer": "https://example.com/"}},
            {"id": 3, "type": "nonsense"},
        )
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertTrue(replies[0]["ok"])
        self.assertIn("intercept_extensions", replies[0]["settings"])
        self.assertTrue(replies[1]["ok"])
        self.assertFalse(replies[2]["ok"])
        kind, payload = self.ui.get(timeout=2)
        self.assertEqual(kind, "add")
        self.assertEqual(payload["url"], "https://example.com/a.zip")

    def test_rejects_wrong_token(self):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://{config.API_HOST}:{config.API_PORT}/ping",
                                     headers={"X-YDM-Token": "wrong"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(cm.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
