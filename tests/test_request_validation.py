"""What the bridge accepts from the in-game app, and how it refuses.

The Lua client is the only thing that talks to this API, but it is a client
all the same: a bad field must cost that one car, never the batch, and never
the process. Rules of the house -- malformed input is a 400 with a readable
reason, an out-of-range optional field is dropped rather than coerced into
something plausible, and nothing here ever produces a 500.
"""

import http.client
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (FakeCollector, get, make_session, post,  # noqa: E402
                     run_module, temp_db)

from assetto_mcp import bridge as B, db  # noqa: E402


def _recording_bridge(path, conn):
    sid = make_session(conn)
    br = B.Bridge(path, FakeCollector(session_id=sid, running=True), 0)
    br.start()
    time.sleep(0.2)
    assert br.error is None, br.error
    return br, sid


# --- field coercion -----------------------------------------------------


def test_optional_ints_are_bounded_and_never_truncated():
    # Truncating 3.7 to 3 turns malformed client data into a
    # plausible-but-wrong gear, which is worse than admitting it's unknown.
    assert B._opt_int(3.7, -10, 10) is None
    assert B._opt_int(3.0, -10, 10) == 3
    assert B._opt_int(3, -10, 10) == 3
    assert B._opt_int(True, -10, 10) is None
    # Unbounded, this would be an int SQLite cannot bind.
    assert B._opt_int(1e30, 0, B.MAX_LAP_MS) is None
    assert B._opt_float(2.0, 0.0, 1.0) is None
    assert B._opt_float(float("nan"), 0.0, 1.0) is None
    print("  optional numeric fields bounded and integral-only")


def test_absurd_optional_fields_cost_one_car_not_the_batch():
    """An OverflowError from SQLite mid-loop would escape the per-item skip
    and roll back the whole transaction, every sample with it.
    """
    with temp_db() as path:
        conn = db.connect(path)
        br, _ = _recording_bridge(path, conn)
        try:
            cars = [{"car_index": i, "lap_count": 1, "spline": i / 10,
                     "speed_kmh": 100} for i in range(5)]
            cars[2]["best_lap_ms"] = 1e30
            cars[3]["gear"] = 1e25
            cars[4]["last_lap_ms"] = "99999999999999999999999"

            code, body = post(br.port, "/rivals", {"cars": cars})
            assert code == 200, (code, body)
            assert body["stored"] == 5, body
            gears = [r[0] for r in conn.execute(
                "SELECT gear FROM rival_samples ORDER BY car_index")]
            assert gears[3] is None, gears
            print("  absurd fields dropped; all 5 samples survived")
        finally:
            br.stop()
            conn.close()


def test_driver_name_is_not_repr_of_a_non_string():
    with temp_db() as path:
        conn = db.connect(path)
        br, sid = _recording_bridge(path, conn)
        try:
            post(br.port, "/rivals", {"cars": [
                {"car_index": 1, "lap_count": 1, "spline": 0.5,
                 "speed_kmh": 100, "driver_name": {"a": 1}}]})
            name = db.list_rivals(conn, sid)[0]["driver_name"]
            assert name == "", repr(name)
            print("  non-string driver_name dropped, not stringified")
        finally:
            br.stop()
            conn.close()


# --- request shape ------------------------------------------------------


def test_non_object_bodies_are_rejected_not_crashed():
    with temp_db() as path:
        conn = db.connect(path)
        br, _ = _recording_bridge(path, conn)
        try:
            for raw in (b"[1,2]", b"5", b'"hi"', b"null", b"true",
                        b"{not json,,", b""):
                for endpoint in ("/note", "/ack", "/rivals"):
                    code, _ = post(br.port, endpoint, raw=raw)
                    assert code == 400, (endpoint, raw, code)
            print("  non-object and malformed bodies -> 400, never 500")
        finally:
            br.stop()
            conn.close()


def test_requests_a_web_page_could_send_are_refused():
    with temp_db() as path:
        conn = db.connect(path)
        br, _ = _recording_bridge(path, conn)
        try:
            body = b'{"tag": "understeer", "pos": 0.5}'
            cases = [
                ("POST", "/note", {"Content-Type": "text/plain"}),
                ("POST", "/note", {}),
                ("POST", "/note", {"Content-Type": "application/json",
                                   "Host": "evil.example:%d" % br.port}),
                ("GET", "/status", {"Host": "evil.example:%d" % br.port}),
            ]
            for method, endpoint, headers in cases:
                c = http.client.HTTPConnection("127.0.0.1", br.port, timeout=5)
                c.request(method, endpoint,
                          body=body if method == "POST" else None,
                          headers=headers)
                code = c.getresponse().status
                c.close()
                assert code == 403, (method, endpoint, headers, code)
            assert not conn.execute("SELECT 1 FROM notes").fetchall()
            assert "message" in get(br.port, "/status")
            print("  cross-site and rebound-host requests -> 403")
        finally:
            br.stop()
            conn.close()


def test_oversized_body_is_refused_with_an_answer():
    """The body is drained before the refusal, or the client gets a broken
    pipe, and a 400 the client never reads is indistinguishable from a crash.
    """
    with temp_db() as path:
        conn = db.connect(path)
        br, _ = _recording_bridge(path, conn)
        try:
            blob = b'{"cars":[' + b"0," * (B.MAX_BODY_BYTES // 2) + b"0]}"
            code, _ = post(br.port, "/rivals", raw=blob)
            assert code == 400, code
            print("  oversized body refused without being parsed")
        finally:
            br.stop()
            conn.close()


def test_batch_cap_exceeds_the_lua_clients_buffer():
    """Assert the relationship, not the number.

    A loose bracket on the constant would accept a cap sized for one grid
    snapshot, which rejects every POST from a grid of four or more cars.
    """
    assert B.MAX_RIVAL_BATCH >= B.RIVAL_BUFFER_MAX_CLIENT
    lua = (Path(__file__).resolve().parents[1]
           / "lua_app/assetto_mcp/assetto_mcp.lua").read_text(
               encoding="utf-8", errors="replace")
    for line in lua.splitlines():
        if "RIVAL_BUFFER_MAX" in line and "local" in line and "=" in line:
            client = int(line.split("=")[1].strip())
            assert client == B.RIVAL_BUFFER_MAX_CLIENT, (
                f"Lua RIVAL_BUFFER_MAX={client} but the bridge expects "
                f"{B.RIVAL_BUFFER_MAX_CLIENT}")
            assert B.MAX_RIVAL_BATCH >= client
            break
    else:
        raise AssertionError("RIVAL_BUFFER_MAX not found in the Lua app")
    print(f"  MAX_RIVAL_BATCH={B.MAX_RIVAL_BATCH} >= client buffer "
          f"{B.RIVAL_BUFFER_MAX_CLIENT}")


def test_note_fields_are_range_checked():
    with temp_db() as path:
        conn = db.connect(path)
        br, _ = _recording_bridge(path, conn)
        try:
            ok = {"tag": "understeer", "spline": 0.34, "lap_count": 3,
                  "speed_kmh": 120}
            assert post(br.port, "/note", ok)[0] == 200
            for bad in ({**ok, "spline": -1}, {**ok, "spline": 1.5},
                        {**ok, "spline": None}, {**ok, "lap_count": -1},
                        {**ok, "lap_count": 3.7}, {**ok, "speed_kmh": -5},
                        {**ok, "tag": "bogus"}, {k: v for k, v in ok.items()
                                                 if k != "spline"}):
                code, _ = post(br.port, "/note", bad)
                assert code == 400, (bad, code)
            print("  note fields validated; spline stays comparable")
        finally:
            br.stop()
            conn.close()


def test_ack_ids_are_validated():
    with temp_db() as path:
        conn = db.connect(path)
        br, _ = _recording_bridge(path, conn)
        try:
            mid = br.set_message("claude_v2 saved")
            for bad in ({"id": None}, {"id": "abc"}, {}, {"id": [1]},
                        {"id": 0}, {"id": -5}, {"id": 1.5}, {"id": True}):
                code, _ = post(br.port, "/ack", bad)
                assert code == 400, (bad, code)
            assert post(br.port, "/ack", {"id": mid})[1]["ok"] is True
            # Acking a message that was already replaced is not an error.
            assert post(br.port, "/ack", {"id": 9999})[1]["ok"] is False
            print("  ack ids validated; stale ack is a false, not a 400")
        finally:
            br.stop()
            conn.close()


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
