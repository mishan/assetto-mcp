"""Track limits as evidence rather than as a verdict.

One boolean used to decide whether a lap counted, and it was wrong in both
directions. A clean 2:06.769 at Sebring was stored invalid because wide flat
kerbs put three wheels over a line the game did not care about; a scrappy
2:10 stayed valid because it was only 7% off the pace. Either way
`compare_runs` dropped the lap and never said so.

So a lap now stores what was measured -- how many wheels, how many times,
for how long -- and the verdict is a threshold applied to it. Which means
the threshold can change and be re-applied to laps already driven, and that
is what these tests are mostly about.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module  # noqa: E402

from assetto_mcp import db  # noqa: E402

# t_ms, then tyres_out. score_excursions takes just these two.
STEP_MS = 40  # 25Hz


def _pairs(wheels_out: list) -> list:
    return [(i * STEP_MS, n) for i, n in enumerate(wheels_out)]


def test_a_lap_that_never_left_the_track_is_clean():
    s = db.score_excursions(_pairs([0] * 100))
    assert s == {"max_tyres_out": 0, "excursions": 0, "off_track_ms": 0,
                 "invalid": False}, s


def test_three_wheels_on_a_kerb_is_not_a_cut():
    """Sebring lap 129: a 2:06.769 the game counted, stored invalid.

    The circuit is ringed with wide flat kerbs and painted apron that put
    three wheels outside the surface routinely. The old rule was "> 2".
    """
    s = db.score_excursions(_pairs([0] * 20 + [3] * 25 + [0] * 55))
    assert s["invalid"] is False, s
    # But it is not pretending nothing happened, either.
    assert s["max_tyres_out"] == 3, s
    assert s["excursions"] == 0, s
    print("  three wheels for a second: recorded, not counted as a cut")


def test_all_four_wheels_off_is_a_cut_and_says_how_long_for():
    s = db.score_excursions(_pairs([0] * 20 + [4] * 25 + [0] * 55))
    assert s["invalid"] is True, s
    assert s["max_tyres_out"] == 4 and s["excursions"] == 1, s
    # 25 off-track samples, measured from where the episode began to where
    # it ended -- not to its last off-track sample, which loses a tick.
    assert s["off_track_ms"] == 25 * STEP_MS, s


def test_a_single_glitched_tick_is_not_an_excursion():
    # 40ms of four-wheels-off is one sample. Counting it would make every
    # noisy tick a cut.
    s = db.score_excursions(_pairs([0] * 50 + [4] + [0] * 49))
    assert s["invalid"] is False, s
    assert s["max_tyres_out"] == 4, "still recorded, just not counted"


def test_the_minimum_duration_is_where_the_comment_says_it_is():
    """MIN_EXCURSION_MS is 120ms, which at 25Hz is three samples.

    It used to be four, because duration was measured to the last
    off-track sample rather than to where the episode ended -- so the
    constant and the behaviour disagreed by a tick, in the direction of
    missing real excursions.
    """
    assert db.score_excursions(_pairs([0] + [4] * 2 + [0]))["excursions"] == 0
    assert db.score_excursions(_pairs([0] + [4] * 3 + [0]))["excursions"] == 1
    assert db.MIN_EXCURSION_MS == 3 * STEP_MS


def test_separate_excursions_are_counted_separately():
    s = db.score_excursions(_pairs(
        [0] * 10 + [4] * 10 + [0] * 10 + [4] * 10 + [0] * 10))
    assert s["excursions"] == 2, s
    assert s["off_track_ms"] == 2 * 10 * STEP_MS, s


def test_an_excursion_still_running_at_the_flag_counts():
    # A lap that ends with the car still off track -- which is exactly what
    # a lap ending in the gravel looks like.
    s = db.score_excursions(_pairs([0] * 50 + [4] * 50))
    assert s["excursions"] == 1 and s["invalid"] is True, s


def test_samples_out_of_order_are_scored_correctly():
    # _store_lap scores whatever order the collector assembled, so this
    # cannot rely on the SQL ORDER BY that backfill_excursions uses.
    ordered = db.score_excursions([(0, 0), (40, 4), (80, 4), (120, 4),
                                   (160, 0)])
    shuffled = db.score_excursions([(80, 4), (0, 0), (160, 0), (40, 4),
                                    (120, 4)])
    assert ordered == shuffled, (ordered, shuffled)
    assert ordered["excursions"] == 1


def test_tyres_out_that_was_never_recorded_reads_as_unknown():
    # A pre-v1 sample row with NULL tyres_out must not report "clean".
    s = db.score_excursions([(0, None), (40, None), (80, None)])
    assert s["max_tyres_out"] is None, s
    assert s["excursions"] is None, s


def test_a_lap_with_no_samples_is_unknown_not_clean():
    # "Nobody looked" and "looked and saw nothing" are different answers,
    # and only one of them may report a lap as clean.
    s = db.score_excursions([])
    assert s["max_tyres_out"] is None, s
    assert s["excursions"] is None, s
    assert s["invalid"] is False, "unknown must not read as a cut either"


def test_duration_uses_the_samples_own_clock():
    # A thinned lap is not 25Hz any more, and an assumed rate would report
    # an eighth of the real time off track.
    thinned = [(i * 320, 4) for i in range(10)]
    s = db.score_excursions(thinned)
    assert s["off_track_ms"] == 9 * 320, s


# --- the stored side ---------------------------------------------------

_SAMPLE = (180.0, 1.0, 0.0, 0.0, 4, 9000, 0.0, 0.0,
           0.4, 0.4, 0.3, 0.3, 26.0, 26.0, 26.0, 26.0,
           85.0, 85.0, 85.0, 85.0, 0.02, 0.024)


def _lap_samples(wheels_out: list) -> list:
    return [(i * STEP_MS, i / len(wheels_out), *_SAMPLE, n)
            for i, n in enumerate(wheels_out)]


def test_the_evidence_is_stored_on_the_lap_not_recomputed_every_read():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            sid = make_session(conn)
            lap_id = db.store_lap(conn, sid, 1, 113000, True,
                                  _lap_samples([0] * 20 + [4] * 25 + [0] * 55))
            lap = db.get_lap(conn, lap_id)
            assert lap["invalid"] == 1, dict(lap)
            assert lap["max_tyres_out"] == 4, dict(lap)
            assert lap["excursions"] == 1, dict(lap)
            assert lap["off_track_ms"] == 25 * STEP_MS, dict(lap)
            assert lap["invalid_source"] == "inferred", dict(lap)
        finally:
            conn.close()


def test_a_lap_that_ran_wide_is_still_usable():
    """The whole point. It is a lap; the driving on it happened."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            sid = make_session(conn)
            lap_id = db.store_lap(conn, sid, 1, 113000, True,
                                  _lap_samples([4] * 100))
            lap = dict(db.get_lap(conn, lap_id))
            assert lap["invalid"] == 1
            assert db.lap_usability(lap) == (True, None), lap
        finally:
            conn.close()


def test_an_out_lap_is_stored_and_flagged_rather_than_dropped():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            sid = make_session(conn)
            lap_id = db.store_lap(conn, sid, 1, 0, True,
                                  _lap_samples([0] * 40), out_lap=True)
            lap = dict(db.get_lap(conn, lap_id))
            assert lap["out_lap"] == 1
            usable, why = db.lap_usability(lap)
            assert usable is False and "out-lap" in why, why
            assert len(db.get_samples(conn, lap_id)) == 40, \
                "the driving out of the pits is still telemetry"
        finally:
            conn.close()


def test_changing_the_threshold_re_scores_laps_already_driven():
    """The reason for storing evidence instead of a verdict.

    Under the old design this was impossible: the boolean was computed as
    the lap landed and the samples behind it were never read again, so a
    threshold that turned out to be wrong cost every lap driven under it.
    """
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            sid = make_session(conn)
            three = db.store_lap(conn, sid, 1, 113000, True,
                                 _lap_samples([0] * 20 + [3] * 25 + [0] * 55))
            assert db.get_lap(conn, three)["invalid"] == 0

            original = db.TRACK_LIMITS_WHEELS
            db.TRACK_LIMITS_WHEELS = 3          # the old, stricter rule
            try:
                changed = db.backfill_excursions(conn)
                assert changed >= 1, changed
                assert db.get_lap(conn, three)["invalid"] == 1, \
                    "re-scoring should now count three wheels as a cut"
            finally:
                db.TRACK_LIMITS_WHEELS = original

            db.backfill_excursions(conn)
            assert db.get_lap(conn, three)["invalid"] == 0, \
                "and putting the threshold back gives the lap back"
            print("  threshold changed and reverted; no lap re-driven")
        finally:
            conn.close()


def test_re_scoring_never_overrides_a_verdict_that_came_from_the_game():
    # invalid_source = 'game' is reserved for the game's own answer, via a
    # CSP physics worker. Inference must not overwrite a measurement.
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            sid = make_session(conn)
            lap_id = db.store_lap(conn, sid, 1, 113000, True,
                                  _lap_samples([0] * 100))
            conn.execute("UPDATE laps SET invalid = 1,"
                         " invalid_source = 'game' WHERE id = ?", (lap_id,))
            conn.commit()
            db.backfill_excursions(conn)
            lap = db.get_lap(conn, lap_id)
            assert lap["invalid"] == 1, "the game said so; inference disagreed"
            assert lap["invalid_source"] == "game"
        finally:
            conn.close()


def test_outliers_are_flagged_but_never_hidden():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            sid = make_session(conn)
            fast = db.store_lap(conn, sid, 1, 113000, True,
                                _lap_samples([0] * 40))
            slow = db.store_lap(conn, sid, 2, 190000, True,
                                _lap_samples([0] * 40))
            db.backfill_outliers(conn)
            assert db.get_lap(conn, slow)["outlier"] == 1
            assert db.get_lap(conn, fast)["outlier"] == 0
            # Flagged, and still a perfectly usable lap to look at.
            assert db.lap_usability(dict(db.get_lap(conn, slow)))[0] is True
        finally:
            conn.close()


def test_an_out_lap_does_not_become_the_sessions_best_lap():
    """`best_ms` read `valid`, which stopped meaning "counts for timing".

    Out-laps are stored with lap_time_ms = 0 and are not invalid, so MIN()
    over valid laps returned 0 and every session reported a best lap of
    0:00.000 -- on every session, since an out-lap is now recorded for all
    of them.
    """
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            sid = make_session(conn)
            clean = _lap_samples([0] * 40)
            db.store_lap(conn, sid, 1, 0, True, clean, out_lap=True)
            db.store_lap(conn, sid, 2, 114054, True, clean)
            db.store_lap(conn, sid, 3, 21000, True, clean, complete=False)
            db.store_lap(conn, sid, 4, 300000, True, clean, pitted=True)
            s = db.list_sessions(conn)[0]
            assert s["best_ms"] == 114054, dict(s)
            assert s["timed_laps"] == 1, dict(s)
            assert s["lap_count"] == 4, "every lap is still stored"
            print("  4 laps stored, 1 of them a lap time, best", s["best_ms"])
        finally:
            conn.close()


def test_a_lap_that_ran_wide_can_still_be_the_best_lap():
    # Running wide does not make a lap time fictional, and this is the
    # clearest statement that `invalid` no longer gates anything.
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        try:
            sid = make_session(conn)
            db.store_lap(conn, sid, 1, 114000, True, _lap_samples([0] * 40))
            db.store_lap(conn, sid, 2, 112000, True,
                         _lap_samples([0] * 10 + [4] * 10 + [0] * 20))
            assert db.list_sessions(conn)[0]["best_ms"] == 112000
        finally:
            conn.close()


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
