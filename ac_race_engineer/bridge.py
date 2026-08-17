"""Localhost HTTP bridge for the in-game CSP Lua app.

The Lua app can't speak MCP stdio, so this thread exposes a minimal JSON API
on 127.0.0.1 that it polls and posts to:

    GET  /status  -> collector state + pending message from Claude
    POST /note    -> driver complaint tag at current track position
    POST /ack     -> dismiss the currently displayed message

Messages flow the other way via the send_driver_message MCP tool: Claude sets
a message, the app shows it on the next poll, the driver dismisses it.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db

VALID_TAGS = {"understeer", "oversteer", "braking", "traction", "note"}


class Bridge:
    def __init__(self, db_path, collector, port: int = 9666):
        self._db_path = db_path
        self._collector = collector
        self._port = port
        self._lock = threading.Lock()
        self._message: dict | None = None
        self._msg_seq = 0
        self._server: ThreadingHTTPServer | None = None
        self.error: str | None = None

    # -- message slot (Claude -> driver) --------------------------------

    def set_message(self, text: str) -> int:
        with self._lock:
            self._msg_seq += 1
            self._message = {"id": self._msg_seq, "text": text}
            return self._msg_seq

    def get_message(self) -> dict | None:
        with self._lock:
            return dict(self._message) if self._message else None

    def ack_message(self, msg_id: int) -> bool:
        with self._lock:
            if self._message and self._message["id"] == msg_id:
                self._message = None
                return True
            return False

    # -- HTTP -----------------------------------------------------------

    def start(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep stdio clean: it's MCP's channel
                pass

            def _send(self, code: int, obj: dict):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict | None:
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    return json.loads(self.rfile.read(n)) if n else {}
                except (ValueError, json.JSONDecodeError):
                    return None

            def do_GET(self):
                if self.path != "/status":
                    return self._send(404, {"error": "unknown path"})
                c = bridge._collector
                self._send(200, {
                    "running": c.running,
                    "status": c.status,
                    "session_id": c.session_id,
                    "laps_recorded": c.laps_recorded,
                    "message": bridge.get_message(),
                })

            def do_POST(self):
                body = self._body()
                if body is None:
                    return self._send(400, {"error": "bad JSON"})

                if self.path == "/note":
                    tag = str(body.get("tag", "")).lower()
                    if tag not in VALID_TAGS:
                        return self._send(400, {"error": f"tag must be one "
                                                f"of {sorted(VALID_TAGS)}"})
                    try:
                        spline = float(body.get("spline", -1))
                        lap_count = int(body.get("lap_count", -1))
                        speed = float(body.get("speed_kmh", 0))
                    except (TypeError, ValueError):
                        return self._send(400, {"error": "bad field types"})
                    # Short-lived connection per request: rate is a few per
                    # lap, and it keeps each thread's SQLite usage isolated.
                    conn = db.connect(bridge._db_path)
                    try:
                        note_id = db.add_note(
                            conn, bridge._collector.session_id,
                            lap_count, spline, tag, speed)
                    finally:
                        conn.close()
                    return self._send(200, {"ok": True, "id": note_id})

                if self.path == "/ack":
                    ok = bridge.ack_message(int(body.get("id", -1)))
                    return self._send(200, {"ok": ok})

                return self._send(404, {"error": "unknown path"})

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self._port),
                                               Handler)
        except OSError as e:
            self.error = f"bridge failed to bind port {self._port}: {e}"
            return
        self._port = self._server.server_address[1]  # resolve port=0
        threading.Thread(target=self._server.serve_forever,
                         daemon=True).start()

    @property
    def port(self) -> int:
        return self._port

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server = None
