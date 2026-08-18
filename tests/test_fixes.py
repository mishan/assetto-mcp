"""Regression tests for the bugs found during the Mugello session.

Runs anywhere -- the collector is driven by a fake SimInfo, so no Windows
and no Assetto Corsa required.
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ac_race_engineer import analysis, db  # noqa: E402
from ac_race_engineer.collector import Collector, _is_outlier  # noqa: E402

AC_LIVE = 2
AC_OFF = 0


# --- fake shared memory -------------------------------------------------


class _Phys:
    def __init__(self):
        self.packetId = 0
        self.gas = 1.0
        self.brake = 0.0
        self.steerAngle = 0.1
        self.speedKmh = 120.0
        self.gear = 4
        self.rpms = 9000
        self.accG = [0.5, 0.0, 0.2]
        self.wheelSlip = [0.4, 0.4, 0.3, 0.3]
        self.wheelsPressure = [26.0] * 4
        self.tyreCoreTemperature = [85.0] * 4
        self.rideHeight = [0.02, 0.024]
        self.numberOfTyresOut = 0
        self.airTemp = 25.0
        self.roadTemp = 36.0


class _Graph:
    def __init__(self):
        self.status = AC_LIVE
        self.completedLaps = 0
        self.iLastTime = 0
        self.normalizedCarPosition = 0.0
        self.isInPitLane = 0
        self.tyreCompound = "F200 (S)"


class _Static:
    carModel = "rss_formula_rss_4"
    track = "mugello"
    trackConfiguration = "mugello_osrw"


class FakeSim:
    def __init__(self):
        self.physics = _Phys()
        self.graphics = _Graph()
        self.static = _Static()

    def close(self):
        pass


def _run_collector(script, db_path):
    """Drive a Collector through `script`, a list of (fn(sim)) steps."""
    sim = FakeSim()
    col = Collector(db_path, lambda: sim)
    col.start()
    for step in script:
        step(sim)
        time.sleep(0.06)
    time.sleep(0.15)
    col.stop()
    return col


def _tick(sim, n=6):
    """Advance physics packets so samples accumulate."""
    for _ in range(n):
        sim.physics.packetId += 1
        sim.graphics.normalizedCarPosition = (
            sim.graphics.normalizedCarPosition + 0.1) % 1.0
        time.sleep(0.01)


def _complete_lap(sim, lap_time_ms):
    sim.graphics.completedLaps += 1
    sim.graphics.iLastTime = lap_time_ms


# --- tests --------------------------------------------------------------


def test_slip_spike_does_not_poison_balance():
    """A single 30007 slip sample must not decide the corner's balance."""
    good = {"slip_fl": 1.4, "slip_fr": 1.4, "slip_rl": 0.5, "slip_rr": 0.5}
    spike = {"slip_fl": 30007.881, "slip_fr": 1.4,
             "slip_rl": 0.5, "slip_rr": 0.5}

    assert analysis._sane_slip(1.4, 1.4) == 1.4
    assert analysis._sane_slip(30007.881, 1.4) is None
    assert analysis._sane_slip(float("inf"), 1.0) is None
    assert analysis._sane_slip(float("nan"), 1.0) is None

    # Build a synthetic corner: 15 clean samples, one spiked.
    samples = []
    for i in range(16):
        src = spike if i == 8 else good
        samples.append({
            "norm_pos": i / 16, "speed_kmh": 100.0, "gear": 3,
            "brake": 0.0, "gas": 1.0, "steer": 0.5, **src,
        })
    stats = analysis._corner_stats(samples, 0, 8, 15)

    assert stats["slip_samples_dropped"] == 1
    assert abs(stats["front_slip"] - 1.4) < 0.001, stats["front_slip"]
    assert abs(stats["slip_balance"] - 0.9) < 0.001, stats["slip_balance"]
    print("  slip spike dropped, balance", stats["slip_balance"])


def test_steer_field_renamed():
    samples = [{"norm_pos": i / 16, "speed_kmh": 100.0, "gear": 3,
                "brake": 0.0, "gas": 1.0, "steer": 0.5,
                "slip_fl": 1.0, "slip_fr": 1.0,
                "slip_rl": 0.5, "slip_rr": 0.5} for i in range(16)]
    stats = analysis._corner_stats(samples, 0, 8, 15)
    assert "peak_steer_deg" not in stats
    assert stats["peak_steer_norm"] == 0.5
    print("  peak_steer_norm =", stats["peak_steer_norm"])


def test_outlier_helper():
    assert _is_outlier(115000, None) is False       # no reference yet
    assert _is_outlier(115000, 114000) is False     # normal lap
    assert _is_outlier(274832, 114054) is True      # the 4:34 lap
    assert _is_outlier(622162, 114054) is True      # the 10:22 lap
    assert _is_outlier(170000, 114054) is False     # scrappy but real
    print("  outlier thresholds behave")


def test_pit_lap_marked_invalid():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"

        def phase(fn):
            return fn

        script = [
            lambda s: _tick(s),
            # Lap 1: clean flying lap.
            lambda s: (_tick(s), _complete_lap(s, 114000)),
            lambda s: _tick(s),
            # Lap 2: driver dives into the pits mid-lap.
            lambda s: setattr(s.graphics, "isInPitLane", 1),
            lambda s: setattr(s.graphics, "isInPitLane", 0),
            lambda s: (_tick(s), _complete_lap(s, 622162)),
            lambda s: _tick(s),
            lambda s: (_tick(s), _complete_lap(s, 115000)),
        ]
        _run_collector(script, path)

        conn = db.connect(path)
        laps = sorted(db.list_laps(conn), key=lambda r: r["id"])
        times = [(l["lap_time_ms"], bool(l["valid"])) for l in laps]
        print("  stored laps:", times)
        assert (114000, True) in times, times
        pit_lap = [t for t in times if t[0] == 622162]
        assert pit_lap and pit_lap[0][1] is False, "pit lap should be invalid"
        conn.close()


def test_session_rolls_on_lap_counter_reset():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        script = [
            lambda s: _tick(s),
            lambda s: (_tick(s), _complete_lap(s, 114000)),
            lambda s: _tick(s),
            lambda s: (_tick(s), _complete_lap(s, 115000)),
            # In-game session restart: lap counter goes backwards while
            # status never leaves AC_LIVE.
            lambda s: (setattr(s.graphics, "completedLaps", 0),
                       setattr(s.graphics, "iLastTime", 0)),
            lambda s: _tick(s),
            lambda s: (_tick(s), _complete_lap(s, 113000)),
        ]
        _run_collector(script, path)

        conn = db.connect(path)
        sessions = db.list_sessions(conn)
        print("  sessions:", [(s["id"], s["lap_count"]) for s in sessions])
        assert len(sessions) >= 2, (
            f"expected a new session after restart, got {len(sessions)}")
        conn.close()


def test_set_session_setup():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = db.create_session(
            conn, car="rss_formula_rss_4", track="mugello",
            track_config="mugello_osrw", tyre_compound="F200 (S)",
            air_temp=25.0, road_temp=36.0)
        assert db.set_session_setup(conn, sid, "claude_v3") is True
        assert db.set_session_setup(conn, 9999, "nope") is False

        row = [s for s in db.list_sessions(conn) if s["id"] == sid][0]
        assert row["setup_name"] == "claude_v3"

        db.store_lap(conn, sid, 1, 114000, True, [])
        lap = db.list_laps(conn)[0]
        assert lap["setup_name"] == "claude_v3", lap
        print("  setup_name propagates to laps:", lap["setup_name"])
        conn.close()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        print(f"\n{name}")
        try:
            fn()
            print("  PASS")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL: {e}")
    print(f"\n{'all passed' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
