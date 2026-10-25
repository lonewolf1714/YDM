"""Chrome/Edge/Firefox native messaging host.

The browser starts this process and talks to it over stdin/stdout using
length-prefixed JSON. It relays each message to the running app's loopback API,
starting the app first if it is not running.
"""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ydm_app  # noqa: E402
from ydm import api  # noqa: E402


def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (length,) = struct.unpack("<I", raw)
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send_message(obj):
    data = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def ensure_app(start=True):
    if ydm_app.app_running():
        return True
    return start and ydm_app.launch_background()


def handle(msg):
    kind = msg.get("type")
    if kind == "ping":
        # Don't launch the app just because the extension checks on it.
        if not ensure_app(start=msg.get("launch", False)):
            return {"ok": False, "error": "YDM is not running"}
        return api.call("/ping")
    if not ensure_app():
        return {"ok": False, "error": "Could not start YDM"}
    if kind == "add":
        return api.call("/add", msg.get("payload", {}))
    if kind == "batch":
        return api.call("/batch", msg.get("payload", {}))
    if kind == "formats":
        return api.call("/formats", msg.get("payload", {}), timeout=90)
    if kind == "show":
        return api.call("/show", {})
    return {"ok": False, "error": f"unknown message type {kind!r}"}


def main():
    while True:
        msg = read_message()
        if msg is None:
            return
        try:
            reply = handle(msg)
        except Exception as e:
            reply = {"ok": False, "error": str(e)}
        reply["id"] = msg.get("id")
        send_message(reply)


if __name__ == "__main__":
    main()
