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
                     make_session, run_collector, run_module, tick)

from ac_race_engineer import analysis, db  # noqa: E402
from ac_race_engineer.collector import _is_outlier  # noqa: E402


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


def test_lap_containing_a_pit_visit_is_invalid():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
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
        laps = sorted(db.list_laps(conn), key=lambda r: r["id"])
        times = [(l["lap_time_ms"], bool(l["valid"])) for l in laps]
        print("  stored laps:", times)
        assert (114000, True) in times, times
        pit_lap = [t for t in times if t[0] == 622162]
        assert pit_lap and pit_lap[0][1] is False, "pit lap should be invalid"
        conn.close()


def test_pit_stop_alone_invalidates_a_lap():
    """Isolated from the outlier rule, which used to mask it.

    The pit lap in the test above is also a 5.4x outlier, so either
    mechanism could be deleted and the assertion would still hold. A quick
    tyre change or a drive-through produces a lap that is only a few seconds
    off -- which is the case the pit rule actually exists for.
    """
    ref = 114000
    quick_stop = ref + 20_000            # inside the outlier allowance
    assert _is_outlier(quick_stop, ref) is False
    # So if this lap is invalid, it can only be because of the pit visit.
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
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
        by_time = {l["lap_time_ms"]: bool(l["valid"])
                   for l in db.list_laps(conn)}
        assert by_time.get(quick_stop) is False, by_time
        print("  a quick pit stop invalidates its lap on its own:", by_time)
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


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
