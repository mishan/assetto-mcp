"""Setup ranges and values taken from the game rather than reverse-engineered.

ac.getSetupSpinners() reports, for every adjustable entry, its legal
min/max/step and its current value, keyed by the same section names the
setup files use. That retires three things this project previously did by
hand: unpacking data.acd for ranges, inferring units by comparing a saved
file against the setup screen, and asking the driver which setup is loaded.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, post, run_module  # noqa: E402

from ac_race_engineer import db, setups  # noqa: E402
from ac_race_engineer.bridge import Bridge  # noqa: E402


class _Col:
    session_id = 1
    running = True
    status = "recording (session 1)"
    laps_recorded = 0


# The real shape, taken from the RSS Formula 4 at Mugello. CAMBER and
# ROD_LENGTH are the two that caused trouble: camber stores tenths of a
# degree, ride height stores a click index, and both were previously
# worked out by hand from a saved file.
SPINNERS = [
    {"name": "ARB_FRONT", "label": "Front ARB", "min": 52608, "max": 182107,
     "step": 9250, "value": 126607, "units": "N/m"},
    {"name": "ARB_REAR", "label": "Rear ARB", "min": 25459, "max": 88127,
     "step": 4476, "value": 47839, "units": "N/m"},
    {"name": "PRESSURE_LF", "label": "Cold Pressure LF", "min": 16,
     "max": 23, "step": 1, "value": 16, "units": "psi"},
    {"name": "CAMBER_LF", "label": "Camber LF", "min": -36, "max": -26,
     "step": 1, "value": -30, "displayMultiplier": 0.1, "units": "deg"},
    {"name": "ROD_LENGTH_LF", "label": "Height LF", "min": 0, "max": 36,
     "step": 1, "value": 14, "showClicksMode": 2, "units": "mm"},
    {"name": "WING_0", "label": "FW Angle", "min": -5, "max": 2, "step": 1,
     "value": -2, "units": "deg"},
]


def _bridge(path, session_id=1):
    b = Bridge(path, _Col(), port=0)
    _Col.session_id = session_id
    b.start()
    return b


def _write_setup_file(root, car, track, name, values):
    d = root / "setups" / car / track
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"[CAR]\nMODEL={car}\n"]
    for k, v in values.items():
        lines.append(f"[{k}]\nVALUE={v}\n")
    (d / f"{name}.ini").write_text("\n".join(lines), encoding="utf-8")


def test_spinners_arrive_and_split_into_ranges_and_values():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        sid = make_session(conn, car="rss_formula_rss_4")
        b = _bridge(path, sid)
        try:
            code, body = post(b.port, "/setup",
                              {"car": "rss_formula_rss_4",
                               "spinners": SPINNERS, "state": "legal"})
            assert code == 200 and body["ok"], body
            assert body["ranges"] == len(SPINNERS), body
            assert body["values"] == len(SPINNERS), body

            r = db.setup_ranges(conn, "rss_formula_rss_4")
            assert r["ARB_REAR"] == (25459, 88127, 4476), r["ARB_REAR"]
            # The two that needed hand-conversion before, straight from AC.
            assert r["CAMBER_LF"] == (-36, -26, 1), r["CAMBER_LF"]
            assert r["ROD_LENGTH_LF"] == (0, 36, 1), r["ROD_LENGTH_LF"]

            v = db.setup_values(conn, sid)
            assert v["ARB_REAR"] == 47839 and v["CAMBER_LF"] == -30, v
            assert db.setup_state(conn, sid)["state"] == "legal"
            print(f"  {body['ranges']} ranges, {body['values']} values stored")
        finally:
            b.stop()
            conn.close()


def test_display_conventions_are_recorded_not_inferred():
    """displayMultiplier and showClicksMode are the two units mysteries."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        sid = make_session(conn, car="rss_formula_rss_4")
        b = _bridge(path, sid)
        try:
            post(b.port, "/setup",
                 {"car": "rss_formula_rss_4", "spinners": SPINNERS})
            by_name = {r["name"]: r
                       for r in db.setup_range_details(conn,
                                                       "rss_formula_rss_4")}
            # Stored -30 shows as -3.0 degrees.
            assert by_name["CAMBER_LF"]["display_multiplier"] == 0.1
            assert by_name["ROD_LENGTH_LF"]["show_clicks_mode"] == 2
            # Absent must stay absent: it is not the same as 1, or as 0.
            assert by_name["ARB_FRONT"]["display_multiplier"] is None
            assert by_name["ARB_FRONT"]["show_clicks_mode"] is None
            print("  camber x0.1, rod length in clicks, ARB neither")
        finally:
            b.stop()
            conn.close()


def test_game_ranges_beat_a_ranges_file():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        ranges_dir = root / "ranges"
        ranges_dir.mkdir()
        # A stale file claiming a much narrower range.
        (ranges_dir / "rss_formula_rss_4.ini").write_text(
            "[ARB_REAR]\nMIN=1\nMAX=2\nSTEP=1\n", encoding="utf-8")

        game = {"ARB_REAR": (25459, 88127, 4476)}
        chosen, source = setups.resolve_ranges(ranges_dir,
                                               "rss_formula_rss_4", game)
        assert source == "game" and chosen["ARB_REAR"][1] == 88127, chosen

        # With nothing from the game, the file still works.
        chosen, source = setups.resolve_ranges(ranges_dir,
                                               "rss_formula_rss_4", None)
        assert source == "file" and chosen["ARB_REAR"][1] == 2, chosen
        print("  game ranges win; file remains the fallback")


def test_write_setup_clamps_against_game_ranges():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        out = setups.write_setup(
            root, root / "ranges", "rss_formula_rss_4", "mugello",
            "claude_test", {"ARB_REAR": 999999, "CAMBER_LF": -30},
            game_ranges={"ARB_REAR": (25459, 88127, 4476),
                         "CAMBER_LF": (-36, -26, 1)})
        assert out["ranges_source"] == "game", out
        assert out["written"]["ARB_REAR"] == 88123, out["written"]
        assert out["written"]["CAMBER_LF"] == -30, out["written"]
        print(f"  clamped to {out['written']['ARB_REAR']} from the game's max")


def test_identify_finds_the_one_matching_setup():
    """The case that motivated this: two setups differing only in ARB_REAR."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        base = {"ARB_FRONT": 126607, "PRESSURE_LF": 16, "CAMBER_LF": -30,
                "WING_0": -2}
        _write_setup_file(root, "rss_formula_rss_4", "mugello",
                          "claude_camber_v2", dict(base, ARB_REAR=38887))
        _write_setup_file(root, "rss_formula_rss_4", "mugello",
                          "claude_arb_v1", dict(base, ARB_REAR=47839))

        live = dict(base, ARB_REAR=47839)
        got = setups.identify_setup(root, "rss_formula_rss_4", "mugello", live)
        assert got["match"] == "claude_arb_v1", got
        # The other one should show up as a near miss naming the difference.
        assert any(n["name"] == "claude_camber_v2"
                   and "ARB_REAR" in n["differs_in"]
                   for n in got["near_misses"]), got["near_misses"]
        print(f"  identified {got['match']} across {got['compared']} values")


def test_identify_refuses_when_two_setups_are_identical():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        same = {"ARB_FRONT": 126607, "ARB_REAR": 38887}
        _write_setup_file(root, "car", "track", "one", same)
        _write_setup_file(root, "car", "track", "two", same)
        got = setups.identify_setup(root, "car", "track", dict(same))
        assert got["match"] is None, got
        assert sorted(got["candidates"]) == ["one", "two"], got
        assert "cannot be told apart" in got["reason"]
        print("  ambiguity reported rather than resolved by guessing")


def test_identify_says_so_when_the_app_is_not_running():
    with tempfile.TemporaryDirectory() as d:
        got = setups.identify_setup(Path(d), "car", "track", {})
        assert got["match"] is None
        assert "in-game app" in got["reason"], got["reason"]


def test_a_malformed_spinner_does_not_lose_the_others():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        sid = make_session(conn, car="c")
        b = _bridge(path, sid)
        try:
            code, body = post(b.port, "/setup", {
                "car": "c",
                "spinners": SPINNERS[:2] + [{"no_name": 1}, "not a dict"]})
            assert code == 200 and body["ranges"] == 2, body
            assert body["skipped"] == 2, body
        finally:
            b.stop()
            conn.close()


def test_a_setup_for_another_car_is_refused_not_filed():
    """The posted car is a persistence key, so it cannot be taken on trust.

    setup_ranges is keyed on the car alone, and those ranges then clamp
    every value written for it and decide which saved setup is identified as
    loaded. One client posting the wrong carId -- an app still holding the
    previous session's car -- would file one car's limits under another's
    name, where they would stay and quietly distort both. The session knows
    its car from shared memory; that is the answer that counts.
    """
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        sid = make_session(conn, car="rss_formula_rss_4")
        b = _bridge(path, sid)
        try:
            code, body = post(b.port, "/setup",
                              {"car": "ks_mazda_miata", "spinners": SPINNERS})
            assert code == 400, (code, body)
            assert "mismatch" in body["error"], body
            # Nothing was written under either name.
            assert db.setup_ranges(conn, "ks_mazda_miata") == {}
            assert db.setup_ranges(conn, "rss_formula_rss_4") == {}

            # The session's own car still goes through.
            code, body = post(b.port, "/setup",
                              {"car": "rss_formula_rss_4",
                               "spinners": SPINNERS})
            assert code == 200 and body["ok"], body
            assert body["car"] == "rss_formula_rss_4", body
            assert db.setup_ranges(conn, "rss_formula_rss_4"), "nothing stored"
            print("  wrong car refused, matching car stored")
        finally:
            b.stop()
            conn.close()


def test_a_session_with_no_car_still_accepts_the_clients():
    """Refusing on a blank session car would lose real data to a blank field.

    The session's car is authoritative when it is known. When it is not, the
    client's is the only value there is, and it is better than nothing.
    """
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        sid = make_session(conn, car="")
        b = _bridge(path, sid)
        try:
            code, body = post(b.port, "/setup",
                              {"car": "carx", "spinners": SPINNERS})
            assert code == 200 and body["ok"], body
            assert db.setup_ranges(conn, "carx"), "nothing stored"
            print("  unknown session car falls back to the client's")
        finally:
            b.stop()
            conn.close()


def test_setup_is_refused_when_nothing_is_recording():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        db.connect(path).close()
        col = _Col()
        b = Bridge(path, col, port=0)
        col.session_id = None
        b.start()
        try:
            code, body = post(b.port, "/setup",
                              {"car": "c", "spinners": SPINNERS})
            assert code == 200 and body["ok"] is False, body
        finally:
            b.stop()


def test_a_written_value_says_what_it_reads_as_on_the_setup_screen():
    """Stored and displayed are not the same number.

    Ride height is stored as a click index and camber in tenths of a degree.
    Asking for 20mm of rod length and writing 20 *clicks* is a setup that
    looks fine and isn't, reported as success -- which is exactly what
    show_clicks_mode exists to prevent. Nothing is converted (the file must
    hold the stored value), but the report has to say which is which.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        docs = root / "docs"
        docs.mkdir()
        rng = root / "ranges"
        rng.mkdir()
        report = setups.write_setup(
            docs, rng, car="carx", track="mugello", name="v1",
            values={"ROD_LENGTH_LF": 20, "CAMBER_LF": -32,
                    "PRESSURE_LF": 26},
            game_ranges={"ROD_LENGTH_LF": (0, 36, 1),
                         "CAMBER_LF": (-36, -26, 1),
                         "PRESSURE_LF": (15, 35, 1)},
            display={
                "ROD_LENGTH_LF": {"units": "", "display_multiplier": None,
                                  "show_clicks_mode": 2},
                "CAMBER_LF": {"units": "deg", "display_multiplier": 0.1,
                              "show_clicks_mode": 0},
                "PRESSURE_LF": {"units": "psi", "display_multiplier": 1,
                                "show_clicks_mode": 0},
            })

        shown = report["displays_as"]
        # The stored values are unchanged -- the file must hold these.
        assert report["written"]["ROD_LENGTH_LF"] == 20
        assert report["written"]["CAMBER_LF"] == -32
        # But the report says what they mean.
        assert "click" in shown["ROD_LENGTH_LF"], shown
        assert "-3.2 deg" in shown["CAMBER_LF"], shown
        assert "stored -32" in shown["CAMBER_LF"], shown
        assert shown["PRESSURE_LF"] == "26 psi", shown
        print("  ", shown)


def test_display_conventions_of_none_zero_and_one_all_mean_as_stored():
    """Absent, 0 and 1 are three ways of saying "no conversion".

    Coalescing 0 to a multiplier would scale every value to nothing.
    """
    for mult in (None, 0, 1):
        got = setups._displays_as(26, {"units": "psi",
                                       "display_multiplier": mult,
                                       "show_clicks_mode": 0})
        assert got == "26 psi", (mult, got)
        # No units either: the screen shows the bare stored number, which is
        # an answer. Returning None dropped the entry out of displays_as
        # entirely, so the one report that says what every written value
        # means was missing exactly the entries with nothing to explain.
        bare = setups._displays_as(26, {"units": "",
                                        "display_multiplier": mult,
                                        "show_clicks_mode": 0})
        assert bare == "26", (mult, bare)
    # None only when there is genuinely nothing known about the display.
    assert setups._displays_as(26, None) is None
    assert setups._displays_as(26, {}) is None
    print("  absent, 0 and 1 all read as stored, with or without units")


def test_identify_will_not_match_on_a_handful_of_shared_fields():
    """A file holding only [FUEL] agrees with every setup at that fuel load.

    Reporting that as an exact match is the same failure as the brake-bias
    fingerprint this replaced: a confident answer from fields that cannot
    separate the candidates.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _write_setup_file(root, "car", "track", "one_field", {"FUEL": 50})
        live = {f"ENTRY_{i}": i for i in range(20)}
        live["FUEL"] = 50
        got = setups.identify_setup(root, "car", "track", live)
        assert got["match"] is None, got
        assert got["too_few_fields_to_judge"][0]["name"] == "one_field"
        assert "not enough to identify" in got["reason"], got["reason"]
        print(" ", got["reason"][:60] + "...")


def test_a_car_with_few_adjustable_entries_can_still_be_identified():
    """The floor is a share of what exists, not a fixed count.

    Two of two adjustable entries is everything there is to know; two of
    twenty-one is a coincidence. A fixed count cannot tell those apart.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        vals = {"ARB_FRONT": 5, "ARB_REAR": 3}
        _write_setup_file(root, "car", "track", "small", dict(vals))
        got = setups.identify_setup(root, "car", "track", dict(vals))
        assert got["match"] == "small", got
        print(f"  identified on {got['compared']} of {len(vals)} entries")


def test_a_step_of_zero_is_not_rewritten_as_one():
    """0 is how a continuous entry reports itself.

    `step or 1` turned that into a grid of 1 and snapped every value onto
    it, which is a quiet way to write a setup nobody asked for.
    """
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        db.store_setup_snapshot(conn, sid, "carx", spinners=[
            {"name": "CONTINUOUS", "min": 0.0, "max": 10.0, "step": 0},
            {"name": "STEPPED", "min": 0.0, "max": 10.0, "step": 2},
            {"name": "UNSTATED", "min": 0.0, "max": 10.0},
        ])
        ranges = db.setup_ranges(conn, "carx")
        assert ranges["CONTINUOUS"][2] == 0, ranges["CONTINUOUS"]
        assert ranges["STEPPED"][2] == 2, ranges["STEPPED"]
        # Genuinely missing still defaults to 1.
        assert ranges["UNSTATED"][2] == 1, ranges["UNSTATED"]
        print("  step 0 preserved, missing step defaults to 1")
        conn.close()


def test_read_only_entries_are_not_offered_as_writable():
    """AC reports them so the screen can grey them out; writing one is
    silently ignored, so offering it produces a confident report of a value
    the car never sees."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        db.store_setup_snapshot(conn, sid, "carx", spinners=[
            {"name": "WRITABLE", "min": 0.0, "max": 10.0, "step": 1},
            {"name": "LOCKED", "min": 0.0, "max": 10.0, "step": 1,
             "read_only": True},
        ])
        ranges = db.setup_ranges(conn, "carx")
        assert "WRITABLE" in ranges
        assert "LOCKED" not in ranges, ranges
        print("  read-only entry withheld from the writable ranges")
        conn.close()


def test_identify_falls_back_from_a_layout_id_to_the_track_folder():
    """Sessions report track_config like 'mugello_osrw'; setups live under
    'mugello'. list_setups matches directories that START WITH what it is
    given, which is the wrong direction for a layout id -- so identifying a
    setup returned nothing at all while 30 live values sat there unused.

    Every AC id but the handful the original game shipped is
    `<vendor>_<track>[_<layout>]`, so taking the text before the first
    underscore resolved to "ks" -- a folder that exists nowhere. The one
    shape it did work for is the one circuit it was developed against.
    """
    vals = {"ARB_FRONT": 126607, "ARB_REAR": 47839}
    # folder on disk, what the session calls the layout, plain track name
    shapes = [
        ("mugello", "mugello_osrw", ""),
        ("ks_nordschleife", "ks_nordschleife_endurance", ""),
        ("ks_silverstone", "ks_silverstone_international", ""),
        ("ks_barcelona", "ks_barcelona_layout_moto", ""),
        # trackConfiguration is the bare layout at some circuits, where the
        # folder is the session's `track` instead and no amount of stripping
        # the layout id reaches it.
        ("ks_barcelona", "layout_moto", "ks_barcelona"),
        # And the plain folder name still works, with and without a layout
        # directory sitting next to it.
        ("mugello", "mugello", ""),
    ]
    for folder, layout, plain in shapes:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _write_setup_file(root, "rss_formula_rss_4", folder,
                              "claude_arb_v1", vals)
            got = setups.identify_setup(root, "rss_formula_rss_4", layout,
                                        dict(vals),
                                        track_folder=plain or None)
            assert got["match"] == "claude_arb_v1", (layout, got)
            # Which folder was actually read is part of the answer: a
            # fallback landing on another circuit's setups looks exactly
            # like one that found the right folder.
            assert got["track_dir"] == folder, (layout, got)
            if folder != layout:
                assert folder in got["track_dir_note"], got
            print(f"  {layout:28s} -> {got['track_dir']}")


def test_a_vendor_prefix_is_never_resolved_to_another_circuit():
    """Stripping suffixes ends at "ks", which must not match a ks_ folder.

    list_setups matches directories that start with what it is given, so a
    bare vendor tag would resolve to whichever Kunos track that car happens
    to have setups for -- and identification would report a confident match
    on another circuit's file. Shortened candidates are matched exactly, so
    "ks" only resolves if a folder is literally called that.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        vals = {"ARB_FRONT": 126607, "ARB_REAR": 47839}
        _write_setup_file(root, "car", "ks_nordschleife", "nords", vals)
        got = setups.identify_setup(root, "car", "ks_monza_junior", dict(vals))
        assert got["match"] is None, got
        assert got["candidates"] == [], got
        # And it says there was nothing to read, rather than reporting the
        # car's setups as not matching.
        assert "no setup folder" in got["reason"], got["reason"]
        print(" ", got["reason"])


def test_the_folder_read_is_the_folder_listed():
    """Listing one directory and reading from another finds nothing.

    The loose match resolves a plain track name onto a layout folder, and
    every read then went to the name it was asked for -- FileNotFoundError,
    skipped, "no saved setup matches the car" from a search that never
    opened a file.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        vals = {"ARB_FRONT": 126607, "ARB_REAR": 47839}
        _write_setup_file(root, "car", "spa_2020", "v1", vals)
        got = setups.identify_setup(root, "car", "spa", dict(vals))
        assert got["track_dir"] == "spa_2020", got
        assert got["match"] == "v1", got
        print("  'spa' listed and read 'spa_2020'")


def test_track_dir_names_only_a_folder_that_was_really_there():
    """track_dir used to echo back the id it was asked for on failure.

    Three states, and the first two were reported identically:

        no folder anywhere    -> track_dir 'ks_nowhere_special'
        folder exists, empty  -> track_dir 'mugello'
        folder with setups    -> track_dir 'mugello'

    The first is a wrong track id or an install that was never found; the
    second is a real folder with nothing saved in it yet. One is fixed by
    correcting the lookup, the other by saving a setup, and the field that
    is supposed to say which folder was read named a directory that does
    not exist on disk.
    """
    vals = {"ARB_FRONT": 126607, "ARB_REAR": 47839}

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # 1. Nothing at all: not even a car directory.
        got = setups.identify_setup(root, "car", "ks_nowhere_special",
                                    dict(vals))
        assert got["track_dir"] is None, got
        assert got["match"] is None, got
        assert "no setup folder" in got["reason"], got["reason"]
        # And nothing claims a folder was read in place of the one asked for.
        assert "track_dir_note" not in got, got
        print("  no folder        -> track_dir", got["track_dir"])

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # 2. The folder is there, and holds no setups.
        (root / "setups" / "car" / "mugello").mkdir(parents=True)
        got = setups.identify_setup(root, "car", "mugello_osrw", dict(vals),
                                    track_folder="mugello")
        assert got["track_dir"] == "mugello", got
        assert got["match"] is None, got
        assert "holds no setups" in got["reason"], got["reason"]
        # It was found, not read, so it must not be reported as read.
        assert "track_dir_note" not in got, got
        print("  empty folder     -> track_dir", got["track_dir"])

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # 3. The same folder with a setup in it: the answer is unchanged.
        _write_setup_file(root, "car", "mugello", "claude_arb_v1", vals)
        got = setups.identify_setup(root, "car", "mugello_osrw", dict(vals),
                                    track_folder="mugello")
        assert got["track_dir"] == "mugello", got
        assert got["match"] == "claude_arb_v1", got
        assert "mugello" in got["track_dir_note"], got
        print("  folder + setups  -> track_dir", got["track_dir"],
              "match", got["match"])


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
