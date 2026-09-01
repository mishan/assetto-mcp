"""Which setup each lap was driven on.

AC's shared memory doesn't expose the loaded setup, so it has to be stated.
The subtlety is that the tuning loop changes setup *within* a session --
pit, load claude_v2, keep driving -- so attribution has to live on the lap.
Storing it on the session meant recording the new setup relabelled the
baseline laps as the new setup, destroying the A/B comparison the field
exists to enable.
"""

import atexit
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module  # noqa: E402

from assetto_mcp import db  # noqa: E402

# The trap these tests exist for lived in the *tool*, not in db.py: the SQL
# did exactly what it said, and the tool called it on every blank lap in the
# session. So this reaches through the server module, imported lazily against
# a scratch data directory and a bridge on port 0 so that importing this file
# starts nothing on its own.

_SERVER = None


def _server():
    global _SERVER
    if _SERVER is None:
        d = tempfile.mkdtemp(prefix="ac-attribution-")
        os.environ["ASSETTO_MCP_DATA"] = d
        os.environ["AC_DOCS_DIR"] = d
        os.environ["ASSETTO_MCP_BRIDGE_PORT"] = "0"
        os.environ["ASSETTO_MCP_NO_AUTOSTART"] = "1"
        import importlib
        _SERVER = importlib.import_module("assetto_mcp.server")
        atexit.register(_SERVER._bridge.stop)
    return _SERVER


def _call(fn, **kw):
    """Unwrap an MCP tool's JSON string into a dict."""
    inner = getattr(fn, "fn", fn)
    return json.loads(inner(**kw))


def _labels(srv, sid):
    return {l["id"]: (l.get("setup_name") or "")
            for l in db.list_laps(srv._conn, sid, limit=100)}


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


def test_naming_a_new_setup_does_not_relabel_the_baseline():
    """The Suzuka/Sebring bug, in the shape it actually happened.

    Drive a baseline nobody named, pit, load claude_v1, say so. The old tool
    filled every blank lap in the session, so the baseline became claude_v1
    and the A/B compared a run against itself. It cost roughly a dozen laps
    across two circuits before anyone noticed.
    """
    srv = _server()
    sid = make_session(srv._conn)
    baseline = [db.store_lap(srv._conn, sid, n, 113000 + n, True, [])
                for n in (1, 2, 3)]

    out = _call(srv.set_session_setup, setup_name="claude_v1", session_id=sid)
    assert out["ok"] is True, out

    labels = _labels(srv, sid)
    assert all(labels[i] == "" for i in baseline), \
        f"baseline was relabelled: {labels}"
    # And it has to say the laps are there, or "left alone" becomes "lost".
    assert sorted(out["unlabelled_laps"]) == sorted(baseline), out
    assert "label_laps" in out["note"], out

    # Laps from now on do carry it.
    after = db.store_lap(srv._conn, sid, 4, 112000, True, [])
    assert _labels(srv, sid)[after] == "claude_v1"
    print("  baseline kept blank, later laps tagged claude_v1")


def test_label_laps_fills_only_the_ids_it_is_given():
    srv = _server()
    sid = make_session(srv._conn)
    ids = [db.store_lap(srv._conn, sid, n, 113000 + n, True, [])
           for n in (1, 2, 3, 4)]

    out = _call(srv.label_laps, lap_ids=f"{ids[2]}, {ids[3]}",
                setup_name="claude_press_v1", session_id=sid)
    assert out["laps_labelled"] == 2, out

    labels = _labels(srv, sid)
    assert labels[ids[0]] == "" and labels[ids[1]] == "", labels
    assert labels[ids[2]] == "claude_press_v1", labels
    assert labels[ids[3]] == "claude_press_v1", labels
    print("  labelled exactly the two ids asked for")


def test_label_laps_will_not_overwrite_a_lap_that_already_has_a_name():
    # A late correction applied to the wrong half of an A/B destroys the
    # comparison it was meant to complete. That is what relabel_laps.py is
    # for, and why it is a script someone has to type.
    srv = _server()
    sid = make_session(srv._conn)
    db.set_session_setup(srv._conn, sid, "baseline")
    named = db.store_lap(srv._conn, sid, 1, 113000, True, [])
    db.set_session_setup(srv._conn, sid, "")
    blank = db.store_lap(srv._conn, sid, 2, 113500, True, [])

    out = _call(srv.label_laps, lap_ids=f"{named},{blank}",
                setup_name="claude_v9", session_id=sid)
    assert out["laps_labelled"] == 1, out
    assert out["left_alone"] == {str(named): "baseline"}, out
    assert "relabel_laps.py" in out["note"], out

    labels = _labels(srv, sid)
    assert labels[named] == "baseline", labels
    assert labels[blank] == "claude_v9", labels


def test_label_laps_reports_ids_that_are_not_in_this_session():
    # Silently labelling nothing looks identical to succeeding.
    srv = _server()
    sid = make_session(srv._conn)
    real = db.store_lap(srv._conn, sid, 1, 113000, True, [])
    out = _call(srv.label_laps, lap_ids=f"{real},999999",
                setup_name="claude_v1", session_id=sid)
    assert out["not_in_this_session"] == [999999], out
    assert out["laps_labelled"] == 1, out


def test_space_separated_ids_are_read_as_separate_laps():
    """"87 88 89" used to collapse into the single id 878889.

    The old parser stripped spaces and then split on commas, so that read as
    one lap that does not exist: nothing labelled, and "ok": true. A no-op
    that reports success is the worst answer available here, and the README
    invites exactly this phrasing ("laps 41 to 44 were on baseline").
    """
    srv = _server()
    sid = make_session(srv._conn)
    ids = [db.store_lap(srv._conn, sid, n, 113000 + n, True, [])
           for n in (1, 2, 3)]

    out = _call(srv.label_laps, lap_ids=" ".join(str(i) for i in ids),
                setup_name="baseline", session_id=sid)
    assert out["laps_labelled"] == 3, out
    assert all(v == "baseline" for v in _labels(srv, sid).values())


def test_labelling_nothing_at_all_does_not_report_ok():
    srv = _server()
    sid = make_session(srv._conn)
    db.store_lap(srv._conn, sid, 1, 113000, True, [])
    out = _call(srv.label_laps, lap_ids="999998,999999",
                setup_name="claude_v1", session_id=sid)
    assert out["ok"] is False, out
    assert out["laps_labelled"] == 0, out


def test_relabelling_a_lap_to_the_name_it_already_has_is_acknowledged():
    # Neither filled nor left alone, so it used to vanish from the report.
    srv = _server()
    sid = make_session(srv._conn)
    db.set_session_setup(srv._conn, sid, "baseline")
    lap = db.store_lap(srv._conn, sid, 1, 113000, True, [])
    out = _call(srv.label_laps, lap_ids=str(lap), setup_name="baseline",
                session_id=sid)
    assert out["already_labelled"] == [lap], out
    assert out["ok"] is True, out


def test_a_lap_older_than_any_window_is_not_called_missing():
    # list_laps defaults to a limit; reporting an old lap as "not in this
    # session" is a false claim about the driver's own data.
    srv = _server()
    sid = make_session(srv._conn)
    ids = [db.store_lap(srv._conn, sid, n, 113000 + n, True, [])
           for n in range(1, 60)]
    oldest = ids[0]
    out = _call(srv.label_laps, lap_ids=str(oldest), setup_name="baseline",
                session_id=sid)
    assert "not_in_this_session" not in out, out
    assert out["laps_labelled"] == 1, out
    assert _labels(srv, sid)[oldest] == "baseline"


def test_neither_tool_reads_the_whole_session_to_answer():
    """Both used to fetch every lap row through the sessions JOIN.

    set_session_setup wanted a count and some ids; label_laps wanted the
    setup names of the handful of ids it was given. On a long session that
    is hundreds of wide rows read and discarded, on every call.
    """
    srv = _server()
    sid = make_session(srv._conn)
    ids = [db.store_lap(srv._conn, sid, n, 113000 + n, True, [])
           for n in range(1, 41)]

    # set_trace_callback rather than monkeypatching execute, which sqlite3
    # will not allow on a Connection.
    seen = []
    srv._conn.set_trace_callback(seen.append)
    try:
        out = _call(srv.set_session_setup, setup_name="claude_v1",
                    session_id=sid)
        assert len(out["unlabelled_laps"]) == 40, out
        out = _call(srv.label_laps, lap_ids=f"{ids[0]},{ids[1]}",
                    setup_name="baseline", session_id=sid)
        assert out["laps_labelled"] == 2, out
    finally:
        srv._conn.set_trace_callback(None)

    reads = [s for s in seen
             if s.lstrip().upper().startswith("SELECT") and "laps" in s]
    assert reads, "no lap query was observed at all"
    wide = [s for s in reads if "laps.*" in s or "sessions.car" in s]
    assert not wide, f"still reading whole lap rows: {wide}"
    print(f"  {len(reads)} narrow lap queries, no full-row scan")


def test_label_laps_rejects_ids_that_are_not_numbers():
    srv = _server()
    sid = make_session(srv._conn)
    out = _call(srv.label_laps, lap_ids="87,eighty-eight",
                setup_name="claude_v1", session_id=sid)
    assert "error" in out, out


def test_labelling_an_empty_id_list_changes_nothing():
    # db.label_unattributed_laps(lap_ids=[]) must not fall through to the
    # unfiltered UPDATE, which is the whole bug wearing a different hat.
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        db.store_lap(conn, sid, 1, 113000, True, [])
        assert db.label_unattributed_laps(conn, sid, "nope", []) == 0
        assert db.list_laps(conn, sid)[0]["setup_name"] == ""
        conn.close()


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
