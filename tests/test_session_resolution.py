"""Which session inbound driver data is filed against.

The app runs one server instance per client surface and only one of them
wins the bridge port, so the process receiving the driver's notes is
routinely not the process doing the recording. Everything here exists
because the overlay and the write path disagreeing is worse than either
being wrong on its own: a note filed into a plausible-looking session from
another circuit is far harder to notice than one filed nowhere.
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (FakeCollector, age_session, clear_heartbeat,  # noqa: E402
                     get, heartbeat_session, make_session, post, run_module)

from ac_race_engineer import bridge as B, db  # noqa: E402


def _bridge(path, collector):
    br = B.Bridge(path, collector, 0)
    br.start()
    time.sleep(0.2)
    assert br.error is None, br.error
    return br


def test_a_stale_session_is_never_written_to():
    """Nothing has touched this session for three days. It is not live."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        sid = make_session(conn, "monza")
        age_session(conn, sid, 3 * 86400)

        br = _bridge(path, FakeCollector())
        try:
            status = get(br.port, "/status")
            assert status["running"] is False, status
            assert status["session_id"] is None, status

            code, body = post(br.port, "/note", {
                "tag": "understeer", "spline": 0.34,
                "lap_count": 3, "speed_kmh": 120})
            assert code == 200
            # The note is kept -- the driver really did press the button --
            # but it must not be attached to the stale Monza session. NULL
            # is honest and findable; a guessed session_id is neither.
            assert body["orphaned"] is True, body
            assert body["session_id"] is None, body
            rows = [dict(r) for r in conn.execute(
                "SELECT session_id FROM notes")]
            assert rows == [{"session_id": None}], rows
            print("  stale session not written to; note kept as an orphan")
        finally:
            br.stop()
            conn.close()


def test_status_and_writes_agree_after_the_collector_stops():
    """collector.session_id survives stop(), so it cannot be trusted alone.

    Reproduced before the fix: /status reported session 2 (Mugello) while
    the same request cycle filed the note into session 1 (Monza).
    """
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        old = make_session(conn, "monza")
        new = make_session(conn, "mugello")

        stopped = FakeCollector(session_id=old, running=False,
                                status="stopped")
        br = _bridge(path, stopped)
        try:
            status = get(br.port, "/status")
            code, body = post(br.port, "/note", {
                "tag": "oversteer", "spline": 0.5,
                "lap_count": 1, "speed_kmh": 90})
            assert code == 200 and body["ok"] is True, body
            assert status["session_id"] == body["session_id"] == new, (
                status, body)
            rows = [dict(r) for r in conn.execute(
                "SELECT session_id FROM notes")]
            assert all(r["session_id"] == new for r in rows), rows
            print(f"  status and write path agree on session {new}")
        finally:
            br.stop()
            conn.close()


def test_this_instances_own_recording_takes_precedence():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        mine = make_session(conn, "mugello")
        make_session(conn, "spa")     # newer, but another instance's

        br = _bridge(path, FakeCollector(session_id=mine, running=True,
                                         status="recording"))
        try:
            assert br.active_session_id() == mine
            assert br.status_snapshot()["by_other"] is False
            print("  own recording wins over a newer foreign session")
        finally:
            br.stop()
            conn.close()


def test_another_instances_recording_is_reported_and_used():
    """The overlay saying "not recording" while laps are being stored was
    the single most misleading thing this tool did."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        theirs = make_session(conn, "mugello")

        br = _bridge(path, FakeCollector(running=False))
        try:
            snap = br.status_snapshot()
            assert snap["running"] is True, snap
            assert snap["by_other"] is True, snap
            assert snap["session_id"] == theirs, snap
            assert br.active_session_id() == theirs
            print("  other instance's session surfaced:", snap["status"])
        finally:
            br.stop()
            conn.close()


def test_orphaned_notes_are_countable():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        db.add_note(conn, None, 1, 0.3, "understeer", 100.0)
        db.add_note(conn, None, 1, 0.4, "oversteer", 100.0)
        db.add_note(conn, sid, 1, 0.5, "braking", 100.0)
        assert db.count_orphan_notes(conn) == 2
        assert len(db.list_notes(conn, sid)) == 1
        print("  orphaned notes are findable rather than silently lost")
        conn.close()


def test_a_collector_that_went_quiet_stops_claiming_to_record():
    """Seven laps at Suzuka went unrecorded behind a green indicator.

    A session row is created the instant a collector starts, and started_at
    never advances. Reading that timestamp meant a session whose collector
    had failed asserted itself as "recording, other instance" for a full
    fifteen minutes while the driver drove.

    The heartbeat is what separates the two. It stops when the collector
    does, so this is now noticed in seconds rather than minutes.
    """
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        sid = make_session(conn)          # no laps stored, ever
        col = FakeCollector(session_id=None)
        col.running = False
        br = B.Bridge(path, col, port=0)

        # Freshly created, heartbeat fresh: another instance is recording.
        assert br.status_snapshot()["running"] is True

        heartbeat_session(conn, sid, db.SESSION_STALE_SECONDS + 1)
        after = br.status_snapshot()
        assert after["running"] is False, after
        assert after["session_id"] is None, after
        # And nothing may be filed against it either.
        assert br.active_session_id() is None
        print(f"  heartbeat {db.SESSION_STALE_SECONDS + 1:.0f}s old "
              f"-> {after['running']}, id {after['session_id']}")
        conn.close()


def test_an_out_lap_is_not_mistaken_for_a_dead_session():
    """The failure the lap-based rules produced in the other direction.

    The first lap a session STORES is its first flying lap -- the out-lap
    has no time and is skipped by design. So judging liveness from stored
    laps declared a live session dead for the length of a garage sit plus
    an out-lap plus a flying lap: over five minutes at Sebring, and over
    ten at Nordschleife. During that window the overlay read "not
    recording" and every complaint tag the driver pressed was orphaned --
    on the first laps of a new setup, which is when she presses them.

    An hour, because a session can be that old and still not have stored a
    lap. What settles it is that the collector is still talking.
    """
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        sid = make_session(conn)          # no laps stored yet
        col = FakeCollector(session_id=None)
        col.running = False
        br = B.Bridge(path, col, port=0)

        age_session(conn, sid, 3600)
        heartbeat_session(conn, sid, 1.0)
        snap = br.status_snapshot()
        assert snap["running"] is True, snap
        assert snap["session_id"] == sid and snap["by_other"] is True, snap
        assert br.active_session_id() == sid
        print("  session an hour old with no laps and a live heartbeat "
              "-> still recording")
        conn.close()


def test_a_session_from_before_the_heartbeat_falls_back_to_its_laps():
    """v9 and earlier have no last_seen_at and never will.

    Guessing one from started_at would make a session that ended last week
    look like one being recorded right now, so these keep the old rule:
    the last stored lap, and the generous window it needs.
    """
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        sid = make_session(conn)
        db.store_lap(conn, sid, 1, 113000, True, [])
        clear_heartbeat(conn, sid)
        col = FakeCollector(session_id=None)
        col.running = False
        br = B.Bridge(path, col, port=0)

        # Well past the heartbeat window, well inside the lap window.
        age_session(conn, sid, db.SESSION_STALE_SECONDS + 120)
        clear_heartbeat(conn, sid)
        snap = br.status_snapshot()
        assert snap["running"] is True, snap
        assert snap["session_id"] == sid and snap["by_other"] is True, snap

        age_session(conn, sid, B.OTHER_INSTANCE_STALE_SECONDS + 60)
        clear_heartbeat(conn, sid)
        conn.execute("UPDATE laps SET completed_at = ? WHERE session_id = ?",
                     (time.time() - B.OTHER_INSTANCE_STALE_SECONDS - 60, sid))
        conn.commit()
        assert br.status_snapshot()["running"] is False
        print("  pre-v10 session still judged by its laps, both ways")
        conn.close()


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
