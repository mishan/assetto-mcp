"""Rival laps timed off their own trace, and what each corner is worth.

At the Glen the server's lap time for a remote car lagged its lap counter,
so the time stored for a lap was often the lap before's: one rival read
1:45.97 on eleven laps running while his trace showed 1:44.5 to 1:45.3,
and the handoff ranked the driver ahead of two cars that were quicker.
These pin the trace timing, the ranking built on it, and the per-turn
time split that compare_to_rival and field_benchmark report.
"""

import atexit
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module  # noqa: E402

from assetto_mcp import analysis, db  # noqa: E402

LENGTH = 4000.0

# One turn on a 4 km lap: turn-in at 1600 m, apex 1800 m, exit 2000 m.
TURN = {"turn": "T1", "entry_pos": 0.40, "apex_pos": 0.45,
        "exit_pos": 0.50, "brake_point_pos": 0.38}


def _speed(pos, corner_kmh):
    """200 km/h everywhere but the turn, which is taken at corner_kmh."""
    return corner_kmh if 0.40 <= pos <= 0.50 else 200.0


def _drive(corner_kmh, brake_from, hz, laps=1, t0=0.0, start_pos=0.0):
    """Samples of a car lapping at constant speeds, in clock order.

    Each sample: t_ms, pos, speed, brake, lap (completed laps so far).
    """
    dt = 1.0 / hz
    t, pos, lap, out = t0, start_pos, 0, []
    while lap < laps:
        v = _speed(pos, corner_kmh)
        brake = 1.0 if brake_from <= pos < 0.40 else 0.0
        out.append((t * 1000.0, pos, v, brake, lap))
        t += dt
        pos += v / 3.6 * dt / LENGTH
        if pos >= 1.0:
            pos -= 1.0
            lap += 1
    return out


def _lap_seconds(corner_kmh):
    corner = 0.1 * LENGTH / (corner_kmh / 3.6)
    rest = 0.9 * LENGTH / (200 / 3.6)
    return corner + rest


# --- trace timing ------------------------------------------------------------

def test_lap_times_come_from_the_line_crossings_in_the_trace():
    trace = _drive(100.0, 0.38, hz=10, laps=4, start_pos=0.5)
    # The lap counter ticks one sample after the wrap, as it does live.
    rows = []
    for i, (t, pos, v, b, lap) in enumerate(trace):
        late = lap - 1 if i and trace[i - 1][4] != lap else lap
        rows.append({"lap_count": late, "spline": pos, "t_ms": t})
    times = analysis.trace_lap_times(rows)
    expect = _lap_seconds(100.0) * 1000
    assert sorted(times) == [1, 2, 3][:len(times)] and len(times) >= 2, times
    for ms in times.values():
        assert abs(ms - expect) < 100, (ms, expect)
    print(f"  {len(times)} laps timed at {expect / 1000:.3f} s")


def test_a_lap_the_feed_dropped_out_of_is_not_timed():
    trace = _drive(100.0, 0.38, hz=10, laps=3, start_pos=0.5)
    rows = [{"lap_count": lap, "spline": pos, "t_ms": t}
            for t, pos, v, b, lap in trace
            if not (lap == 1 and 0.2 < pos < 0.8)]      # half a lap missing
    times = analysis.trace_lap_times(rows)
    assert 1 not in times, times


def test_a_crossing_the_feed_dropped_does_not_make_a_double_lap():
    trace = _drive(100.0, 0.38, hz=10, laps=4, start_pos=0.5)
    # 1.2 s of nothing either side of the second wrap: too long a gap to
    # pin that crossing, so the crossings either side bracket two laps.
    wrap = next(t for i, (t, pos, v, b, lap) in enumerate(trace)
                if lap == 2 and trace[i - 1][4] == 1)
    rows = [{"lap_count": lap, "spline": pos, "t_ms": t}
            for t, pos, v, b, lap in trace if abs(t - wrap) > 600]
    times = analysis.trace_lap_times(rows)
    expect = _lap_seconds(100.0) * 1000
    for lap, ms in times.items():
        assert abs(ms - expect) < 100, (lap, ms, times)
    assert 1 not in times and 2 not in times, times


def _store_rival(conn, sid, car, corner_kmh, brake_from, laps, server_ms):
    trace = _drive(corner_kmh, brake_from, hz=10, laps=laps, start_pos=0.99)
    samples = [{"car_index": car, "lap_count": lap, "spline": pos,
                "speed_kmh": v, "gas": 0.0 if b else 1.0, "brake": b,
                "t_ms": int(t)} for t, pos, v, b, lap in trace]
    db.store_rival_batch(
        conn, sid,
        [{"car_index": car, "lap_count": laps, "driver_name": f"R{car}",
          "car_model": "gt3", "best_lap_ms": server_ms,
          "last_lap_ms": server_ms}], samples)


def test_rivals_are_ranked_by_their_traced_best_not_the_servers():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            _ranked(conn)
        finally:
            conn.close()


def _ranked(conn):
    sid = make_session(conn)
    # Car 5 is quicker, but the server says it is a second slower.
    _store_rival(conn, sid, 5, 120.0, 0.39, 3, server_ms=200_000)
    _store_rival(conn, sid, 6, 110.0, 0.39, 3, server_ms=100_000)
    rivals = db.list_rivals(conn, sid)
    assert [r["car_index"] for r in rivals] == [5, 6], rivals
    assert rivals[0]["server_best_lap_ms"] == 200_000, rivals[0]
    # Within 0.1 s: the simulated car changes speed only on a sample, so
    # its own lap is up to a sample off the arithmetic.
    assert abs(rivals[0]["best_lap_ms"]
               - _lap_seconds(120.0) * 1000) < 100, rivals[0]
    laps = db.well_covered_rival_laps(conn, sid, 5)
    assert laps[0]["lap_time_source"] == "trace", laps[0]


def test_a_new_driver_in_a_car_slot_does_not_inherit_the_old_ones_laps():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            sid = make_session(conn)
            # R5 laps quickly in slot 5 and leaves; a slower driver joins
            # in the same slot, their lap counter starting again from zero.
            _store_rival(conn, sid, 5, 140.0, 0.39, 4, server_ms=90_000)
            trace = _drive(100.0, 0.38, hz=10, laps=3, start_pos=0.985)
            db.store_rival_batch(
                conn, sid,
                [{"car_index": 5, "lap_count": 0, "driver_name": "Newcomer",
                  "car_model": "gt3", "best_lap_ms": None,
                  "last_lap_ms": None}],
                [{"car_index": 5, "lap_count": lap, "spline": pos,
                  "speed_kmh": v, "brake": b, "t_ms": int(t) + 10_000_000}
                 for t, pos, v, b, lap in trace])
            (r,) = db.list_rivals(conn, sid)
            assert r["driver_name"] == "Newcomer", r
            assert abs(r["best_lap_ms"]
                       - _lap_seconds(100.0) * 1000) < 100, r
            laps = db.well_covered_rival_laps(conn, sid, 5)
            assert max(l["lap_count"] for l in laps) <= 2, laps
            assert all(l["lap_time_ms"] > _lap_seconds(120.0) * 1000
                       for l in laps if l["lap_time_ms"]), laps
        finally:
            conn.close()


# --- time by turn ------------------------------------------------------------

def _as_samples(trace, pos_key):
    return [{"t_ms": t, pos_key: pos, "speed_kmh": v, "brake": b}
            for t, pos, v, b, lap in trace if lap == 0]


def test_turn_metrics_time_the_turn_and_the_road_after_it():
    mine = analysis.turn_metrics(
        _as_samples(_drive(100.0, 0.38, hz=25), "norm_pos"), "norm_pos",
        [TURN, dict(TURN, turn="T2", entry_pos=0.80, apex_pos=0.82,
                    exit_pos=0.84)])
    t1 = mine[0]
    # Within one 25 Hz sample: the speed steps at turn-in, between samples.
    assert abs(t1["time_s"] - 0.1 * LENGTH / (100 / 3.6)) < 0.06, t1
    assert abs(t1["after_s"] - 0.3 * LENGTH / (200 / 3.6)) < 0.06, t1
    assert t1["min_kmh"] == 100.0, t1
    assert abs(t1["brake_onset_pos"] - 0.38) < 0.002, t1


def test_time_by_turn_says_what_the_quicker_corner_was_worth():
    turns = [TURN]
    mine = analysis.turn_metrics(
        _as_samples(_drive(100.0, 0.38, hz=25), "norm_pos"), "norm_pos",
        turns)
    theirs = analysis.turn_metrics(
        _as_samples(_drive(125.0, 0.39, hz=10), "spline"), "spline", turns)
    rows = analysis.time_by_turn(mine, theirs, {"T1": "Turn 1"})
    turn = next(r for r in rows if r["stretch"] == "Turn 1")
    expect = 0.1 * LENGTH / (100 / 3.6) - 0.1 * LENGTH / (125 / 3.6)
    assert abs(turn["delta_s"] - expect) < 0.03, (turn, expect)
    print(f"  Turn 1 worth {turn['delta_s']:.3f} s to the rival")


# A second turn that runs across the line, taken flat.
OVER_THE_LINE = {"turn": "T2", "entry_pos": 0.95, "apex_pos": 0.99,
                 "exit_pos": 0.05}


def _counted(trace, tick):
    """One lap of a car's samples as the server groups them, by its counter.

    `tick` is when the counter changes against the wrap: "early" one
    sample before it, "late" one sample after it.
    """
    out = []
    for i, (t, pos, v, b, lap) in enumerate(trace):
        if tick == "early" and i + 1 < len(trace):
            lap = trace[i + 1][4]
        elif tick == "late" and i and trace[i - 1][4] != lap:
            lap -= 1
        if lap == 1:
            out.append({"t_ms": t, "spline": pos, "speed_kmh": v,
                        "brake": b})
    return out


def test_a_lap_whose_counter_ticked_early_is_still_timed_turn_by_turn():
    turns = [TURN, OVER_THE_LINE]
    for tick in ("early", "late"):
        lap = _counted(_drive(125.0, 0.39, hz=10, laps=3, start_pos=0.5),
                       tick)
        if tick == "early":
            # The case that lost the lap: it opens just short of the line.
            first = min(lap, key=lambda s: s["t_ms"])
            assert first["spline"] > 0.99, first
        got = analysis.turn_metrics(lap, "spline", turns)
        for m in got:
            assert m["time_s"] is not None and m["after_s"] is not None, \
                (tick, got)
        total = sum(m["time_s"] + m["after_s"] for m in got)
        assert abs(total - _lap_seconds(125.0)) < 0.1, (tick, total)


def test_time_by_turn_adds_up_to_the_lap_difference():
    turns = [TURN, OVER_THE_LINE]
    mine = analysis.turn_metrics(
        _as_samples(_drive(100.0, 0.38, hz=25), "norm_pos"), "norm_pos",
        turns)
    theirs = analysis.turn_metrics(
        _counted(_drive(125.0, 0.39, hz=10, laps=3, start_pos=0.5),
                 "early"), "spline", turns)
    rows = analysis.time_by_turn(mine, theirs, {})
    # Two turns and the road after each, the last across the line.
    assert len(rows) == 4, rows
    assert rows[-1]["stretch"] == "after T2", rows
    assert next(m for m in mine if m["turn"] == "T2")["min_kmh"] == 200.0
    lap_delta = _lap_seconds(100.0) - _lap_seconds(125.0)
    total = sum(r["delta_s"] for r in rows)
    assert abs(total - lap_delta) < 0.1, (total, lap_delta)
    print(f"  turns add to {total:.3f} s of a {lap_delta:.3f} s lap gap")


# --- the tool ----------------------------------------------------------------

_SERVER = None


def _server():
    global _SERVER
    if _SERVER is None:
        d = tempfile.mkdtemp(prefix="ac-rival-intel-")
        os.environ.setdefault("ASSETTO_MCP_DATA", d)
        os.environ.setdefault("AC_DOCS_DIR", d)
        os.environ.setdefault("ASSETTO_MCP_BRIDGE_PORT", "0")
        os.environ.setdefault("ASSETTO_MCP_NO_AUTOSTART", "1")
        import importlib
        _SERVER = importlib.import_module("assetto_mcp.server")
        atexit.register(_SERVER._bridge.stop)
    return _SERVER


# The columns after t_ms and norm_pos in a stored sample, less the three
# the test varies (speed, gas, brake).
_REST = (0.0, 4, 9000, 0.0, 0.0, 0.4, 0.4, 0.3, 0.3, 26.0, 26.0, 26.0, 26.0,
         85.0, 85.0, 85.0, 85.0, 0.02, 0.024, 0)


def _store_my_lap(conn, sid, number):
    trace = [x for x in _drive(100.0, 0.38, hz=25) if x[4] == 0]
    samples = [(int(t), pos, v, 0.0 if b else 1.0, b, *_REST)
               for t, pos, v, b, lap in trace]
    return db.store_lap(conn, sid, number, int(trace[-1][0]), True, samples)


def test_field_benchmark_ranks_the_corner_where_the_field_is_quicker():
    try:
        srv = _server()
    except ImportError as e:
        print(f"  skipped: {e}")
        return
    sid = make_session(srv._conn)
    srv._conn.execute("UPDATE sessions SET track_length_m = ? WHERE id = ?",
                      (LENGTH, sid))
    srv._conn.commit()
    for n in (2, 3):
        _store_my_lap(srv._conn, sid, n)
    _store_rival(srv._conn, sid, 5, 125.0, 0.39, 3, server_ms=999_999)
    ids = srv.turns.basis_lap_ids(srv._conn, sid)
    srv._corner_maps[sid] = (tuple(ids), {"turns": [TURN]})

    out = json.loads(srv.field_benchmark(session_id=sid))
    assert "error" not in out, out
    row = out["turns"][0]
    assert row["turn"] == "T1", row
    assert row["available_s"] > 0.2, row
    assert out["priorities"][0]["turn"] == "T1", out["priorities"]
    # The rival brakes 40 m later: 0.39 against 0.38 of a 4 km lap, to
    # within a sample (2 m at 25 Hz, 6 m at 10 Hz).
    assert abs(row["you"]["brake_before_turn_in_m"] - 80) <= 3, row["you"]
    assert abs(row["field"]["brake_before_turn_in_m"] - 40) <= 6, row["field"]
    assert out["field"][0]["car_index"] == 5, out["field"]
    print(f"  T1 available {row['available_s']} s; brake "
          f"{row['you']['brake_before_turn_in_m']} v "
          f"{row['field']['brake_before_turn_in_m']} m before turn-in")



def _bench_session(srv):
    sid = make_session(srv._conn)
    srv._conn.execute("UPDATE sessions SET track_length_m = ? WHERE id = ?",
                      (LENGTH, sid))
    srv._conn.commit()
    return sid


def _set_turns(srv, sid, turns, lap_id=None):
    """Stand in for the corner map of whatever laps the session now has."""
    ids = srv.turns.basis_lap_ids(srv._conn, sid, lap_id)
    srv._corner_maps[sid] = (tuple(ids), {"turns": list(turns)})


def test_a_stretch_only_one_side_timed_is_not_counted_as_time_available():
    try:
        srv = _server()
    except ImportError as e:
        print(f"  skipped: {e}")
        return
    sid = _bench_session(srv)
    # My laps stop short of the line, so the road after T1 -- which runs
    # across it -- has no time on my side; the field's has.
    ids = []
    for n in (2, 3):
        trace = [x for x in _drive(100.0, 0.38, hz=25)
                 if x[4] == 0 and x[1] < 0.9]
        samples = [(int(t), pos, v, 0.0 if b else 1.0, b, *_REST)
                   for t, pos, v, b, lap in trace]
        ids.append(db.store_lap(srv._conn, sid, n,
                                int(_lap_seconds(100.0) * 1000), True,
                                samples))
    _store_rival(srv._conn, sid, 5, 125.0, 0.39, 3, server_ms=999_999)
    _set_turns(srv, sid, [TURN])

    out = json.loads(srv.field_benchmark(
        session_id=sid, lap_ids=",".join(map(str, ids))))
    assert "error" not in out, out
    row = out["turns"][0]
    assert row["you"]["after_s"] is None, row
    assert row["field"]["after_s"] is not None, row
    expect = 0.1 * LENGTH / (100 / 3.6) - 0.1 * LENGTH / (125 / 3.6)
    assert abs(row["available_s"] - expect) < 0.1, (row, expect)


def test_field_benchmark_says_why_when_there_is_nothing_to_measure():
    try:
        srv = _server()
    except ImportError as e:
        print(f"  skipped: {e}")
        return
    # My laps carry no time: say so, not a ValueError.
    sid = _bench_session(srv)
    trace = [x for x in _drive(100.0, 0.38, hz=25) if x[4] == 0]
    samples = [(int(t), pos, v, 0.0 if b else 1.0, b, *_REST)
               for t, pos, v, b, lap in trace]
    lap = db.store_lap(srv._conn, sid, 2, 0, True, samples)
    _set_turns(srv, sid, [TURN])
    out = json.loads(srv.field_benchmark(session_id=sid, lap_ids=str(lap)))
    assert "lap time" in out.get("error", ""), out

    # Rivals recorded before samples carried a clock: no trace timing, which
    # is not the same as nobody being quicker.
    sid = _bench_session(srv)
    lap = _store_my_lap(srv._conn, sid, 2)
    trace = _drive(125.0, 0.39, hz=10, laps=3, start_pos=0.99)
    db.store_rival_batch(
        srv._conn, sid,
        [{"car_index": 5, "lap_count": 3, "driver_name": "R5",
          "car_model": "gt3", "best_lap_ms": 1, "last_lap_ms": 1}],
        [{"car_index": 5, "lap_count": l, "spline": pos, "speed_kmh": v,
          "brake": b} for t, pos, v, b, l in trace])
    _set_turns(srv, sid, [TURN])
    out = json.loads(srv.field_benchmark(session_id=sid, lap_ids=str(lap)))
    assert "trace" in out.get("error", ""), out
    assert "quicker" not in out["error"], out


def test_compare_to_rival_times_every_stretch_of_the_lap():
    try:
        srv = _server()
    except ImportError as e:
        print(f"  skipped: {e}")
        return
    sid = _bench_session(srv)
    lap = _store_my_lap(srv._conn, sid, 2)
    _store_rival(srv._conn, sid, 5, 125.0, 0.39, 3, server_ms=999_999)
    _set_turns(srv, sid, [TURN, OVER_THE_LINE], lap)
    out = json.loads(srv.compare_to_rival(car_index=5, lap_id=lap,
                                          rival_lap_count=1,
                                          session_id=sid))
    assert "error" not in out, out
    rows = out["time_by_turn"]
    assert len(rows) == 4, rows
    lap_delta = _lap_seconds(100.0) - _lap_seconds(125.0)
    total = sum(r["delta_s"] for r in rows)
    assert abs(total - lap_delta) < 0.15, (total, lap_delta, rows)


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
