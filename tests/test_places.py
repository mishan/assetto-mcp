"""Positions in meters and turns, not lap fractions.

A driver told "6.8 g at 0.065" had no idea where that was. Told "T1, 70 m
after turn-in", they did. These pin the wording a position is turned into,
the arithmetic across the start/finish line, and where the track length
comes from when the session never stored one.
"""

import atexit
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (FakeSim, complete_lap, make_session,  # noqa: E402
                     run_collector, run_module, temp_db, tick)

from assetto_mcp import db, places  # noqa: E402

LENGTH = 4000.0

# Every position below is a round number of meters on a 4 km lap.
TURNS = [
    {"turn": "T1", "brake_point_pos": 0.02, "entry_pos": 0.05,
     "apex_pos": 0.07, "exit_pos": 0.09},
    {"turn": "T2", "brake_point_pos": 0.45, "entry_pos": 0.48,
     "apex_pos": 0.50, "exit_pos": 0.53},
    # Across the line: turns in at 3880 m, exits at 60 m on the next lap.
    {"turn": "T3", "brake_point_pos": 0.95, "entry_pos": 0.97,
     "apex_pos": 0.99, "exit_pos": 0.015},
]


def where(pos, turns=TURNS):
    return places.where(pos, LENGTH, turns)


def test_a_place_inside_a_turn_counts_from_turn_in_then_the_apex():
    assert where(0.06) == "T1, 40 m after turn-in"
    assert where(0.07) == "T1 apex"
    assert where(0.072) == "T1 apex"            # 8 m: inside APEX_M
    assert where(0.085) == "T1, 60 m after the apex"
    print(f"  {where(0.06)!r}, {where(0.085)!r}")


def test_a_braking_zone_counts_down_to_turn_in_like_the_boards():
    assert where(0.46) == "T2 braking zone, 80 m before turn-in"
    print(f"  {where(0.46)!r}")


def test_a_straight_names_whichever_end_is_nearer():
    assert where(0.15) == "240 m after T1 exit"
    assert where(0.40) == "320 m before T2 turn-in"


def test_positions_either_side_of_the_line_are_one_turn():
    """T3 starts before the line and ends after it."""
    assert where(0.992) == "T3 apex"
    assert where(0.01) == "T3, 80 m after the apex"
    assert where(0.96) == "T3 braking zone, 40 m before turn-in"


def test_a_brake_point_after_turn_in_is_not_a_braking_zone_all_lap():
    """Interlagos T2, the Senna S: braked 6 m after turning in.

    Measured forward from the brake point, turn-in was 99.8% of a lap away,
    so every place on the circuit read as T2's braking zone.
    """
    turns = [{"turn": "T2", "brake_point_pos": 0.0824, "entry_pos": 0.0809,
              "apex_pos": 0.0876, "exit_pos": 0.1017}]
    assert "braking zone" not in where(0.5, turns), where(0.5, turns)
    placed = places.place({"turns": turns}, LENGTH, turns)
    assert placed["turns"][0]["brake_before_turn_in_m"] == -6, placed
    print(f"  {where(0.5, turns)!r}; brake_before_turn_in_m -6")


def test_apex_only_turns_are_placed_against_the_nearest_apex():
    """compare_runs and compare_laps carry turn and apex, nothing else."""
    turns = [{"turn": t["turn"], "apex_pos": t["apex_pos"]} for t in TURNS]
    assert where(0.06, turns) == "40 m before the T1 apex"
    assert where(0.15, turns) == "320 m after the T1 apex"
    assert where(0.50, turns) == "T2 apex"


def test_with_no_turns_it_is_meters_past_the_line():
    assert where(0.25, []) == "1000 m past the start/finish line"


def test_place_adds_meters_beside_every_fraction_and_keeps_the_fraction():
    payload = {
        "corners": [{"turn": "T1", "apex_pos": 0.07, "entry_pos": 0.05,
                     "brake_point_pos": 0.02, "brake_point_delta": -0.01,
                     "entry_phase": {"from": "brake point",
                                     "from_pos": 0.02}}],
        "contacts": [{"pos": 0.06, "g": 6.8}],
        "segments": [{"from": 0.1, "to": 0.2, "gain_ms": 12.0}],
        "laps": [{"from": 3, "to": 7}],
    }
    out = places.place(payload, LENGTH, TURNS)
    corner = out["corners"][0]
    assert corner["apex_pos"] == 0.07 and corner["apex_m"] == 280, corner
    assert corner["brake_point_m"] == 80, corner
    assert corner["brake_before_turn_in_m"] == 120, corner
    assert corner["brake_point_delta_m"] == -40, corner
    # A labelled turn already says where it is.
    assert "where" not in corner, corner
    assert corner["entry_phase"]["where"] == (
        "T1 braking zone, 120 m before turn-in"), corner

    contact = out["contacts"][0]
    assert contact["at_m"] == 240, contact
    assert contact["where"] == "T1, 40 m after turn-in", contact

    seg = out["segments"][0]
    assert (seg["from_m"], seg["to_m"]) == (400, 800), seg
    assert seg["where"] == ("from 40 m after T1 exit "
                            "to 440 m after T1 exit"), seg

    # Lap numbers under the same key names are not positions.
    assert out["laps"] == [{"from": 3, "to": 7}], out["laps"]
    print(f"  contact: {contact['where']!r}")


def test_place_with_no_length_changes_nothing():
    payload = {"contacts": [{"pos": 0.06}]}
    assert places.place(payload, None, TURNS) == payload


# --- where the length comes from ---------------------------------------

# One sample per 40 ms at a constant 180 km/h (50 m/s): 2 m per sample.
_SAMPLE = (180.0, 1.0, 0.0, 0.0, 4, 9000, 0.0, 0.0,
           0.4, 0.4, 0.3, 0.3, 26.0, 26.0, 26.0, 26.0,
           85.0, 85.0, 85.0, 85.0, 0.02, 0.024, 0)


def _store(conn, sid, lap_number, n=2001, **flags):
    return db.store_lap(conn, sid, lap_number, n * 40, True,
                        [(i * 40, i / n, *_SAMPLE) for i in range(n)],
                        **flags)


def test_a_stored_length_is_the_games():
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            db.set_fuel_basis(conn, sid, track_length_m=5245.0)
            assert places.session_length(conn, sid) == (5245.0, "game")
        finally:
            conn.close()


def test_a_missing_length_is_estimated_from_clean_laps_only():
    """Half the sessions recorded before the collector stored the length
    have none. Speed over the clock gives it back to within half a percent
    on real laps; an out-lap from the pit exit would not."""
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            assert places.session_length(conn, sid) == (None, None)
            _store(conn, sid, 1, n=900, out_lap=True)
            _store(conn, sid, 2)
            _store(conn, sid, 3)
            length, source = places.session_length(conn, sid)
            assert source == "estimated", source
            assert abs(length - 4000.0) < 1, length
            print(f"  estimated {length} m, out-lap ignored")
        finally:
            conn.close()


def test_the_collector_stores_the_games_track_length():
    with temp_db() as path:
        sim = FakeSim()
        sim.static.trackSPlineLength = 5245.0
        run_collector([lambda s, c: (tick(s, c),
                                     complete_lap(s, c, 114000))],
                      path, sim)
        conn = db.connect(path)
        try:
            session = db.get_session(conn, db.list_laps(conn)[0]["session_id"])
            assert session["track_length_m"] == 5245.0, session
        finally:
            conn.close()


# --- the tools -----------------------------------------------------------

_SERVER = None


def _server():
    global _SERVER
    if _SERVER is None:
        d = tempfile.mkdtemp(prefix="ac-places-")
        os.environ["ASSETTO_MCP_DATA"] = d
        os.environ["AC_DOCS_DIR"] = d
        os.environ["ASSETTO_MCP_BRIDGE_PORT"] = "0"
        os.environ["ASSETTO_MCP_NO_AUTOSTART"] = "1"
        import importlib
        _SERVER = importlib.import_module("assetto_mcp.server")
        atexit.register(_SERVER._bridge.stop)
    return _SERVER


def test_tool_payloads_say_where_the_length_came_from():
    try:
        srv = _server()
    except ImportError as e:
        print(f"  skipped: {e}")
        return
    sid = make_session(srv._conn)
    _store(srv._conn, sid, 1)
    _store(srv._conn, sid, 2)
    out = srv._placed({"contacts": [{"pos": 0.25}]}, sid, turns=[])
    assert out["track_length"] == {"m": 4000, "source": "estimated"}, out
    assert out["contacts"][0]["at_m"] == 1000, out
    # An error payload is passed through as it is.
    assert srv._placed({"error": "x"}, sid) == {"error": "x"}
    print(f"  {json.dumps(out['track_length'])}")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
