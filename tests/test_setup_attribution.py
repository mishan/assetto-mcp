"""Which setup each lap was driven on.

AC's shared memory doesn't expose the loaded setup, so it has to be stated.
The subtlety is that the tuning loop changes setup *within* a session --
pit, load claude_v2, keep driving -- so attribution has to live on the lap.
Storing it on the session meant recording the new setup relabelled the
baseline laps as the new setup, destroying the A/B comparison the field
exists to enable.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module  # noqa: E402

from ac_race_engineer import db  # noqa: E402


def test_a_lap_is_tagged_with_the_setup_current_when_it_was_stored():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        assert db.set_session_setup(conn, sid, "claude_v3") is True
        db.store_lap(conn, sid, 1, 114000, True, [])
        assert db.list_laps(conn)[0]["setup_name"] == "claude_v3"
        print("  lap tagged claude_v3")
        conn.close()


def test_changing_setup_mid_session_does_not_rewrite_history():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)

        db.set_session_setup(conn, sid, "baseline")
        db.store_lap(conn, sid, 1, 114000, True, [])
        # Driver pits and loads the new setup, still the same session.
        db.set_session_setup(conn, sid, "claude_v2")
        db.store_lap(conn, sid, 2, 113000, True, [])

        by_number = {l["lap_number"]: l["setup_name"]
                     for l in db.list_laps(conn, sid)}
        assert by_number == {1: "baseline", 2: "claude_v2"}, by_number
        print("  baseline stays the baseline:", by_number)
        conn.close()


def test_setting_a_setup_reports_what_it_does_not_cover():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        assert db.set_session_setup(conn, sid, "v1") is True
        assert db.set_session_setup(conn, 9999, "nope") is False
        assert db.session_setup(conn, sid) == "v1"
        assert db.session_setup(conn, 9999) == ""
        print("  session setup readable, unknown session reported")
        conn.close()


def test_an_untagged_lap_is_empty_not_wrong():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        db.store_lap(conn, sid, 1, 114000, True, [])
        assert db.list_laps(conn)[0]["setup_name"] == ""
        print("  no setup recorded -> empty, never a guess")
        conn.close()


def test_an_explicit_setup_name_overrides_the_session_default():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        db.set_session_setup(conn, sid, "session_default")
        db.store_lap(conn, sid, 1, 114000, True, [], setup_name="explicit")
        assert db.list_laps(conn)[0]["setup_name"] == "explicit"
        print("  caller can state the setup for a specific lap")
        conn.close()


def test_blank_laps_are_filled_in_but_named_ones_are_not():
    """A blank is a gap; a name is a competing claim.

    Telling the tool which setup you were on *after* a run is the normal
    case -- five laps of claude_arb_v1 were stored unattributed for exactly
    that reason. Filling those completes a comparison. Overwriting a lap
    that already names a different setup would destroy one, which is why
    the two cases are not treated alike.
    """
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)

        # Three laps with no setup recorded, then two on a known one.
        for n in range(1, 4):
            db.store_lap(conn, sid, n, 113000 + n, True, [])
        db.set_session_setup(conn, sid, "claude_camber_v2")
        for n in (4, 5):
            db.store_lap(conn, sid, n, 113500 + n, True,
                         [], setup_name="claude_camber_v2")

        filled = db.label_unattributed_laps(conn, sid, "claude_arb_v1")
        assert filled == 3, filled

        labels = {}
        for lap in db.list_laps(conn, sid):
            labels[lap["lap_number"]] = lap["setup_name"] or ""
        assert labels[1] == "claude_arb_v1", labels
        assert labels[3] == "claude_arb_v1", labels
        assert labels[4] == "claude_camber_v2", "must not overwrite a name"
        assert labels[5] == "claude_camber_v2", "must not overwrite a name"
        print(f"  filled {filled} blanks, left 2 named laps alone")
        conn.close()


def test_filling_blanks_twice_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        db.store_lap(conn, sid, 1, 113000, True, [])
        assert db.label_unattributed_laps(conn, sid, "claude_arb_v1") == 1
        # Second call finds nothing blank, and must not relabel its own work.
        assert db.label_unattributed_laps(conn, sid, "something_else") == 0
        assert db.list_laps(conn, sid)[0]["setup_name"] == "claude_arb_v1"
        conn.close()


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
