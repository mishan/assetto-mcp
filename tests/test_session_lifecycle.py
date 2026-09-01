"""When a new session begins, and when the old one stops being current.

Sessions are the unit everything else is filed against, so a missed boundary
merges two runs at different track grip into one set of lap numbers, and a
boundary that lingers after recording stops leaves data attached to a session
that has ended.
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (FakeSim, complete_lap, go_live, go_off,  # noqa: E402
                     restart_from_menu, run_collector, run_module, tick,
                     wait_for)

from ac_race_engineer import db  # noqa: E402
from ac_race_engineer.collector import Collector  # noqa: E402


def test_restart_from_the_menu_starts_a_new_session():
    """Restarting in-game never passes through AC_OFF.

    Watching only for that transition left the new run appended to the old
    session: same session_id, lap numbers starting over, and two different
    track states averaged together.
    """
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        script = [
            lambda s, c: tick(s, c),
            lambda s, c: (tick(s, c), complete_lap(s, c, 114000)),
            lambda s, c: tick(s, c),
            lambda s, c: (tick(s, c), complete_lap(s, c, 115000)),
            restart_from_menu,
            lambda s, c: tick(s, c),
            lambda s, c: (tick(s, c), complete_lap(s, c, 113000)),
        ]
        run_collector(script, path)

        conn = db.connect(path)
        sessions = db.list_sessions(conn)
        print("  sessions:", [(s["id"], s["lap_count"]) for s in sessions])
        assert len(sessions) >= 2, (
            f"expected a new session after restart, got {len(sessions)}")
        conn.close()


def test_leaving_the_session_starts_a_new_one():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        script = [
            lambda s, c: tick(s, c),
            lambda s, c: (tick(s, c), complete_lap(s, c, 114000)),
            go_off,
            go_live,
            lambda s, c: tick(s, c),
            lambda s, c: (tick(s, c), complete_lap(s, c, 116000)),
        ]
        run_collector(script, path)
        conn = db.connect(path)
        assert len(db.list_sessions(conn)) >= 2
        print("  AC_OFF also rolls the session")
        conn.close()


def test_status_stops_claiming_to_record_once_ac_is_off():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        seen = {}
        script = [
            lambda s, c: tick(s, c),
            lambda s, c: seen.update(recording=c.status),
            go_off,
            lambda s, c: seen.update(idle=c.status),
        ]
        run_collector(script, path)
        assert "recording" in seen["recording"], seen
        assert "waiting" in seen["idle"], seen
        print(f"  {seen['recording']!r} -> {seen['idle']!r}")


def test_stopping_clears_the_current_session():
    """session_id must not outlive the recording it describes.

    The bridge asks the collector which session inbound driver data belongs
    to. A leftover id means notes and rival telemetry keep being filed
    against a session that ended -- which is harder to spot than filing them
    against nothing, because the data looks perfectly plausible.
    """
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        sim = FakeSim()
        col = Collector(path, lambda: sim)
        col.start()
        try:
            wait_for(lambda: col.session_id is not None, "a session to open")
            opened = col.session_id
        finally:
            col.stop()

        assert col.session_id is None, (
            f"session_id survived stop(): {col.session_id}")
        # Kept for reporting, where being stale is only cosmetic.
        assert col.last_session_id == opened
        assert not col.running, col.last_error
        print(f"  session {opened} cleared on stop, retained for reporting")


def test_a_recorded_lap_carries_a_driving_line():
    """The point of schema v8, asserted end to end.

    norm_pos has always said where the car is ALONG the lap. Nothing said
    where it was across it, which is the whole of what a line is -- so
    "I took a wider entry" was a claim the telemetry could not check.

    carCoordinates was in the shared-memory struct the whole time, read 25
    times a second and dropped on the floor. This test fails if it goes back
    on the floor, and it fails if the collector stores one position over and
    over, which a test against a stationary stub would not catch.
    """
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        script = [
            lambda s, c: tick(s, c),
            lambda s, c: (tick(s, c), complete_lap(s, c, 114000)),
        ]
        run_collector(script, path)

        conn = db.connect(path)
        lap = db.list_laps(conn)[0]
        rows = conn.execute(
            "SELECT pos_x, pos_y, pos_z, heading, pitch, roll,"
            " tc_active, abs_active FROM samples WHERE lap_id = ?"
            " ORDER BY t_ms", (lap["id"],)).fetchall()
        assert rows, "no samples stored"

        assert all(r["pos_x"] is not None for r in rows), "position not stored"
        xs = [r["pos_x"] for r in rows]
        zs = [r["pos_z"] for r in rows]
        assert len(set(xs)) > 1 and len(set(zs)) > 1, (
            f"the car never moved: x={set(xs)} z={set(zs)}")

        first = rows[0]
        assert first["roll"] == -0.02, tuple(first)
        assert first["pitch"] == 0.01, tuple(first)
        assert first["tc_active"] == 0.3 and first["abs_active"] == 0.4, \
            tuple(first)
        print(f"  {len(rows)} samples, x {xs[0]:.0f}->{xs[-1]:.0f}, "
              f"z {zs[0]:.0f}->{zs[-1]:.0f}, attitude and electronics stored")


def test_a_sim_without_the_newer_fields_still_records():
    """An older CSP, or a shared-memory layout missing them, must not crash.

    The collector reads these through getattr for exactly this reason, and
    a missing field has to land as NULL rather than as a zero coordinate.
    """
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        sim = FakeSim()
        del sim.graphics.carCoordinates
        del sim.physics.roll

        script = [
            lambda s, c: tick(s, c),
            lambda s, c: (tick(s, c), complete_lap(s, c, 114000)),
        ]
        run_collector(script, path, sim=sim)

        conn = db.connect(path)
        row = conn.execute(
            "SELECT pos_x, roll, pitch FROM samples").fetchone()
        assert row["pos_x"] is None and row["roll"] is None, tuple(row)
        # The fields that ARE present still arrive.
        assert row["pitch"] == 0.01, tuple(row)
        print("  missing fields recorded as NULL, present ones still stored")
        conn.close()


def test_start_does_not_return_the_status_it_had_before_starting():
    """start_recording answered "stopped" on every first call, for months.

    start() returned the instant the thread was spawned, so the tool read
    `status` before the thread had touched it and reported the
    constructor's default. Calling it a second time "worked" only because
    the second call arrived late enough to see the truth -- which taught
    both of us to distrust a tool that was working.
    """
    with tempfile.TemporaryDirectory() as d:
        sim = FakeSim()
        col = Collector(Path(d) / "t.db", lambda: sim)
        try:
            col.start()
            assert col.status != "stopped", (
                "start() returned before the thread published a status")
            assert col.status != "starting", (
                f"start() gave up waiting: {col.status!r}")
            assert col.running
            print(f"  first call to start() reports {col.status!r}")
        finally:
            col.stop()


def test_a_fresh_collector_is_distinguishable_from_a_stopped_one():
    """The ambiguity that cost sixteen laps and that I misread twice.

    Both report running=False, status="stopped", error=None and
    laps_recorded=0 -- the constructor's defaults are also what stop()
    leaves behind. But they mean opposite things: one is "the process was
    replaced underneath a driving session and nothing resumed", the other
    is "you asked me to stop".
    """
    with tempfile.TemporaryDirectory() as d:
        sim = FakeSim()
        col = Collector(Path(d) / "t.db", lambda: sim)

        # Never asked to run: the state a restarted server presents.
        assert col.ever_started is False
        assert col.stopped_by_request is False
        assert col.status == "stopped" and col.last_error is None

        col.start()
        wait_for(lambda: col.session_id is not None, "a session to open")
        assert col.ever_started is True
        col.stop()

        # Same status string, same error, same lap count -- different cause.
        assert col.status == "stopped" and col.last_error is None
        assert col.ever_started is True
        assert col.stopped_by_request is True
        print("  never-started and stopped-on-request now tell themselves "
              "apart")


def test_restarting_clears_the_previous_runs_error():
    """A stale error made a healthy restarted collector look broken."""
    with tempfile.TemporaryDirectory() as d:
        sim = FakeSim()
        col = Collector(Path(d) / "t.db", lambda: sim)
        col.last_error = "something that already happened"
        try:
            col.start()
            assert col.last_error is None, col.last_error
        finally:
            col.stop()


# --- surviving an absent game ------------------------------------------
#
# The collector used to try the sim exactly once and retire if it wasn't
# there. Combined with a host that restarts this process whenever it likes,
# that is how sixteen laps were lost across three evenings -- each one
# driven past a collector that had already given up, reporting a status
# indistinguishable from one nobody had ever started.


def test_a_missing_game_is_waited_for_rather_than_fatal():
    """AC not being open yet is a state, not a failure."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        sim = FakeSim()
        attempts = {"n": 0}

        def factory():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("shared memory not available")
            return sim

        col = Collector(path, factory)
        col.SIM_RETRY_SECONDS = 0.05
        col.start()
        try:
            wait_for(lambda: col.session_id is not None,
                     "a session once the game appears")
            assert attempts["n"] >= 3, attempts
            # And the failures along the way are not left looking like the
            # collector's final word.
            assert col.last_error is None, col.last_error
            print(f"  recovered after {attempts['n'] - 1} failed attempts")
        finally:
            col.stop()


def test_stop_is_immediate_while_waiting_for_the_game():
    """The retry must go through the stop event, not through sleep().

    A collector that waits in sleep() cannot be stopped for as long as the
    retry interval, which would make stop_recording hang and -- worse --
    make the collector look wedged at exactly the moment someone is trying
    to diagnose it.
    """
    with tempfile.TemporaryDirectory() as d:
        def never():
            raise RuntimeError("no AC here")

        col = Collector(Path(d) / "t.db", never)
        col.SIM_RETRY_SECONDS = 30.0
        col.start()
        wait_for(lambda: "waiting for Assetto Corsa" in col.status,
                 "the collector to notice the game is absent")

        began = time.monotonic()
        col.stop()
        took = time.monotonic() - began
        assert took < 5.0, f"stop() took {took:.1f}s against a 30s retry"
        assert not col.running
        print(f"  stopped in {took:.2f}s despite a 30s retry interval")


def test_a_crash_in_the_recording_loop_is_not_the_end_of_recording():
    """One bad read used to retire the collector for the whole process."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        good = FakeSim()
        made = {"n": 0}

        class Exploding(FakeSim):
            @property
            def graphics(self):
                raise RuntimeError("shared memory went away")

        def factory():
            made["n"] += 1
            return Exploding() if made["n"] == 1 else good

        col = Collector(path, factory)
        col.SIM_RETRY_SECONDS = 0.05
        col.start()
        try:
            wait_for(lambda: col.session_id is not None,
                     "recording to resume after the crash")
            assert made["n"] >= 2, made
            print(f"  recovered after a loop crash on attempt {made['n'] - 1}")
        finally:
            col.stop()


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
