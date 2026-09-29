"""Corners called by the circuit's names, from the track's sections.ini.

At Road Atlanta the session's T12 was the circuit's Turn 10A, and the
driver said plainly that meters past the start/finish line meant nothing
to them either: "I need relative positions -- like how far from the
corner?" These pin how a track's sections.ini is found and read, how a
detected turn is matched to a named section, and the wording that comes
out.
"""

import atexit
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module  # noqa: E402

from assetto_mcp import circuit, places  # noqa: E402

LENGTH = 4000.0

# Road Atlanta's own file, as the track ships it (a subset).
SECTIONS_INI = """\
[SECTION_0]
IN=0.075
OUT=0.130
TEXT=Turn 1

[SECTION_4]
IN=0.255
OUT=0.325
TEXT=The Esses

[SECTION_10]
IN=0.845
OUT=0.863
TEXT=Turn 10A

[SECTION_11]
IN=0.869
OUT=0.890
TEXT=Turn 10B

[SECTION_BAD]
IN=abc
OUT=0.5
TEXT=Not a section
"""

# Detected turns on a 4 km lap, shaped like session 55's.
TURNS = [
    {"turn": "T1", "brake_point_pos": 0.076, "entry_pos": 0.082,
     "apex_pos": 0.108, "exit_pos": 0.156},
    {"turn": "T5", "entry_pos": 0.259, "apex_pos": 0.259, "exit_pos": 0.296},
    {"turn": "T6", "entry_pos": 0.300, "apex_pos": 0.301, "exit_pos": 0.312},
    # Median apex past 10A's OUT, because laps that went straight on there
    # pulled it along; its stretch of road is still 10A's.
    {"turn": "T12", "brake_point_pos": 0.821, "entry_pos": 0.851,
     "apex_pos": 0.8685, "exit_pos": 0.8698},
    {"turn": "T13", "entry_pos": 0.871, "apex_pos": 0.877, "exit_pos": 0.890},
]


def _fake_install(tmp: Path, track="jr_road_atlanta_2022", layout="full"):
    data = tmp / "content" / "tracks" / track / layout / "data"
    data.mkdir(parents=True)
    (data / "sections.ini").write_text(SECTIONS_INI, encoding="utf-8")
    return tmp


class _AcRoot:
    """Point ASSETTO_MCP_AC_ROOT at a fake install for one test."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ac-root-"))
        _fake_install(self.dir)

    def __enter__(self):
        self.old = os.environ.get("ASSETTO_MCP_AC_ROOT")
        os.environ["ASSETTO_MCP_AC_ROOT"] = str(self.dir)
        return self.dir

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("ASSETTO_MCP_AC_ROOT", None)
        else:
            os.environ["ASSETTO_MCP_AC_ROOT"] = self.old


def _sections():
    with _AcRoot():
        secs, source = circuit.sections_for("jr_road_atlanta_2022", "full")
    return secs, source


def test_sections_are_read_from_the_tracks_own_file():
    secs, source = _sections()
    assert source and source.endswith("sections.ini"), source
    assert [s["name"] for s in secs] == [
        "Turn 1", "The Esses", "Turn 10A", "Turn 10B"], secs
    assert secs[2] == {"name": "Turn 10A", "in_pos": 0.845,
                       "out_pos": 0.863}, secs[2]
    print(f"  {len(secs)} sections, the malformed one skipped")


def test_a_track_without_the_file_has_no_names():
    with _AcRoot():
        assert circuit.sections_for("no_such_track", "") == ([], None)
    named = circuit.name_turns(TURNS, [])
    assert all("name" not in t for t in named), named


def test_a_turn_is_named_by_the_section_its_road_lies_in():
    named = {t["turn"]: t.get("name")
             for t in circuit.name_turns(TURNS, _sections()[0])}
    assert named["T1"] == "Turn 1", named
    # Its apex is nearer 10B; its turn-in-to-exit is 10A's.
    assert named["T12"] == "Turn 10A", named
    assert named["T13"] == "Turn 10B", named
    print(f"  {named}")


def test_two_turns_in_one_section_are_told_apart_by_order():
    named = {t["turn"]: t.get("name")
             for t in circuit.name_turns(TURNS, _sections()[0])}
    assert named["T5"] == "The Esses (1st)", named
    assert named["T6"] == "The Esses (2nd)", named


def test_an_apex_only_turn_takes_the_name_of_the_section_it_is_in():
    """compare_laps and compare_runs carry apexes and nothing else."""
    secs = _sections()[0]
    turns = [{"turn": "T4", "apex_pos": 0.87}, {"turn": "T9", "apex_pos": 0.6}]
    named = {t["turn"]: t.get("name") for t in circuit.name_turns(turns, secs)}
    assert named == {"T4": "Turn 10B", "T9": None}, named


def test_a_section_across_the_start_finish_line():
    secs = [{"name": "Turn 12", "in_pos": 0.95, "out_pos": 0.02}]
    turns = [{"turn": "T15", "entry_pos": 0.96, "apex_pos": 0.99,
              "exit_pos": 0.01}]
    assert circuit.name_turns(turns, secs)[0]["name"] == "Turn 12"


def test_where_speaks_in_circuit_names_relative_to_the_corner():
    named = circuit.name_turns(TURNS, _sections()[0])
    w = lambda pos: places.where(pos, LENGTH, named)  # noqa: E731
    assert w(0.83) == "Turn 10A braking zone, 84 m before turn-in", w(0.83)
    assert w(0.09) == "Turn 1, 32 m after turn-in", w(0.09)
    assert w(0.20) == "176 m after Turn 1 exit", w(0.20)
    assert w(0.24) == "76 m before The Esses (1st) turn-in", w(0.24)
    # Nothing in the wording says how far into the lap it is.
    assert "start/finish" not in w(0.5), w(0.5)
    print(f"  {w(0.83)!r}")


def test_place_puts_the_name_beside_the_session_number():
    named = circuit.name_turns(TURNS, _sections()[0])
    out = places.place({"corners": [{"turn": "T12", "apex_pos": 0.8685}]},
                       LENGTH, named)
    assert out["corners"][0]["name"] == "Turn 10A", out
    assert out["corners"][0]["turn"] == "T12", out


# --- the tools -------------------------------------------------------------

_SERVER = None


def _server():
    global _SERVER
    if _SERVER is None:
        d = tempfile.mkdtemp(prefix="ac-circuit-")
        os.environ.setdefault("ASSETTO_MCP_DATA", d)
        os.environ.setdefault("AC_DOCS_DIR", d)
        os.environ.setdefault("ASSETTO_MCP_BRIDGE_PORT", "0")
        os.environ.setdefault("ASSETTO_MCP_NO_AUTOSTART", "1")
        import importlib
        _SERVER = importlib.import_module("assetto_mcp.server")
        atexit.register(_SERVER._bridge.stop)
    return _SERVER


def _session(srv):
    from assetto_mcp import db
    sid = db.create_session(srv._conn, car="ks_mazda_mx5_cup",
                            track="jr_road_atlanta_2022",
                            track_config="full", tyre_compound="SM",
                            air_temp=24.0, road_temp=31.0)
    srv._conn.execute("UPDATE sessions SET track_length_m = ? WHERE id = ?",
                      (LENGTH, sid))
    srv._conn.commit()
    return sid


def test_locate_places_meters_and_fractions_by_the_nearest_corner():
    try:
        srv = _server()
    except ImportError as e:
        print(f"  skipped: {e}")
        return
    sid = _session(srv)
    srv._corner_maps[sid] = ((), {"turns": TURNS})
    with _AcRoot():
        out = json.loads(srv.locate(meters="3320", fractions="0.09",
                                    session_id=sid))
    by_m = [p["where"] for p in out["places"]]
    assert by_m == ["Turn 10A braking zone, 84 m before turn-in",
                    "Turn 1, 32 m after turn-in"], out
    assert out["places"][0]["at_m"] == 3320, out
    print(f"  {by_m}")


def test_locate_refuses_what_is_not_a_position():
    try:
        srv = _server()
    except ImportError as e:
        print(f"  skipped: {e}")
        return
    sid = _session(srv)
    assert "error" in json.loads(srv.locate(session_id=sid))
    assert "error" in json.loads(srv.locate(meters="far", session_id=sid))
    assert "error" in json.loads(srv.locate(fractions="1.5", session_id=sid))


def test_track_corners_lists_the_named_sections_and_their_turns():
    try:
        srv = _server()
    except ImportError as e:
        print(f"  skipped: {e}")
        return
    sid = _session(srv)
    cmap = {"turns": TURNS, "unnumbered": [], "note": "",
            "basis_lap_ids": [], "reference": {"reference": None}}
    srv._corner_maps[sid] = ((), cmap)
    real = srv.turns.basis_lap_ids
    srv.turns.basis_lap_ids = lambda *a, **k: []
    try:
        with _AcRoot():
            out = json.loads(srv.track_corners(session_id=sid))
    finally:
        srv.turns.basis_lap_ids = real
    by_name = {s["name"]: s["turns"] for s in out["circuit_names"]["sections"]}
    # "Turn 1" is a prefix of "Turn 10A"; T12 must not land under it.
    assert by_name == {"Turn 1": ["T1"], "The Esses": ["T5", "T6"],
                       "Turn 10A": ["T12"], "Turn 10B": ["T13"]}, by_name
    assert {t["turn"]: t.get("name") for t in out["turns"]}["T12"] == \
        "Turn 10A", out["turns"]


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
