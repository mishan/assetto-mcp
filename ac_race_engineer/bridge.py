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
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db

VALID_TAGS = {"understeer", "oversteer", "braking", "traction", "note"}

# Sanity ceiling for speed_kmh. Nothing in AC gets near this; anything above
# it means the client sent garbage, not a fast car.
MAX_SPEED_KMH = 1000.0


class FieldError(ValueError):
    """A request field was missing, the wrong type, or out of range."""


def _req_float(body: dict, key: str, lo: float, hi: float) -> float:
    """Coerce body[key] to a finite float within [lo, hi], or raise."""
    if key not in body:
        raise FieldError(f"missing field '{key}'")
    raw = body[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise FieldError(f"'{key}' must be a number")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise FieldError(f"'{key}' must be a number, got {raw!r}")
    if not math.isfinite(val):
        raise FieldError(f"'{key}' must be finite, got {raw!r}")
    if not (lo <= val <= hi):
        raise FieldError(f"'{key}' must be between {lo} and {hi}, got {val}")
    return val


def _req_int(body: dict, key: str, lo: int, hi: int) -> int:
    """Coerce body[key] to an int within [lo, hi], or raise.

    Accepts a float only when it is integral (3.0 yes, 3.7 no) so a client
    bug can't silently truncate a lap number.
    """
    if key not in body:
        raise FieldError(f"missing field '{key}'")
    raw = body[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise FieldError(f"'{key}' must be an integer")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise FieldError(f"'{key}' must be an integer, got {raw!r}")
    if not math.isfinite(val) or val != int(val):
        raise FieldError(f"'{key}' must be a whole number, got {raw!r}")
    val = int(val)
    if not (lo <= val <= hi):
        raise FieldError(f"'{key}' must be between {lo} and {hi}, got {val}")
    return val


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

            _responded = False

            def _send(self, code: int, obj: dict):
                # Guard against a second response on the same connection: if
                # the client vanished mid-write, the error handler must not
                # try to write another status line onto a half-sent reply.
                if self._responded:
                    return
                self._responded = True
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict | None:
                """Parsed JSON object body, or None if malformed.

                A non-object body (list, number, bare string) is treated as
                malformed too: every endpoint here reads named fields.
                """
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    parsed = json.loads(self.rfile.read(n)) if n else {}
                except ValueError:  # covers json.JSONDecodeError
                    return None
                return parsed if isinstance(parsed, dict) else None

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
                # The Lua app has no way to surface a dropped connection, so
                # never let an unexpected error escape as a bare traceback.
                try:
                    return self._do_post()
                except Exception as e:  # noqa: BLE001
                    return self._send(500, {"error": f"{type(e).__name__}: {e}"})

            def _do_post(self):
                body = self._body()
                if body is None:
                    return self._send(400, {"error": "bad JSON"})

                if self.path == "/note":
                    tag = str(body.get("tag", "")).lower()
                    if tag not in VALID_TAGS:
                        return self._send(400, {"error": f"tag must be one "
                                                f"of {sorted(VALID_TAGS)}"})
                    # Ranges matter downstream: analysis.py matches notes to
                    # corners by spline position, so an out-of-range value
                    # would silently never correlate with anything.
                    try:
                        spline = _req_float(body, "spline", 0.0, 1.0)
                        lap_count = _req_int(body, "lap_count", 0, 100_000)
                        speed = _req_float(body, "speed_kmh",
                                           0.0, MAX_SPEED_KMH)
                    except FieldError as e:
                        return self._send(400, {"error": str(e)})
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
                    try:
                        msg_id = _req_int(body, "id", 1, 2**31 - 1)
                    except FieldError as e:
                        return self._send(400, {"error": str(e)})
                    # ok=False is not an error: it just means the driver
                    # dismissed a message Claude had already replaced.
                    return self._send(200, {"ok": bridge.ack_message(msg_id)})

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
