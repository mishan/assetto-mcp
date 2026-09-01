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
                     restart_from_menu, run_collector, run_module, temp_db,
                     tick, timed_laps, wait_for)

from assetto_mcp import db, retention  # noqa: E402
from assetto_mcp.collector import Collector  # noqa: E402


def test_restart_from_the_menu_starts_a_new_session():
    """Restarting in-game never passes through AC_OFF.

    Watching only for that transition left the new run appended to the old
    session: same session_id, lap numbers starting over, and two different
    track states averaged together.
    """
    with temp_db() as path:
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
    with temp_db() as path:
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
    with temp_db() as path:
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
    with temp_db() as path:
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
    with temp_db() as path:
        script = [
            lambda s, c: tick(s, c),
            lambda s, c: (tick(s, c), complete_lap(s, c, 114000)),
        ]
        run_collector(script, path)

        conn = db.connect(path)
        try:
            lap = db.list_laps(conn)[0]
            rows = conn.execute(
                "SELECT pos_x, pos_y, pos_z, heading, pitch, roll,"
                " tc_active, abs_active FROM samples WHERE lap_id = ?"
                " ORDER BY t_ms", (lap["id"],)).fetchall()
        finally:
            # try/finally rather than a trailing close(): an assertion below
            # would otherwise skip the close and turn one failing test into
            # a second, unrelated-looking teardown error on Windows.
            conn.close()
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
    with temp_db() as path:
        sim = FakeSim()
        del sim.graphics.carCoordinates
        del sim.physics.roll

        script = [
            lambda s, c: tick(s, c),
            lambda s, c: (tick(s, c), complete_lap(s, c, 114000)),
        ]
        run_collector(script, path, sim=sim)

        conn = db.connect(path)
        try:
            row = conn.execute(
                "SELECT pos_x, roll, pitch FROM samples").fetchone()
        finally:
            conn.close()
        assert row["pos_x"] is None and row["roll"] is None, tuple(row)
        # The fields that ARE present still arrive.
        assert row["pitch"] == 0.01, tuple(row)
        print("  missing fields recorded as NULL, present ones still stored")


def test_a_short_array_is_not_recorded_as_a_partial_reading():
    """A present-but-shorter field is a different failure from an absent one.

    getattr answers "does this field exist", which is not the same claim as
    "does it have four entries". Indexing straight into it raises IndexError
    from inside the sampling loop -- which surfaces as an exception out of
    _loop, ending the session and retrying, so one element short would have
    cost the driver a whole run rather than one nullable column.

    Recorded as absent rather than padded: three wheels of wear is not a
    measurement of a four-wheeled car, and a reader has to be able to tell
    "not recorded" from a number.
    """
    with temp_db() as path:
        sim = FakeSim()
        sim.physics.tyreWear = [99.5, 99.4, 99.2]      # three, not four
        sim.physics.carDamage = [0.0, 1.0]             # two, not five
        sim.graphics.carCoordinates = [100.0, 5.0]     # two, not three

        script = [
            lambda s, c: tick(s, c),
            lambda s, c: (tick(s, c), complete_lap(s, c, 114000)),
        ]
        col = run_collector(script, path, sim=sim)
        assert col.laps_recorded == 1, col.last_error
        assert col.last_error is None, col.last_error

        conn = db.connect(path)
        try:
            row = conn.execute(
                "SELECT wear_fl, wear_rr, damage, pos_x, pos_z, pitch"
                " FROM samples").fetchone()
        finally:
            conn.close()
        assert row["wear_fl"] is None and row["wear_rr"] is None, tuple(row)
        assert row["damage"] is None, tuple(row)
        # Position too: an x with no z is not a place, so a short
        # carCoordinates is absent rather than partially recorded.
        assert row["pos_x"] is None and row["pos_z"] is None, tuple(row)
        # And the lap still recorded, with everything else intact.
        assert row["pitch"] == 0.01, tuple(row)
        print("  short arrays stored as NULL; the lap recorded anyway")


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
    with temp_db() as path:
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
    with temp_db() as path:
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


# --- one recorder, however many server processes -----------------------
#
# Claude Desktop runs one server per client surface, so several of these
# processes are alive at once, reading the same shared memory and writing
# the same database file. That was harmless only while a human had to call
# start_recording to begin. Autostart removed the accident, and the
# duplicates it produced survive into compare_runs as a sample with zero
# deviation from itself -- which is exactly what makes a t-test certain
# about a change that never happened.


def test_only_one_of_two_collectors_records():
    with temp_db() as path:
        sim = FakeSim()
        a = Collector(path, lambda: sim)
        b = Collector(path, lambda: sim)
        try:
            a.start()
            b.start()
            wait_for(lambda: a.holds_recorder or b.holds_recorder,
                     "one collector to take the claim")
            holder = a if a.holds_recorder else b
            standby = b if holder is a else a
            wait_for(lambda: standby.standby_owner is not None,
                     "the other to notice it is standing by")

            assert not standby.holds_recorder
            assert "standby" in standby.status, standby.status

            # FakeSim is live from the start, so the holder has already
            # opened its session by now.
            wait_for(lambda: holder.session_id is not None, "a session")
            tick(sim, holder)
            complete_lap(sim, holder, 0)          # out-lap, kept + flagged
            tick(sim, holder)
            complete_lap(sim, holder, 113000)

            conn = db.connect(path)
            try:
                sessions = conn.execute(
                    "SELECT COUNT(*) c FROM sessions").fetchone()["c"]
                # Out-laps are stored now, so count the timed ones: the
                # point of this test is that the lap was not written twice.
                timed = timed_laps(conn)
                out = [l for l in db.list_laps(conn, limit=None)
                       if l["out_lap"]]
            finally:
                conn.close()
            assert sessions == 1, f"{sessions} sessions for one game session"
            assert len(timed) == 1, f"{len(timed)} rows for one timed lap"
            assert len(out) == 1, "the out-lap should be kept and flagged"
            assert standby.laps_recorded == 0
            print(f"  1 timed lap + 1 out-lap stored, {sessions} session")
        finally:
            a.stop()
            b.stop()


def test_a_standby_takes_over_when_the_holder_stops():
    """The whole reason the standby stays alive.

    The holder is a server process the host can recycle at any moment. If
    nothing picks the claim up, that is the sixteen-laps failure again with
    an extra step.
    """
    with temp_db() as path:
        sim = FakeSim()
        a = Collector(path, lambda: sim)
        b = Collector(path, lambda: sim)
        a.STANDBY_RETRY_SECONDS = b.STANDBY_RETRY_SECONDS = 0.05
        try:
            a.start()
            b.start()
            wait_for(lambda: a.holds_recorder or b.holds_recorder, "a holder")
            holder = a if a.holds_recorder else b
            standby = b if holder is a else a
            wait_for(lambda: standby.standby_owner is not None, "a standby")

            holder.stop()
            wait_for(lambda: standby.holds_recorder,
                     "the standby to take the claim over", timeout=20)
            wait_for(lambda: standby.session_id is not None,
                     "the standby to open a session")

            tick(sim, standby)
            complete_lap(sim, standby, 0)         # out-lap, kept + flagged
            tick(sim, standby)
            complete_lap(sim, standby, 112500)
            assert standby.laps_recorded == 2, "out-lap + timed lap"
            assert standby.out_laps_recorded == 1
            print("  standby picked the claim up and recorded")
        finally:
            a.stop()
            b.stop()


def test_a_collector_that_loses_its_claim_stops_writing():
    """Losing it means another instance believes it is recording this.

    That only happens after RECORDER_STALE_SECONDS of silence from us, so
    something has stalled this process badly -- and the wrong response is to
    carry on and be the second writer.
    """
    with temp_db() as path:
        sim = FakeSim()
        col = Collector(path, lambda: sim)
        col.HEARTBEAT_SECONDS = 0.01
        col.STANDBY_RETRY_SECONDS = 30.0     # so it cannot re-claim and hide
        try:
            col.start()
            wait_for(lambda: col.session_id is not None, "a session")

            conn = db.connect(path)
            db.claim_recorder(conn, "someone-else", stale_after=0.0)
            conn.close()

            wait_for(lambda: not col.holds_recorder,
                     "the collector to notice it lost the claim")
            assert "standby" in col.status, col.status
            assert "took over" in (col.last_error or ""), col.last_error
            # Cleared in the same breath as holds_recorder, so a reader
            # cannot see "not holding the recorder" and "session 4" at once
            # -- _collector_state checks session_id first and would answer
            # "recording" for a collector that had stopped writing.
            #
            # This assertion does not currently discriminate: _run's finally
            # clears it on the next statement, with no I/O in between, so it
            # passes with or without the explicit clear. It is here as a
            # tripwire for that gap widening, not as a reproduction.
            assert col.session_id is None, (
                f"session {col.session_id} outlived the stand-down")
            assert col.last_session_id is not None
            print(f"  {col.status!r} / {col.last_error!r}")
        finally:
            col.stop()


def test_a_shared_stop_reaches_the_instance_actually_recording():
    """The whole point of putting `enabled` in the database.

    stop_recording is normally typed into whichever chat the driver has
    open, and that is normally NOT the process holding the recorder. The
    holder is inside _loop, which does not return to the outer loop while a
    session is live -- and the outer loop was the only place the flag was
    read. So the driver could switch recording off, be told it had stopped,
    and have the holder keep writing laps until the game closed.
    """
    with temp_db() as path:
        sim = FakeSim()
        holder = Collector(path, lambda: sim)
        holder.HEARTBEAT_SECONDS = 0.01
        holder.STANDBY_RETRY_SECONDS = 0.05
        try:
            holder.start()
            wait_for(lambda: holder.session_id is not None, "a session")
            tick(sim, holder)
            complete_lap(sim, holder, 0)          # out-lap, kept + flagged
            tick(sim, holder)
            complete_lap(sim, holder, 113000)
            assert holder.laps_recorded == 2, "out-lap + timed lap"

            # Another instance's stop_recording: the flag, and nothing else.
            # No stop() on this collector, because the driver never touched
            # this process.
            other = db.connect(path)
            try:
                db.set_recorder_enabled(other, False)
            finally:
                other.close()

            wait_for(lambda: not holder.holds_recorder,
                     "the holder to stand down", timeout=10)
            assert "switched off" in holder.status, holder.status
            # Not an error: it did what it was told.
            assert holder.last_error is None, holder.last_error

            # And it really has stopped writing. Driven by hand rather than
            # through tick(), which waits for a sample that must never
            # arrive -- the absence is the assertion.
            before = holder.laps_recorded
            samples_before = holder.samples_taken
            for _ in range(8):
                sim.graphics.normalizedCarPosition = (
                    sim.graphics.normalizedCarPosition + 0.1) % 1.0
                sim.physics.packetId += 1
                time.sleep(0.02)
            sim.graphics.iLastTime = 112800
            sim.graphics.completedLaps += 1
            time.sleep(0.3)
            assert holder.laps_recorded == before, (
                f"{holder.laps_recorded - before} lap(s) stored after "
                f"recording was switched off")
            assert holder.samples_taken == samples_before, (
                "still sampling after being switched off")
            print(f"  holder stood down: {holder.status!r}")
        finally:
            holder.stop()


def test_stopping_recording_is_shared_and_survives_a_restart():
    """stop_recording used to stop one process until the next restart.

    With every instance autostarting a collector, that stopped whichever
    chat the driver was typing into and left the others recording -- and
    the host undid it the moment it recycled the server.
    """
    with temp_db() as path:
        sim = FakeSim()
        conn = db.connect(path)
        db.set_recorder_enabled(conn, False)

        col = Collector(path, lambda: sim)
        col.STANDBY_RETRY_SECONDS = 0.05
        try:
            col.start()
            wait_for(lambda: "switched off" in col.status,
                     "the collector to honour the shared setting")
            go_live_attempted = col.sessions_started
            assert go_live_attempted == 0, col.sessions_started

            db.set_recorder_enabled(conn, True)
            wait_for(lambda: col.holds_recorder,
                     "recording to resume once it is switched back on")
            print("  a fresh collector honours a stop from a previous run")
        finally:
            col.stop()
            conn.close()


def test_a_database_that_cannot_be_opened_says_so():
    """This used to kill the thread before it published anything, leaving
    status at "starting" and last_error at None -- reported as "died", with
    no reason, by the very function written to end that ambiguity."""
    with tempfile.TemporaryDirectory() as d:
        # A directory where the database file should be.
        path = Path(d) / "t.db"
        path.mkdir()
        col = Collector(path, FakeSim)
        col.start()
        assert not col.running
        assert col.status.startswith("error"), col.status
        assert col.last_error, "no reason given"
        print(f"  {col.status!r}: {col.last_error!r}")


def test_standing_aside_does_not_keep_reporting_an_old_error():
    """Healthy standby must not carry a stale reason to worry.

    Both standby branches are states, not failures: another instance holds
    the recorder, or recording is switched off. A transient problem before
    either -- shared memory briefly unavailable, say -- left last_error set,
    and recording_status then showed a collector doing exactly the right
    thing next to an error explaining nothing about it.
    """
    with temp_db() as path:
        sim = FakeSim()
        holder = Collector(path, lambda: sim)
        standby = Collector(path, lambda: sim)
        standby.STANDBY_RETRY_SECONDS = 0.05
        try:
            holder.start()
            wait_for(lambda: holder.holds_recorder, "the holder to claim")

            standby.start()
            wait_for(lambda: standby.standby_owner is not None,
                     "the second collector to stand aside")
            # Set while it is already standing by, not before start() --
            # start() clears last_error itself, so setting it first proves
            # nothing about the standby branch.
            standby.last_error = "shared memory was briefly unavailable"
            wait_for(lambda: standby.last_error is None,
                     "standby to drop the stale error", timeout=10)
            assert "another instance" in standby.status, standby.status

            # And the switched-off branch, which had this already.
            conn = db.connect(path)
            try:
                db.set_recorder_enabled(conn, False)
            finally:
                conn.close()
            holder.last_error = "something that already happened"
            wait_for(lambda: "switched off" in holder.status,
                     "the holder to stand down", timeout=10)
            assert holder.last_error is None, holder.last_error
            print("  both standby states report no error")
        finally:
            holder.stop()
            standby.stop()


def test_a_retention_pass_that_loses_the_claim_stops_the_collector():
    """The forced heartbeat's answer was computed and thrown away.

    A VACUUM over a large database can outlast RECORDER_STALE_SECONDS, and
    the beat after the pass exists to notice that another instance took
    over. Discarding it left this collector carrying on into sampling while
    a second one recorded the same laps -- the one failure the claim exists
    to prevent.
    """
    with temp_db() as path:
        sim = FakeSim()
        col = Collector(path, lambda: sim)
        col._conn = db.connect(path)
        try:
            # Hold the claim, then hand it to somebody else mid-pass.
            db.claim_recorder(col._conn, col._owner)
            col.holds_recorder = True

            real = retention.enforce_budget

            def steal_the_claim(conn, db_path, budget=None):
                # What a long VACUUM looks like from outside: our heartbeat
                # goes stale and another instance takes the claim.
                taken = db.claim_recorder(conn, "somebody-else", stale_after=0)
                assert taken["held"], taken
                return {"acted": False}

            retention.enforce_budget = steal_the_claim
            try:
                stand_down = col._housekeeping()
            finally:
                retention.enforce_budget = real

            assert stand_down, "losing the claim mid-pass must be reported"
            print("  stand-down propagated:", stand_down)
        finally:
            col._conn.close()


def test_a_retention_report_does_not_outlive_its_pass():
    """One thinning event made every later status say it had just happened.

    last_retention was only ever assigned, never cleared, so a pass that did
    nothing left the previous pass's report standing -- for the rest of the
    process's life.
    """
    with temp_db() as path:
        sim = FakeSim()
        col = Collector(path, lambda: sim)
        col._conn = db.connect(path)
        try:
            db.claim_recorder(col._conn, col._owner)
            col.holds_recorder = True
            col.last_retention = {"acted": True, "actions": ["thinned 40"]}
            col._housekeeping()          # a pass that does nothing
            assert col.last_retention is None, col.last_retention
        finally:
            col._conn.close()


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
