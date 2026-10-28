"""Loopback HTTP API used by the native messaging host (and the CLI) to reach the running app.

Every request must carry the token from %APPDATA%\\YDM\\api_token, so other
local web pages cannot push downloads into the app.
"""
import hmac
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, ytdlp

VERSION = "1.2.0"


class ExclusiveServer(ThreadingHTTPServer):
    """Fail instead of sharing the port: on Windows SO_REUSEADDR lets a second process bind
    the same port, and requests then land in whichever instance Windows picks."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class ApiServer:
    """`on_request(kind, payload)` runs in the UI thread via the queue the GUI drains."""

    def __init__(self, engine, ui_queue):
        self.engine = engine
        self.ui_queue = ui_queue
        self.token = config.api_token()
        self.httpd = None

    def start(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                return hmac.compare_digest(self.headers.get("X-YDM-Token", ""), api.token)

            def do_GET(self):
                if not self._authorized():
                    return self._reply(403, {"error": "bad token"})
                if self.path == "/ping":
                    return self._reply(200, {"ok": True, "version": VERSION,
                                             "ytdlp": ytdlp.available(api.engine),
                                             "settings": {k: api.engine.settings[k] for k in (
                                                 "browser_integration", "intercept_extensions",
                                                 "min_intercept_size_kb")}})
                if self.path == "/list":
                    return self._reply(200, {"downloads": [
                        {"id": d.id, "filename": d.filename, "status": d.status, "percent": d.percent()}
                        for d in api.engine.downloads.values()]})
                self._reply(404, {"error": "not found"})

            def do_POST(self):
                if not self._authorized():
                    return self._reply(403, {"error": "bad token"})
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._reply(400, {"error": "bad json"})
                if self.path in ("/add", "/batch", "/show", "/quit"):
                    api.ui_queue.put((self.path.strip("/"), payload))
                    return self._reply(200, {"ok": True})
                if self.path == "/formats":
                    try:
                        headers = api.engine.headers_for(referer=payload.get("referer", ""),
                                                         cookies=payload.get("cookies", ""))
                        return self._reply(200, ytdlp.formats(api.engine, payload["url"], headers,
                                                                  cookie_jar=payload.get("cookie_jar")))
                    except Exception as e:
                        return self._reply(200, {"error": str(e)})
                self._reply(404, {"error": "not found"})

        self.httpd = ExclusiveServer((config.API_HOST, config.API_PORT), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="api").start()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()


def call(path, payload=None, timeout=5):
    """Client side, used by the CLI and the native host."""
    import urllib.request
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(f"http://{config.API_HOST}:{config.API_PORT}{path}", data=data,
                                 method="POST" if data is not None else "GET")
    req.add_header("X-YDM-Token", config.api_token())
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())
