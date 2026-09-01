"""What the setup SCREEN shows for a stored value.

The game reports a display_multiplier and it is not enough. A stored 20 can
read as 20 clicks, as 2.0 degrees, or as -2.0; the NSX stores 10 for 0.00
degrees of toe, so the zero is wrong as well as the scale; the F4 negates
the front axle. Five driver corrections in one evening, every one of them
this tool stating a display it had inferred and got wrong.

So the mapping is fitted from what the driver reads off the screen, and
where there is nothing to fit from, the answer is "unknown" rather than a
number. The five corrections are all here as cases -- they are the reason
the registry exists, and a regression in any of them is a regression in the
thing itself.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module  # noqa: E402

from assetto_mcp import db, setups  # noqa: E402


# --- the fit ------------------------------------------------------------

def test_two_readings_pin_a_negated_axis():
    """F4 front toe: the screen shows the negative of the stored value.

    No multiplier can express a sign flip, and the tool reported rear
    toe-out for a front toe-in setting until the driver corrected it.
    """
    m = setups.fit_display([(-20, 0.20), (20, -0.20)], multiplier=0.01)
    assert m["basis"] == "two observations", m
    assert setups.apply_display(-20, m) == 0.20
    assert setups.apply_display(0, m) == 0.0
    assert round(setups.apply_display(10, m), 6) == -0.10
    print("  screen = %g * stored + %g" % (m["slope"], m["offset"]))


def test_one_reading_fixes_a_zero_that_is_not_at_zero():
    """NSX toe: stored 10 IS 0.00 degrees. Scale right, offset wrong.

    One reading cannot separate a scale error from an offset error, so it
    borrows the game's multiplier as the slope and fits only the offset.
    That is exactly this case, and it costs the driver one number.
    """
    m = setups.fit_display([(10, 0.0)], multiplier=0.01)
    assert m["basis"] == "one observation, scale from the game", m
    assert setups.apply_display(10, m) == 0.0
    assert round(setups.apply_display(20, m), 6) == 0.10
    assert round(setups.apply_display(0, m), 6) == -0.10


def test_two_readings_pin_a_scale_the_game_understated():
    # F4 rod length: reported as roughly a millimetre a click, actually a
    # fraction of one.
    m = setups.fit_display([(0, 0.0), (100, 2.5)])
    assert m["slope"] == 0.025, m
    assert setups.apply_display(40, m) == 1.0


def test_one_reading_with_no_multiplier_cannot_be_fitted():
    # Nothing to borrow a slope from, and inventing one is the failure this
    # exists to stop.
    assert setups.fit_display([(10, 0.5)], multiplier=None) is None
    assert setups.fit_display([(10, 0.5)], multiplier=0) is None


def test_no_readings_fit_to_nothing():
    assert setups.fit_display([]) is None


def test_repeated_readings_at_one_position_are_still_one_point():
    # Two readings of the same spinner position do not define a line.
    m = setups.fit_display([(10, 0.0), (10, 0.0)], multiplier=0.01)
    assert m["points"] == 1, m
    assert m["basis"].startswith("one observation"), m


def test_the_widest_pair_anchors_the_fit():
    # Exactly linear inside the game, so extra points are confirmation
    # rather than signal, and the widest pair rounds least.
    m = setups.fit_display([(0, 0.0), (10, 0.25), (100, 2.5)])
    assert m["slope"] == 0.025 and m["points"] == 3, m
    assert "disagreement" not in m, "consistent points must not be flagged"


def test_a_reading_that_contradicts_the_others_is_named():
    """`points` was published as confirmation without anything checking it.

    Three observations, one of them wild, fitted cleanly from the outer two
    and reported "observations: 3" -- the contradiction silently discarded
    while inflating the apparent evidence.
    """
    m = setups.fit_display([(0, 0.0), (10, 99.0), (100, 2.5)])
    assert m["slope"] == 0.025, m
    d = m["disagreement"]
    assert d["stored"] == 10 and d["read"] == 99.0, d
    assert d["predicted"] == 0.25, d
    assert "misread" in d["note"], d


def test_two_readings_of_the_same_number_do_not_make_a_flat_line():
    """A screen that rounds, or the same number written down twice.

    A slope of 0 reported every value of the entry as one number with
    "observed" beside it -- the original failure with a badge on.
    """
    m = setups.fit_display([(-40, 0.5), (40, 0.5)], multiplier=0.01)
    assert m["slope"] == 0.01, "falls back to the game's scale, not zero"
    assert "why_partial" in m, m
    # And with no scale to fall back on, it refuses outright.
    assert setups.fit_display([(-40, 0.5), (40, 0.5)]) is None


def test_stored_values_that_differ_only_by_float_noise_are_one_reading():
    # Exact inequality gave a slope of 2e14 and a screen value of 1.5e15,
    # reported as observed. Continuous entries (step 0) are supported, so
    # this is reachable rather than theoretical.
    m = setups.fit_display([(3.4, 0.5), (3.4000000000000004, 0.6)],
                           multiplier=0.01)
    assert m["slope"] == 0.01, m
    assert "why_partial" in m, m


def test_a_value_outside_the_readings_is_flagged_as_extrapolated():
    conv = {"units": "deg", "display_multiplier": 0.01,
            "mapping": setups.fit_display([(0, 0.0), (10, 0.1)], 0.01)}
    inside = setups._displays_as(5, conv)
    assert "extrapolated" not in inside, inside
    outside = setups._displays_as(400, conv)
    assert "extrapolated" in outside, outside
    assert "0 to 10" in outside["extrapolated"], outside


def test_non_numeric_observations_are_skipped_rather_than_raising():
    # A NULL or a string in the table must not take the server down.
    m = setups.fit_display([(0, 0.0), (None, 1.0), ("x", 2.0), (100, 2.5)])
    assert m["slope"] == 0.025 and m["points"] == 2, m
    assert setups.fit_display([(float("nan"), 1.0)]) is None
    assert setups.fit_display([(0, float("inf")), (1, 1.0)],
                              multiplier=0.5)["slope"] == 0.5


# --- what gets reported -------------------------------------------------

def test_a_click_index_is_reported_as_unknown_not_guessed_at():
    """The exact spot where the guessing happened.

    It used to answer "20 (click index, mode 2)" -- an admission of
    ignorance shaped like an answer, which got repeated to the driver as
    one. There is no multiplier and no units for a click index; nothing
    here can turn it into what the screen shows.
    """
    got = setups._displays_as(20, {"units": "", "display_multiplier": None,
                                   "show_clicks_mode": 2})
    assert got["source"] == "unknown", got
    assert got["shown"] is None, "it must not state a displayed value"
    assert "record_display_value" in got["how_to_fix"], got


def test_the_games_multiplier_is_used_but_labelled_as_the_games():
    got = setups._displays_as(-32, {"units": "deg", "display_multiplier": 0.1,
                                    "show_clicks_mode": 0})
    assert got["source"] == "game" and got["value"] == -3.2, got
    assert "wrong before" in got["caveat"], got


def test_an_observation_beats_the_games_multiplier():
    conv = {"units": "deg", "display_multiplier": 0.01,
            "mapping": setups.fit_display([(-20, 0.20), (20, -0.20)], 0.01)}
    got = setups._displays_as(-20, conv)
    assert got["source"] == "observed", got
    assert got["value"] == 0.20, "the game's multiplier would say -0.2"


def test_a_note_rides_along_with_whatever_else_is_known():
    # Traction control: "1 = MOST intervention" is not a number mapping and
    # was re-derived wrongly more than once.
    conv = {"units": "", "display_multiplier": 1,
            "note": "1 = MOST intervention, 11 = least"}
    got = setups._displays_as(3, conv)
    assert got["note"].startswith("1 = MOST"), got
    assert got["source"] == "stored", got


def test_describe_display_says_unknown_when_it_knows_nothing():
    assert setups.describe_display(None)["source"] == "unknown"
    assert setups.describe_display({})["source"] == "unknown"


# --- stored and fitted through the database -----------------------------

def _car_with_spinners(conn, car="carx"):
    sid = make_session(conn, car=car, track="mugello")
    db.store_setup_snapshot(conn, sid, car, [
        {"name": "TOE_OUT_LF", "label": "Front toe", "min": -40, "max": 40,
         "step": 1, "value": -20, "display_multiplier": 0.01,
         "show_clicks_mode": 0, "units": "deg"},
        {"name": "ROD_LENGTH_LF", "label": "Ride height", "min": 0,
         "max": 36, "step": 1, "value": 20, "display_multiplier": None,
         "show_clicks_mode": 2, "units": ""},
    ])
    return sid


def test_an_observation_survives_and_is_fitted_on_read():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            _car_with_spinners(conn)
            db.record_display_observation(conn, "carx", "TOE_OUT_LF", -40, 0.4)
            db.record_display_observation(conn, "carx", "TOE_OUT_LF", 40, -0.4)
            conv = db.setup_display(conn, "carx")["TOE_OUT_LF"]
            assert conv["mapping"]["slope"] == -0.01, conv
            assert setups._displays_as(-20, conv)["value"] == 0.2
        finally:
            conn.close()


def test_re_reading_a_position_corrects_it_rather_than_adding_an_opinion():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            _car_with_spinners(conn)
            db.record_display_observation(conn, "carx", "TOE_OUT_LF", 40, -0.5)
            db.record_display_observation(conn, "carx", "TOE_OUT_LF", 40, -0.4)
            obs = db.display_observations(conn, "carx")["TOE_OUT_LF"]
            assert obs == [(40.0, -0.4)], obs
        finally:
            conn.close()


def test_a_misreading_can_be_taken_back():
    # A wrong observation is worse than none: the mapping fitted from it is
    # then stated with confidence.
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            _car_with_spinners(conn)
            db.record_display_observation(conn, "carx", "TOE_OUT_LF", 40, 99.0)
            assert "mapping" in db.setup_display(conn, "carx")["TOE_OUT_LF"]
            assert db.forget_display_observations(conn, "carx",
                                                  "TOE_OUT_LF") == 1
            conv = db.setup_display(conn, "carx")["TOE_OUT_LF"]
            assert "mapping" not in conv, conv
            assert setups.describe_display(conv)["source"] == "game"
        finally:
            conn.close()


def test_observations_for_an_entry_the_game_never_described_are_kept():
    # A reading is worth keeping whether or not the in-game app was running
    # when the spinner data would have arrived.
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            db.record_display_observation(conn, "carx", "MYSTERY", 0, 1.0)
            db.record_display_observation(conn, "carx", "MYSTERY", 10, 3.0)
            conv = db.setup_display(conn, "carx")["MYSTERY"]
            assert conv["mapping"]["slope"] == 0.2, conv
        finally:
            conn.close()


def test_notes_and_observations_are_independent():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            db.record_display_note(conn, "carx", "TRACTION_CONTROL",
                                   "1 = MOST intervention, 11 = least")
            conv = db.setup_display(conn, "carx")["TRACTION_CONTROL"]
            assert conv["note"].startswith("1 = MOST")
            assert "mapping" not in conv, "a note is not a measurement"
        finally:
            conn.close()


# --- through the tools --------------------------------------------------

_SERVER = None


def _server():
    global _SERVER
    if _SERVER is None:
        import atexit
        import importlib
        d = tempfile.mkdtemp(prefix="ac-display-")
        os.environ["ASSETTO_MCP_DATA"] = d
        os.environ["AC_DOCS_DIR"] = d
        os.environ["ASSETTO_MCP_BRIDGE_PORT"] = "0"
        os.environ["ASSETTO_MCP_NO_AUTOSTART"] = "1"
        _SERVER = importlib.import_module("assetto_mcp.server")
        atexit.register(_SERVER._bridge.stop)
    return _SERVER


def _call(fn, **kw):
    return json.loads(getattr(fn, "fn", fn)(**kw))


def test_the_range_shortcut_pins_a_mapping_in_one_exchange():
    """Both ends of the spinner, anchored to the stored min/max.

    The stored limits are already known from the game, so two numbers off
    the screen give scale, sign and zero at once.
    """
    srv = _server()
    _car_with_spinners(srv._conn, car="range_car")
    out = _call(srv.record_display_range, car="range_car",
                field="TOE_OUT_LF", displayed_at_min=0.4,
                displayed_at_max=-0.4)
    assert out["anchored_to"] == {"stored_min": -40.0, "stored_max": 40.0}
    d = out["display"]
    assert d["source"] == "observed" and d["slope"] == -0.01, d
    print(" ", d["formula"])


def test_the_live_value_supplies_the_stored_side():
    # One number from the driver, not two: they read the screen, the game
    # already said what is stored.
    srv = _server()
    sid = _car_with_spinners(srv._conn, car="live_car")
    out = _call(srv.record_display_value, car="live_car", session_id=sid,
                field="TOE_OUT_LF", displayed=-0.2)
    assert out["recorded"]["stored"] == -20.0, out
    # One spinner position: the zero is measured, the scale is still the
    # game's, and `source` says so rather than claiming a full fit.
    assert out["display"]["source"] == "observed_offset", out
    assert "different position" in out["next"], out


def test_a_field_with_no_live_value_says_so_instead_of_guessing_zero():
    srv = _server()
    _car_with_spinners(srv._conn, car="nolive_car")
    out = _call(srv.record_display_value, car="nolive_car",
                field="NOT_ON_THE_CAR", displayed=1.0)
    assert "error" in out and "cannot be inferred" in out["error"], out


def test_setup_ranges_names_the_entries_it_cannot_explain():
    srv = _server()
    _car_with_spinners(srv._conn, car="unknown_car")
    out = _call(srv.setup_ranges, car="unknown_car")
    assert out["display_unknown"] == ["ROD_LENGTH_LF"], out
    assert "record_display_value" in out["display_note"], out

    _call(srv.record_display_range, car="unknown_car", field="ROD_LENGTH_LF",
          displayed_at_min=0.0, displayed_at_max=0.9)
    after = _call(srv.setup_ranges, car="unknown_car")
    assert "display_unknown" not in after, after
    rod = [r for r in after["ranges"] if r["name"] == "ROD_LENGTH_LF"][0]
    assert rod["display"]["source"] == "observed", rod


def test_write_setup_reports_the_observed_display_and_flags_the_unknown():
    srv = _server()
    _car_with_spinners(srv._conn, car="write_car")
    _call(srv.record_display_range, car="write_car", field="TOE_OUT_LF",
          displayed_at_min=0.4, displayed_at_max=-0.4)

    out = _call(srv.write_setup, car="write_car", track="mugello",
                name="claude_v1",
                values_json=json.dumps({"TOE_OUT_LF": -20,
                                        "ROD_LENGTH_LF": 20}))
    shown = out["displays_as"]
    assert shown["TOE_OUT_LF"]["source"] == "observed", shown
    assert shown["TOE_OUT_LF"]["value"] == 0.2, shown
    assert shown["ROD_LENGTH_LF"]["source"] == "unknown", shown
    assert out["display_unknown"] == ["ROD_LENGTH_LF"], out
    assert "record_display_value" in out["display_note"], out
    # The stored values are untouched by any of this -- the file has to
    # hold what the game reads.
    assert out["written"]["TOE_OUT_LF"] == -20
    print("  toe reads", shown["TOE_OUT_LF"]["shown"], "on the screen")


def test_a_note_reaches_the_write_report():
    srv = _server()
    sid = _car_with_spinners(srv._conn, car="note_car")
    _call(srv.record_display_value, car="note_car", session_id=sid,
          field="TOE_OUT_LF", displayed=-0.2,
          note="front axle reads negated")
    out = _call(srv.write_setup, car="note_car", track="mugello", name="v1",
                values_json=json.dumps({"TOE_OUT_LF": -20}))
    assert out["displays_as"]["TOE_OUT_LF"]["note"] == \
        "front axle reads negated", out


def test_recording_against_a_field_with_no_range_is_refused_not_invented():
    srv = _server()
    _car_with_spinners(srv._conn, car="norange_car")
    out = _call(srv.record_display_range, car="norange_car",
                field="NOT_A_FIELD", displayed_at_min=0, displayed_at_max=1)
    assert "error" in out and "nothing to anchor" in out["error"], out


def test_one_reading_on_a_click_index_entry_admits_it_fitted_nothing():
    """The case the whole branch exists for, and it was a silent no-op.

    A click-index entry has no multiplier, so one reading has nothing to
    anchor against and `fit_display` returns None. The tool still answered
    `ok: true` with "the zero is fitted" -- nothing was -- while the entry
    stayed `unknown` and the advice was to do what had just been done.
    """
    srv = _server()
    sid = _car_with_spinners(srv._conn, car="clicks_car")
    out = _call(srv.record_display_value, car="clicks_car", session_id=sid,
                field="ROD_LENGTH_LF", stored=20, displayed=0.5)
    assert out["ok"] is False, out
    assert "not enough" in out["still_unknown"], out
    assert "record_display_range" in out["still_unknown"], out
    assert out["display"]["source"] == "unknown", out
    # The reading is kept, though -- a second one completes it.
    second = _call(srv.record_display_value, car="clicks_car", session_id=sid,
                   field="ROD_LENGTH_LF", stored=0, displayed=0.0)
    assert second["ok"] is True, second
    assert second["display"]["source"] == "observed", second
    print("  one reading refuses, two complete it")


def test_a_reading_is_never_filed_against_the_wrong_car():
    """`car` and the live `stored` value were resolved independently.

    Naming one car while a session for another is live took that session's
    spinner position and filed it under the named car -- a wrong reading,
    recorded confidently.
    """
    srv = _server()
    sid = _car_with_spinners(srv._conn, car="session_car")
    out = _call(srv.record_display_value, car="other_car", session_id=sid,
                field="TOE_OUT_LF", displayed=0.33)
    assert "error" in out, out
    assert "session_car" in out["error"], out
    assert db.display_observations(srv._conn, "other_car") == {}, \
        "nothing should have been recorded"


def test_a_note_can_be_left_without_inventing_a_reading():
    # The TC case: "1 is the most intervention" needs no number, and used to
    # require one to be made up to carry it.
    srv = _server()
    _car_with_spinners(srv._conn, car="note_only_car")
    out = _call(srv.record_display_note, car="note_only_car",
                field="TRACTION_CONTROL",
                note="1 = MOST intervention, 11 = least")
    assert out["ok"] is True and out["display"]["note"].startswith("1 = MOST")
    assert db.display_observations(srv._conn, "note_only_car") == {}, \
        "a note is not a measurement and must not become one"


def test_an_entry_known_only_from_a_note_is_not_reported_as_shown_as_stored():
    """NULL from the game means "nobody said", not "no conversion".

    An entry that exists only because someone left a note on it has no
    spinner data at all, and reporting `source: stored` for it is a
    confident answer built out of an absence.
    """
    srv = _server()
    _car_with_spinners(srv._conn, car="ghost_car")
    _call(srv.record_display_note, car="ghost_car", field="GHOST",
          note="only a note exists for this")
    conv = db.setup_display(srv._conn, "ghost_car")["GHOST"]
    assert conv["from_game"] is False, conv
    got = setups._displays_as(3, conv)
    assert got["source"] == "unknown", got
    assert got["shown"] is None, got


def test_setup_ranges_lists_entries_known_only_from_a_reading():
    # db.setup_display keeps them deliberately; listing only the spinner
    # rows threw them away again.
    srv = _server()
    _car_with_spinners(srv._conn, car="extra_car")
    _call(srv.record_display_note, car="extra_car", field="GHOST",
          note="recorded by hand")
    out = _call(srv.setup_ranges, car="extra_car")
    names = {r["name"] for r in out["ranges"]}
    assert "GHOST" in names, sorted(names)
    ghost = [r for r in out["ranges"] if r["name"] == "GHOST"][0]
    assert ghost["from_game"] is False, ghost
    assert ghost["display"]["note"] == "recorded by hand", ghost


def test_both_ends_reading_the_same_number_is_refused():
    srv = _server()
    _car_with_spinners(srv._conn, car="flat_car")
    out = _call(srv.record_display_range, car="flat_car", field="TOE_OUT_LF",
                displayed_at_min=0.5, displayed_at_max=0.5)
    assert "error" in out and "cannot define a scale" in out["error"], out
    assert "record_display_note" in out["what_to_do"], out
    assert db.display_observations(srv._conn, "flat_car") == {}, out


def test_forgetting_an_entry_clears_the_note_too():
    # "Forget that entry and start again" has to leave nothing behind.
    srv = _server()
    sid = _car_with_spinners(srv._conn, car="forget_car")
    _call(srv.record_display_value, car="forget_car", session_id=sid,
          field="TOE_OUT_LF", displayed=-0.2, note="reads negated")
    out = _call(srv.forget_display_value, car="forget_car",
                field="TOE_OUT_LF")
    assert out["observations_removed"] == 1 and out["note_removed"], out
    assert "note" not in out["display"], out

    _call(srv.record_display_value, car="forget_car", session_id=sid,
          field="TOE_OUT_LF", displayed=-0.2, note="reads negated")
    kept = _call(srv.forget_display_value, car="forget_car",
                 field="TOE_OUT_LF", keep_note=True)
    assert kept["note_removed"] is False, kept
    assert kept["display"]["note"] == "reads negated", kept


def test_recording_an_unknown_field_name_warns_rather_than_vanishing():
    # A typo is recorded just as willingly as a real entry, and then never
    # surfaces against the car's actual setup.
    srv = _server()
    _car_with_spinners(srv._conn, car="typo_car")
    out = _call(srv.record_display_value, car="typo_car",
                field="TOE_OUT_LFF", stored=10, displayed=0.1)
    assert "warning" in out and "never reported" in out["warning"], out


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
