"""Localhost HTTP bridge for the in-game CSP Lua app.

The Lua app can't speak MCP stdio, so this thread exposes a minimal JSON API
on 127.0.0.1 that it polls and posts to:

    GET  /status  -> collector state + pending message from Claude
    POST /note    -> driver complaint tag at current track position
    POST /rivals  -> batched opponent telemetry (AC's shared memory is
                     ego-only, so this is the only way to see other cars)
    POST /ack     -> dismiss the currently displayed message

Messages flow the other way via the send_driver_message MCP tool: Claude sets
a message, the app shows it on the next poll, the driver dismisses it.
"""

import json
import math
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that refuses to share its port.

    HTTPServer sets allow_reuse_address = 1, which on Unix means the benign
    "rebind a port still in TIME_WAIT". On Windows SO_REUSEADDR means
    something else entirely: it lets a second process bind a port another
    process is *actively listening* on. Both binds succeed, neither reports
    an error, and which socket receives a given connection is arbitrary.

    That is a real failure mode here. If a previous server process outlives
    its parent -- Claude Desktop leaving an orphan behind, say -- the stale
    process keeps answering the in-game app's /status polls with its own
    idle collector, while the live process serves MCP. The overlay then
    reports "connected, not recording" even though recording is fine.

    Fail loudly on a port clash instead, so bridge_status surfaces it.
    """

    allow_reuse_address = False

    def server_bind(self):
        if sys.platform == "win32":
            # Belt and braces: SO_EXCLUSIVEADDRUSE makes the duplicate bind
            # impossible rather than merely un-requested.
            try:
                self.socket.setsockopt(
                    socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except (AttributeError, OSError):
                pass
        super().server_bind()

VALID_TAGS = {"understeer", "oversteer", "braking", "traction", "note"}

# Sanity ceiling for speed_kmh. Nothing in AC gets near this; anything above
# it means the client sent garbage, not a fast car.
MAX_SPEED_KMH = 1000.0

# A batch is one second of buffered samples across the whole grid, NOT a
# single snapshot: the Lua app samples at 10Hz and posts once a second, so a
# full 64-car server produces ~640 entries per POST. Sized to hold that with
# headroom for a delayed post, while still capping what a runaway client can
# push into the SQLite writer in one request.
MAX_RIVAL_BATCH = 2000

# Must stay above the Lua app's own RIVAL_BUFFER_MAX (1400): the client caps
# its buffer, so anything above that ceiling is a bug on one side or the
# other. tests/test_rivals.py asserts the relationship rather than the value,
# because bracketing the constant loosely is how it got sized for a single
# grid snapshot the first time.
RIVAL_BUFFER_MAX_CLIENT = 1400

# Ceiling on a request body before we even parse it. A full 1400-sample
# batch measures ~300KB; 4MB is generous for that and still bounded.
MAX_BODY_BYTES = 4 * 1024 * 1024

# How much of an oversized body we're willing to read and throw away so the
# client can receive its 400 instead of a broken pipe. Past this, hanging up
# is the right answer.
DRAIN_LIMIT_BYTES = 64 * 1024 * 1024

# Windows holds a closed listening socket in TIME_WAIT for up to four
# minutes. Since we now refuse to share the port, our own restart can be what
# blocks us, so keep retrying for longer than that before giving up.
BIND_RETRY_INTERVAL = 5.0
BIND_RETRY_SECONDS = 300.0

# How long a session in the shared DB counts as "another instance is still
# recording this". Generous: a driver can sit in the garage between runs.
OTHER_INSTANCE_STALE_SECONDS = 900.0


class FieldError(ValueError):
    """A request field was missing, the wrong type, or out of range."""


def _opt_float(raw, lo: float, hi: float) -> float | None:
    """Best-effort float within [lo, hi], or None.

    Never raises: a remote car whose inputs the server doesn't transmit is
    an expected condition, not a client error. But the bounds are not
    optional. Unbounded, a client sending 1e30 produced an int SQLite
    cannot bind, which surfaced as a 500 *and* rolled back the whole batch
    of 1400 samples -- defeating the per-item skip whose entire purpose is
    that one bad car must not cost the rest.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or not (lo <= val <= hi):
        return None
    return val


def _opt_int(raw, lo: int, hi: int) -> int | None:
    """Best-effort int within [lo, hi], or None. Integral values only.

    Truncating 3.7 to 3 would turn malformed client data into a
    plausible-but-wrong gear or lap time, which is worse than admitting the
    field is unknown.
    """
    val = _opt_float(raw, lo, hi)
    if val is None or val != int(val):
        return None
    return int(val)


# Bounds for the optional rival fields. Lap times: 24h in ms is far past
# anything real while still being a value SQLite can hold comfortably.
MAX_LAP_MS = 24 * 60 * 60 * 1000
GEAR_RANGE = (-2, 12)


def _opt_str(raw, limit: int = 64) -> str:
    """A string field, or empty. Non-strings are dropped, not repr'd."""
    return raw[:limit] if isinstance(raw, str) else ""


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
        self._handler_cls = None
        self._bind_stop = threading.Event()
        self.error: str | None = None

    # -- session resolution ---------------------------------------------

    def _resolve(self) -> dict:
        """Single source of truth for "which session is live, and whose".

        Both what the overlay shows and where inbound data is filed come
        from here, because the two disagreeing is worse than either being
        wrong. They used to: status applied a staleness window and the write
        path did not, so the overlay could report the Mugello session while
        the same request filed the driver's note into a Monza session from
        three days earlier.

        Two rules, in order:

        1. This process's collector, but only while it is actually running.
           collector.session_id survives stop(), so trusting it unguarded
           files data against a session that ended.
        2. Otherwise the newest session in the shared database, and only if
           it is recent. The app runs one server instance per client surface
           and only one wins the bridge port, so the recording instance is
           routinely not this one. Beyond the staleness window there is no
           live session and inbound data has nowhere honest to go.
        """
        c = self._collector
        if c.running and c.session_id is not None:
            return {"session_id": c.session_id, "running": True,
                    "status": c.status, "laps_recorded": c.laps_recorded,
                    "by_other": False}

        idle = {"session_id": None, "running": False, "status": c.status,
                "laps_recorded": 0, "by_other": False}
        try:
            conn = db.connect(self._db_path)
        except Exception:  # noqa: BLE001 - never take the bridge down
            return idle
        try:
            latest = db.latest_session(conn)
        except Exception:  # noqa: BLE001
            return idle
        finally:
            conn.close()
        if not latest:
            return idle

        # "Recent" has to be generous: a driver can sit in the garage for a
        # while between runs without the session having ended.
        age = time.time() - (latest["last_lap_at"] or latest["started_at"])
        if age >= OTHER_INSTANCE_STALE_SECONDS:
            return idle
        return {
            "session_id": latest["id"],
            "running": True,
            "status": f"recording (session {latest['id']}, other instance)",
            "laps_recorded": latest["lap_count"],
            "by_other": True,
        }

    def active_session_id(self) -> int | None:
        """Which session inbound driver data should be filed against.

        None means "nothing is recording" -- and the caller must then refuse
        the data rather than invent somewhere to put it. Storing a note
        against a stale session is harder to notice than storing none.
        """
        return self._resolve()["session_id"]

    def status_snapshot(self) -> dict:
        """What to show the driver, across however many instances exist.

        The overlay saying "connected, not recording" while laps are being
        stored by another instance is the single most misleading thing this
        tool has done, so a session found in the shared database counts.
        """
        r = self._resolve()
        # Never let another instance's recording mask this one being broken.
        if r["by_other"] and self._collector.last_error:
            r = dict(r, status=f"{r['status']}; this instance: "
                               f"{self._collector.status}")
        return r

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
                """Parsed JSON object body, or None if malformed/oversized.

                A non-object body (list, number, bare string) is treated as
                malformed too: every endpoint here reads named fields.

                The length is checked before reading, not after parsing: the
                per-endpoint batch cap only limits what reaches SQLite, so
                without this a client could still make us buffer and parse
                an arbitrarily large body first.
                """
                try:
                    n = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    return None
                if n < 0:
                    return None
                if n > MAX_BODY_BYTES:
                    # Drain before refusing. Writing the response while the
                    # client is still sending fills the socket buffer and
                    # the client dies with a broken pipe instead of reading
                    # the 400 that would have told it what went wrong.
                    self._drain(n)
                    return None
                try:
                    parsed = json.loads(self.rfile.read(n)) if n else {}
                except ValueError:  # covers json.JSONDecodeError
                    return None
                return parsed if isinstance(parsed, dict) else None

            def _drain(self, n: int, chunk: int = 65536):
                remaining = min(n, DRAIN_LIMIT_BYTES)
                while remaining > 0:
                    got = self.rfile.read(min(chunk, remaining))
                    if not got:
                        break
                    remaining -= len(got)
                self.close_connection = True

            def do_GET(self):
                if self.path != "/status":
                    return self._send(404, {"error": "unknown path"})
                self._send(200, {**bridge.status_snapshot(),
                                 "message": bridge.get_message()})

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
                    sid = bridge.active_session_id()
                    conn = db.connect(bridge._db_path)
                    try:
                        note_id = db.add_note(
                            conn, sid, lap_count, spline, tag, speed)
                    finally:
                        conn.close()
                    # sid is None when nothing is recording. Store the note
                    # anyway, against a NULL session -- the driver pressed
                    # the button and that input is real. What must not happen
                    # is guessing a session for it: attaching it to whatever
                    # ran most recently is silent misattribution, and far
                    # harder to notice than an orphan. get_driver_notes
                    # surfaces these; the app toasts them differently.
                    return self._send(200, {"ok": True, "id": note_id,
                                            "session_id": sid,
                                            "orphaned": sid is None})

                if self.path == "/rivals":
                    # Batched opponent telemetry from the Lua app. Rejecting
                    # the whole batch for one bad car would lose the other 19,
                    # so malformed entries are counted and skipped instead.
                    sid = bridge.active_session_id()
                    if sid is None:
                        return self._send(200, {"ok": False,
                                                "reason": "not recording"})
                    raw_cars = body.get("cars")
                    if not isinstance(raw_cars, list):
                        return self._send(400, {"error": "'cars' must be a "
                                                "list"})
                    if len(raw_cars) > MAX_RIVAL_BATCH:
                        return self._send(400, {
                            "error": f"batch too large "
                                     f"(max {MAX_RIVAL_BATCH})"})

                    drivers, samples, skipped = [], [], 0
                    for car in raw_cars:
                        if not isinstance(car, dict):
                            skipped += 1
                            continue
                        try:
                            idx = _req_int(car, "car_index", 0, 255)
                            lap_count = _req_int(car, "lap_count", 0, 100_000)
                            spline = _req_float(car, "spline", -0.1, 1.1)
                            speed = _req_float(car, "speed_kmh",
                                               0.0, MAX_SPEED_KMH)
                        except FieldError:
                            skipped += 1
                            continue
                        # Spline can read slightly outside 0..1 at the
                        # start/finish line; clamp rather than discard.
                        spline = min(max(spline, 0.0), 1.0)
                        # Names are stored as-is only if they really are
                        # strings; str() on a dict would persist its repr.
                        drivers.append({
                            "car_index": idx,
                            "driver_name": _opt_str(car.get("driver_name")),
                            "car_model": _opt_str(car.get("car_model")),
                            "best_lap_ms": _opt_int(car.get("best_lap_ms"),
                                                    0, MAX_LAP_MS),
                            "last_lap_ms": _opt_int(car.get("last_lap_ms"),
                                                    0, MAX_LAP_MS),
                            "lap_count": lap_count,
                        })
                        samples.append({
                            "car_index": idx,
                            "lap_count": lap_count,
                            "spline": spline,
                            "speed_kmh": speed,
                            "gear": _opt_int(car.get("gear"), *GEAR_RANGE),
                            "gas": _opt_float(car.get("gas"), 0.0, 1.0),
                            "brake": _opt_float(car.get("brake"), 0.0, 1.0),
                        })

                    conn = db.connect(bridge._db_path)
                    try:
                        n = db.store_rival_batch(conn, sid, drivers, samples)
                    finally:
                        conn.close()
                    return self._send(200, {"ok": True, "stored": n,
                                            "skipped": skipped})

                if self.path == "/ack":
                    try:
                        msg_id = _req_int(body, "id", 1, 2**31 - 1)
                    except FieldError as e:
                        return self._send(400, {"error": str(e)})
                    # ok=False is not an error: it just means the driver
                    # dismissed a message Claude had already replaced.
                    return self._send(200, {"ok": bridge.ack_message(msg_id)})

                return self._send(404, {"error": "unknown path"})

        self._handler_cls = Handler
        if self._try_bind():
            return
        # Binding failed. Refusing to share the port is deliberate, but it
        # means a socket left in TIME_WAIT by the previous server process
        # blocks us too -- and that clears on its own within a few minutes.
        # Retry in the background so an ordinary restart self-heals instead
        # of leaving the in-game app dead until the user notices.
        threading.Thread(target=self._retry_bind, daemon=True).start()

    def _try_bind(self) -> bool:
        try:
            self._server = _ExclusiveHTTPServer(("127.0.0.1", self._port),
                                                self._handler_cls)
        except OSError as e:
            self.error = (
                f"bridge failed to bind port {self._port}: {e}"
                " -- retrying. If this persists for more than a few minutes"
                " another process is listening on it; a socket merely closing"
                " (TIME_WAIT) would have cleared by then.")
            return False
        self.error = None
        self._port = self._server.server_address[1]  # resolve port=0
        threading.Thread(target=self._server.serve_forever,
                         daemon=True).start()
        return True

    def _retry_bind(self):
        deadline = time.monotonic() + BIND_RETRY_SECONDS
        while not self._bind_stop.is_set():
            if self._bind_stop.wait(BIND_RETRY_INTERVAL):
                return
            if self._try_bind():
                return
            if time.monotonic() > deadline:
                self.error = (
                    f"bridge could not bind port {self._port} after"
                    f" {BIND_RETRY_SECONDS:.0f}s. Another process is holding"
                    f" it -- almost certainly an orphaned server from an"
                    f" earlier run. Find it with:  Get-NetTCPConnection"
                    f" -LocalPort {self._port} -State Listen")
                return

    @property
    def port(self) -> int:
        return self._port

    def stop(self):
        self._bind_stop.set()
        with self._lock:
            server, self._server = self._server, None
        if server:
            server.shutdown()      # stop the accept loop
            # And actually close the listening socket. shutdown() alone
            # leaves it open until refcounting gets around to it, which on
            # Windows stretches the TIME_WAIT window that -- now that we
            # refuse to share the port -- our own restart has to wait out.
            server.server_close()
