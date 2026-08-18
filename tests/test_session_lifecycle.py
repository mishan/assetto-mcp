"""When a new session begins, and when the old one stops being current.

Sessions are the unit everything else is filed against, so a missed boundary
merges two runs at different track grip into one set of lap numbers, and a
boundary that lingers after recording stops leaves data attached to a session
that has ended.
"""

import sys
import tempfile
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


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
