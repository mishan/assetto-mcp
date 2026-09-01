"""Which laps count toward the session best, and which are set aside.

An invalid lap is still stored and still readable -- validity only decides
whether it pollutes best-lap maths. Getting this wrong is expensive in both
directions: a 10:22 pit lap counted as your best makes every subsequent
comparison meaningless, and discarding a merely scrappy lap throws away the
evidence a setup change was meant to produce.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (complete_lap, enter_pits, leave_pits,  # noqa: E402
                     make_session, run_collector, run_module, temp_db,
                     tick, wait_for)

from assetto_mcp import analysis, db  # noqa: E402
from assetto_mcp.collector import _is_outlier  # noqa: E402


def test_gross_outliers_are_set_aside():
    # Reference is a 1:54.054 Mugello lap. The allowance is the larger of
    # 25s and 25% of the reference, so 28.5s here -- a cut at about 2:22.
    assert _is_outlier(115000, None) is False       # no reference yet
    assert _is_outlier(115000, 114054) is False     # normal lap
    assert _is_outlier(120000, 114054) is False     # untidy, +6s
    assert _is_outlier(129000, 114054) is False     # scrappy, +15s
    assert _is_outlier(274832, 114054) is True      # the 4:34 lap
    assert _is_outlier(622162, 114054) is True      # the 10:22 lap
    # +56s is not a scrappy lap, it is a stop or a tow. The old rule (1.5x
    # the reference) put the cut at +57s, so it caught essentially nothing
    # the pit-lane check hadn't already caught.
    assert _is_outlier(170000, 114054) is True
    print("  outlier thresholds behave")


def test_outlier_allowance_scales_with_lap_length():
    """A ratio is the wrong shape for a lap time; an allowance is not."""
    # Nordschleife tourist lap, 8:00. A 1.5x ratio allowed a four-minute
    # margin -- a car could stop for tea and still count.
    ring = 480_000
    assert _is_outlier(ring + 100_000, ring) is False   # +100s, still under
    assert _is_outlier(ring + 200_000, ring) is True    # +200s, a stop
    # Short kart-style lap, 45s: the floor keeps the margin humane.
    kart = 45_000
    assert _is_outlier(kart + 10_000, kart) is False
    assert _is_outlier(kart + 30_000, kart) is True
    print("  outlier rule scales with lap length")


def test_reference_lap_does_not_depend_on_lap_validity():
    """A session where every lap is dirty must still get a reference.

    Deriving it from valid laps only made the outlier rule a dependent of
    the dirty-lap rule: at a track with tight limits nothing is ever valid,
    the reference stays None, and outlier detection silently never runs.
    """
    assert analysis.outlier_reference([]) is None
    assert analysis.outlier_reference([0, None]) is None
    assert analysis.outlier_reference([120000, 114054, 600000]) == 114054
    print("  reference takes all laps, not just valid ones")


def test_a_lap_containing_a_pit_visit_is_kept_but_is_not_a_lap_time():
    """The pit visit is recorded as a fact, not folded into one verdict.

    A pit lap's time is wall-clock nonsense, so nothing may rank or average
    it -- but the driving either side of the stop is real telemetry and it
    is kept. This used to set `valid = 0`, one flag meaning "off track OR
    pitted OR grossly slow", and everything downstream threw all three away
    together without saying which it was.
    """
    with temp_db() as path:
        script = [
            lambda s, c: tick(s, c),
            # Lap 1: clean flying lap.
            lambda s, c: (tick(s, c), complete_lap(s, c, 114000)),
            lambda s, c: tick(s, c),
            # Lap 2: driver dives into the pits mid-lap.
            enter_pits,
            leave_pits,
            lambda s, c: (tick(s, c), complete_lap(s, c, 622162)),
            lambda s, c: tick(s, c),
            lambda s, c: (tick(s, c), complete_lap(s, c, 115000)),
        ]
        run_collector(script, path)

        conn = db.connect(path)
        try:
            laps = {l["lap_time_ms"]: l for l in db.list_laps(conn, limit=None)}
            print("  stored laps:", sorted(laps))
            assert 114000 in laps and 622162 in laps, sorted(laps)

            pit = dict(laps[622162])
            assert pit["pitted"] == 1, pit
            usable, why = db.lap_usability(pit)
            assert usable is False and "pit" in why, why
            assert db.get_samples(conn, pit["id"]), \
                "the telemetry either side of the stop is still real"
            # Not marked as having run wide, because it didn't. That is a
            # different question and now has its own field.
            assert pit["invalid"] == 0, pit
            assert db.lap_usability(dict(laps[114000]))[0] is True
            print("  pit lap kept and flagged, not confused with a cut")
        finally:
            conn.close()


def test_a_quick_pit_stop_is_caught_by_the_pit_flag_not_the_outlier_rule():
    """Isolated from the outlier rule, which used to mask it.

    The pit lap in the test above is also a 5.4x outlier, so either
    mechanism could be deleted and the assertion would still hold. A quick
    tyre change or a drive-through produces a lap only a few seconds off --
    which is the case the pit flag actually exists for.
    """
    ref = 114000
    quick_stop = ref + 20_000            # inside the outlier allowance
    assert _is_outlier(quick_stop, ref) is False
    with temp_db() as path:
        script = [
            lambda s, c: tick(s, c),
            lambda s, c: (tick(s, c), complete_lap(s, c, ref)),
            lambda s, c: tick(s, c),
            enter_pits,
            leave_pits,
            lambda s, c: (tick(s, c), complete_lap(s, c, quick_stop)),
        ]
        run_collector(script, path)
        conn = db.connect(path)
        try:
            laps = {l["lap_time_ms"]: dict(l)
                    for l in db.list_laps(conn, limit=None)}
            stop = laps[quick_stop]
            assert stop["pitted"] == 1, stop
            assert stop["outlier"] == 0, "not slow enough to be an outlier"
            assert db.lap_usability(stop)[0] is False
            print("  a quick stop is excluded by the pit flag alone")
        finally:
            conn.close()


def test_invalid_laps_are_excluded_from_best_but_still_listed():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        db.store_lap(conn, sid, 1, 114054, True, [])
        db.store_lap(conn, sid, 2, 622162, False, [])
        assert db.list_sessions(conn)[0]["best_ms"] == 114054
        assert len(db.list_laps(conn, sid)) == 2
        print("  invalid lap kept and readable, excluded from best_ms")
        conn.close()


def test_an_abandoned_lap_is_kept_marked_incomplete():
    """A crash teleports the car back without the lap counter advancing.

    Those samples used to be dropped, so the lap a driver most wants to see
    -- the one that ended in the barrier -- was the only one guaranteed not
    to be recorded. It is now stored with complete=0 so it can never be
    mistaken for a real lap time.
    """
    with temp_db() as path:

        def drive_then_crash(sim, col):
            # tick() advances norm_pos by 0.1 a step, so start low enough
            # that four of them do not wrap past the line on their own.
            sim.graphics.normalizedCarPosition = 0.30
            tick(sim, col, n=4)                          # now ~0.70
            sim.graphics.normalizedCarPosition = 0.05    # teleported to pits
            wait_for(lambda: col.abandoned_laps >= 1,
                     "the abandoned lap to be stored")

        col = run_collector([
            lambda s, c: (tick(s, c), complete_lap(s, c, 113000)),
            drive_then_crash,
        ], path)
        assert col.abandoned_laps == 1, col.abandoned_laps

        conn = db.connect(path)
        laps = sorted(db.list_laps(conn), key=lambda r: r["id"])
        done = [l for l in laps if l["complete"]]
        gone = [l for l in laps if not l["complete"]]
        assert len(gone) == 1, laps
        assert db.lap_usability(dict(gone[0]))[0] is False, \
            "an unfinished lap's elapsed time is not a lap time"
        assert db.get_samples(conn, gone[0]["id"]), \
            "the whole point is that its samples survive"
        assert any(l["lap_time_ms"] == 113000 for l in done)
        print(f"  kept the abandoned lap with "
              f"{len(db.get_samples(conn, gone[0]['id']))} samples")
        conn.close()


def test_crossing_the_line_is_not_mistaken_for_a_teleport():
    """norm_pos goes ~1.0 -> ~0.0 every lap; that must not look abandoned.

    The lap counter and the position wrap do not land on the same tick, so
    an early version produced a phantom 400ms 'abandoned lap' at the start
    of every single lap.
    """
    with temp_db() as path:

        def lap(sim, col):
            sim.graphics.normalizedCarPosition = 0.98
            tick(sim, col, n=3)
            complete_lap(sim, col, 113500)
            sim.graphics.normalizedCarPosition = 0.01
            tick(sim, col, n=3)

        col = run_collector([lap, lap, lap], path)
        assert col.abandoned_laps == 0, (
            f"{col.abandoned_laps} normal line crossing(s) recorded as "
            f"abandoned laps")
        print("  3 line crossings, 0 false abandonments")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
