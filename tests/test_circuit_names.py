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


def _fake_install(tmp: Path, track="jr_road_atlanta_2022", layout="full",
                  ini=SECTIONS_INI):
    data = tmp / "content" / "tracks" / track / layout / "data"
    data.mkdir(parents=True)
    (data / "sections.ini").write_text(ini, encoding="utf-8")
    return tmp


class _AcRoot:
    """Point ASSETTO_MCP_AC_ROOT at a fake install for one test."""

    def __init__(self, ini=SECTIONS_INI):
        self.dir = Path(tempfile.mkdtemp(prefix="ac-root-"))
        _fake_install(self.dir, ini=ini)

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


def test_a_turn_is_named_where_its_apex_is_not_where_its_exit_runs():
    """Its exit runs well into the Esses; the corner is still Turn 3."""
    secs = [{"name": "Turn 3", "in_pos": 0.18, "out_pos": 0.20},
            {"name": "Esses", "in_pos": 0.20, "out_pos": 0.30}]
    turns = [{"turn": "T3", "entry_pos": 0.185, "apex_pos": 0.195,
              "exit_pos": 0.23}]
    assert circuit.name_turns(turns, secs)[0]["name"] == "Turn 3"


def test_a_flat_kink_does_not_take_the_next_corners_name():
    """Its exit touches Turn 4's IN; that does not make it Turn 4."""
    secs = [{"name": "Turn 4", "in_pos": 0.30, "out_pos": 0.33}]
    turns = [{"turn": "T4", "entry_pos": 0.27, "apex_pos": 0.285,
              "exit_pos": 0.301},
             {"turn": "T5", "entry_pos": 0.303, "apex_pos": 0.315,
              "exit_pos": 0.328}]
    named = {t["turn"]: t.get("name") for t in circuit.name_turns(turns, secs)}
    assert named == {"T4": None, "T5": "Turn 4"}, named


def test_order_in_a_section_across_the_line_runs_from_its_in():
    secs = [{"name": "Turn 12", "in_pos": 0.97, "out_pos": 0.03}]
    turns = [{"turn": "T1", "entry_pos": 0.005, "apex_pos": 0.01,
              "exit_pos": 0.02},
             {"turn": "T15", "entry_pos": 0.972, "apex_pos": 0.98,
              "exit_pos": 0.99}]
    named = {t["turn"]: t["name"] for t in circuit.name_turns(turns, secs)}
    assert named == {"T15": "Turn 12 (1st)", "T1": "Turn 12 (2nd)"}, named


def test_two_sections_with_one_name_number_their_turns_together():
    secs = [{"name": "Chicane", "in_pos": 0.20, "out_pos": 0.25},
            {"name": "Turn 5", "in_pos": 0.40, "out_pos": 0.45},
            {"name": "Chicane", "in_pos": 0.60, "out_pos": 0.65}]
    turns = [{"turn": "T2", "apex_pos": 0.22},
             {"turn": "T4", "apex_pos": 0.42},
             {"turn": "T6", "apex_pos": 0.62}]
    named, owner = circuit.claim(turns, secs)
    assert [t["name"] for t in named] == [
        "Chicane (1st)", "Turn 5", "Chicane (2nd)"], named
    assert owner == [0, 1, 2], owner
    assert all("name" not in t for t in turns), "the input is not changed"


def test_a_layout_without_its_own_file_does_not_borrow_the_roots():
    """The root data/ is another layout's spline; its names would be wrong."""
    with _AcRoot() as root:
        track = root / "content" / "tracks" / "jr_road_atlanta_2022"
        (track / "data").mkdir()
        (track / "data" / "sections.ini").write_text(SECTIONS_INI,
                                                     encoding="utf-8")
        (track / "short").mkdir()
        assert circuit.sections_for("jr_road_atlanta_2022", "short") == \
            ([], None)
        secs, source = circuit.sections_for("jr_road_atlanta_2022", "")
    assert len(secs) == 4 and source, (secs, source)


def _write(text: str, encoding="utf-8") -> Path:
    path = Path(tempfile.mkdtemp(prefix="ac-sections-")) / "sections.ini"
    path.write_text(text, encoding=encoding)
    return path


def _read(text: str, encoding="utf-8") -> list[str]:
    return [s["name"] for s in circuit.read_sections(_write(text, encoding))]


def test_a_byte_order_mark_does_not_cost_the_names():
    """Notepad saves "UTF-8" with one, and it sits before the first [."""
    names = _read(SECTIONS_INI, encoding="utf-8-sig")
    assert names == ["Turn 1", "The Esses", "Turn 10A", "Turn 10B"], names


def test_one_stray_line_does_not_cost_the_other_names():
    ini = SECTIONS_INI.replace("TEXT=Turn 10A\n", "TEXT=Turn 10A\noops\n")
    names = _read(ini)
    assert names == ["Turn 1", "The Esses", "Turn 10A", "Turn 10B"], names


def test_an_inline_comment_is_not_part_of_the_name_or_position():
    ini = SECTIONS_INI.replace("TEXT=Turn 1\n", "TEXT=Turn 1 ; La Source\n")
    ini = ini.replace("OUT=0.130\n", "OUT=0.130 # checked\n")
    secs = circuit.read_sections(_write(ini))
    assert secs[0] == {"name": "Turn 1", "in_pos": 0.075,
                       "out_pos": 0.130}, secs[0]


def test_steam_libraries_are_read_from_either_vdf_form():
    steam = Path(tempfile.mkdtemp(prefix="steam-"))
    (steam / "steamapps").mkdir()
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"LibraryFolders"\n{\n'
        '\t"TimeNextStatsReport"\t\t"1600000000"\n'
        '\t"ContentStatsID"\t\t"-123"\n'
        '\t"1"\t\t"D:\\\\SteamLibrary"\n'
        '\t"2"\t\t"/mnt/games/steam"\n'
        '\t"3"\n\t{\n\t\t"path"\t\t"E:\\\\Games"\n'
        '\t\t"apps"\n\t\t{\n\t\t\t"244210"\t\t"12345678"\n\t\t}\n\t}\n'
        '}\n', encoding="utf-8")
    real = circuit._steam_roots
    circuit._steam_roots = lambda: [steam]
    try:
        libs = [str(p) for p in circuit._steam_libraries()]
    finally:
        circuit._steam_roots = real
    assert libs == [str(steam), str(Path("E:\\Games")),
                    str(Path("D:\\SteamLibrary")),
                    str(Path("/mnt/games/steam"))], libs


def test_the_steam_search_runs_once_and_the_variable_always_wins():
    calls = []
    real = circuit._steam_libraries
    circuit._steam_libraries = lambda: calls.append(1) or []
    old = os.environ.pop("ASSETTO_MCP_AC_ROOT", None)
    circuit._steam_ac_root.cache_clear()
    try:
        assert circuit.ac_root() is None
        assert circuit.ac_root() is None
        assert len(calls) == 1, calls
        with _AcRoot() as root:
            assert circuit.ac_root() == root
    finally:
        circuit._steam_libraries = real
        circuit._steam_ac_root.cache_clear()
        if old is not None:
            os.environ["ASSETTO_MCP_AC_ROOT"] = old


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
    for bad in ("nan", "inf", "-inf", "3320,nan"):
        out = json.loads(srv.locate(meters=bad, session_id=sid))
        assert "not a position" in out.get("error", ""), (bad, out)
    assert "error" in json.loads(srv.locate(fractions="nan", session_id=sid))


def test_locate_refuses_a_number_with_thousands_separators():
    """"3,335" would otherwise come back as two places, 3 m and 335 m."""
    try:
        srv = _server()
    except ImportError as e:
        print(f"  skipped: {e}")
        return
    sid = _session(srv)
    srv._corner_maps[sid] = ((), {"turns": TURNS})
    with _AcRoot():
        for bad in ("3,335", "3,320, 3520", "1,234,567"):
            out = json.loads(srv.locate(meters=bad, session_id=sid))
            assert "thousands" in out.get("error", ""), (bad, out)
        out = json.loads(srv.locate(meters="3320,3335", session_id=sid))
        spaced = json.loads(srv.locate(meters="500, 750", session_id=sid))
    assert [p["at_m"] for p in out["places"]] == [3320, 3335], out
    # The way out the refusal offers has to work.
    assert [p["at_m"] for p in spaced["places"]] == [500, 750], spaced


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



def test_track_corners_lists_turns_by_section_not_by_name():
    """Two sections called "Chicane", and a "Turn 1" beside "Turn 1 (La
    Source)": matching by the name would put turns under the wrong one."""
    try:
        srv = _server()
    except ImportError as e:
        print(f"  skipped: {e}")
        return
    ini = """\
[SECTION_0]
IN=0.075
OUT=0.130
TEXT=Turn 1 (La Source)

[SECTION_1]
IN=0.200
OUT=0.230
TEXT=Turn 1

[SECTION_2]
IN=0.255
OUT=0.325
TEXT=Chicane

[SECTION_3]
IN=0.845
OUT=0.890
TEXT=Chicane
"""
    sid = _session(srv)
    cmap = {"turns": TURNS, "unnumbered": [], "note": "",
            "basis_lap_ids": [], "reference": {"reference": None}}
    srv._corner_maps[sid] = ((), cmap)
    real = srv.turns.basis_lap_ids
    srv.turns.basis_lap_ids = lambda *a, **k: []
    try:
        with _AcRoot(ini=ini):
            out = json.loads(srv.track_corners(session_id=sid))
    finally:
        srv.turns.basis_lap_ids = real
    by_section = [(s["name"], s["turns"])
                  for s in out["circuit_names"]["sections"]]
    assert by_section == [("Turn 1 (La Source)", ["T1"]), ("Turn 1", []),
                          ("Chicane", ["T5", "T6"]),
                          ("Chicane", ["T12", "T13"])], by_section
    names = [t.get("name") for t in out["turns"]]
    assert names == ["Turn 1 (La Source)", "Chicane (1st)", "Chicane (2nd)",
                     "Chicane (3rd)", "Chicane (4th)"], names
    assert all(not k.startswith("_") for t in out["turns"] for k in t), \
        out["turns"]


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
