"""Which setup each lap was driven on, as measured rather than as stated.

A setup name is a claim someone types, and it has been wrong every way a
claim can be: stated late, stated early, not stated after a garage stop.
The in-game app reads every value off the setup menu, so each lap carries a
fingerprint of what the car actually had, and the name only follows laps
driven on the values it was stated for.
"""

import atexit
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module, temp_db  # noqa: E402

from assetto_mcp import db  # noqa: E402

CAR = "ks_mazda_mx5_cup"
BASE = {"ARB_FRONT": 3.0, "ARB_REAR": 2.0, "CAMBER_LF": -25.0, "FUEL": 20.0}
STIFF = {**BASE, "ARB_REAR": 5.0}

_SERVER = None


def _server():
    global _SERVER
    if _SERVER is None:
        d = tempfile.mkdtemp(prefix="ac-measured-")
        os.environ["ASSETTO_MCP_DATA"] = d
        os.environ["AC_DOCS_DIR"] = d
        os.environ["ASSETTO_MCP_BRIDGE_PORT"] = "0"
        os.environ["ASSETTO_MCP_NO_AUTOSTART"] = "1"
        import importlib
        _SERVER = importlib.import_module("assetto_mcp.server")
        atexit.register(_SERVER._bridge.stop)
    return _SERVER


def _call(fn, **kw):
    inner = getattr(fn, "fn", fn)
    return json.loads(inner(**kw))


def _measure(conn, sid, values, ago=300.0):
    """The app reporting a setup `ago` seconds before now -- in the garage,
    before the laps that follow were driven."""
    fp = db.record_measured_setup(conn, sid, CAR, values, time.time() - ago)
    conn.commit()
    return fp


def _lap(conn, sid, n):
    return db.store_lap(conn, sid, n, 100000, True, [])


def _laps(conn, sid):
    return {l["id"]: l for l in db.list_laps(conn, sid, limit=100)}


def test_a_fingerprint_is_the_values_not_their_order_or_the_fuel():
    a = db.setup_fingerprint(CAR, BASE)
    assert a == db.setup_fingerprint(CAR, dict(reversed(list(BASE.items()))))
    assert a == db.setup_fingerprint(CAR, {**BASE, "FUEL": 45.0}), \
        "a refuel is the same setup"
    assert a == db.setup_fingerprint(CAR, {**BASE, "ARB_REAR": 2.0000000001})
    assert a != db.setup_fingerprint(CAR, STIFF)
    assert a != db.setup_fingerprint("other_car", BASE)
    assert db.setup_fingerprint(CAR, {}) == ""
    assert db.setup_fingerprint(CAR, {"FUEL": 20.0}) == ""
    print(f"  {a}: order, float noise and fuel do not change it")


def test_the_history_records_changes_not_reports():
    with temp_db() as path:
        conn = db.connect(path)
        sid = make_session(conn, car=CAR)
        # The app re-posts an unchanged setup at every session change.
        for values, ago in ((BASE, 30), (BASE, 20), (STIFF, 10), (BASE, 5)):
            _measure(conn, sid, values, ago)
        rows = conn.execute("SELECT fp FROM setup_history WHERE"
                            " session_id = ?", (sid,)).fetchall()
        assert len(rows) == 3, [r["fp"] for r in rows]
        assert db.measured_setup_at(conn, sid) == \
            db.setup_fingerprint(CAR, BASE)
        assert db.measured_setup_at(conn, sid, time.time() - 15) == \
            db.setup_fingerprint(CAR, BASE)
        assert db.measured_setup_at(conn, sid, time.time() - 100) == ""
        conn.close()


def test_a_snapshot_from_the_game_is_measured():
    with temp_db() as path:
        conn = db.connect(path)
        sid = make_session(conn, car=CAR)
        db.store_setup_snapshot(conn, sid, CAR, [
            {"name": k, "value": v, "min": 0, "max": 100, "step": 1}
            for k, v in BASE.items()])
        fp = db.measured_setup_at(conn, sid)
        assert fp == db.setup_fingerprint(CAR, BASE), fp
        assert db.setup_fingerprint_values(conn, [fp])[fp] == BASE
        conn.close()


def test_every_lap_carries_the_setup_it_was_measured_on():
    with temp_db() as path:
        conn = db.connect(path)
        sid = make_session(conn, car=CAR)
        unmeasured = _lap(conn, sid, 1)
        a = _measure(conn, sid, BASE, ago=200)
        on_a = _lap(conn, sid, 2)
        # Garage stop inside the next lap: it spans two setups.
        b = _measure(conn, sid, STIFF, ago=10)
        through_pits = _lap(conn, sid, 3)
        laps = _laps(conn, sid)
        assert laps[unmeasured]["setup_fp"] == ""
        assert (laps[on_a]["setup_fp"], laps[on_a]["setup_changed"]) == (a, 0)
        assert (laps[through_pits]["setup_fp"],
                laps[through_pits]["setup_changed"]) == (b, 1)
        print("  unmeasured, measured, and a lap through a garage stop")
        conn.close()


def test_a_setup_changed_without_saying_so_does_not_inherit_the_name():
    """The mislabel this exists for: pit, load something, forget to say."""
    with temp_db() as path:
        conn = db.connect(path)
        sid = make_session(conn, car=CAR)
        _measure(conn, sid, BASE)
        db.set_session_setup(conn, sid, "baseline")
        first = _lap(conn, sid, 1)
        _measure(conn, sid, STIFF, ago=150)
        unsaid = _lap(conn, sid, 2)
        # And back again: the car is on the baseline's values, so it is the
        # baseline whatever was or was not said.
        _measure(conn, sid, BASE, ago=120)
        back = _lap(conn, sid, 3)
        laps = _laps(conn, sid)
        assert laps[first]["setup_name"] == "baseline"
        assert laps[unsaid]["setup_name"] == "", laps[unsaid]
        assert laps[back]["setup_name"] == "baseline", laps[back]
        print("  stiff-rear laps left blank, baseline recognized on return")
        conn.close()


def test_naming_a_setup_before_loading_it_does_not_name_the_baseline():
    """"I'm loading v2" said with the baseline still on the car.

    Binding the name to what is measured at the moment of the call would
    have given the baseline's values the new name.
    """
    with temp_db() as path:
        conn = db.connect(path)
        sid = make_session(conn, car=CAR)
        _measure(conn, sid, BASE, ago=400)
        baseline = [_lap(conn, sid, n) for n in (1, 2)]
        db.set_session_setup(conn, sid, "claude_v2")
        _measure(conn, sid, STIFF, ago=200)
        v2 = _lap(conn, sid, 3)
        laps = _laps(conn, sid)
        assert all(laps[i]["setup_name"] == "" for i in baseline), laps
        assert laps[v2]["setup_name"] == "claude_v2"
        assert db.get_session(conn, sid)["setup_fp"] == \
            db.setup_fingerprint(CAR, STIFF)
        conn.close()


def test_without_the_app_a_stated_name_is_stamped_as_before():
    with temp_db() as path:
        conn = db.connect(path)
        sid = make_session(conn, car=CAR)
        db.set_session_setup(conn, sid, "v1")
        lap = _lap(conn, sid, 1)
        assert _laps(conn, sid)[lap]["setup_name"] == "v1"
        conn.close()


def test_a_late_call_offers_the_laps_already_on_that_setup():
    srv = _server()
    sid = make_session(srv._conn, car=CAR)
    _measure(srv._conn, sid, BASE, ago=900)
    baseline = [_lap(srv._conn, sid, n) for n in (1, 2)]
    _measure(srv._conn, sid, STIFF, ago=500)
    on_new = [_lap(srv._conn, sid, n) for n in (3, 4)]

    out = _call(srv.set_session_setup, setup_name="claude_v1", session_id=sid)
    assert out["measured_setup_now"] == db.setup_fingerprint(CAR, STIFF)
    assert out["unlabelled_laps_on_the_setup_measured_now"] == on_new, out
    # Offered, not applied.
    labels = _laps(srv._conn, sid)
    assert all(labels[i]["setup_name"] == "" for i in baseline + on_new)
    print("  late call lists the stiff-rear laps and labels nothing")


def test_label_laps_refuses_ids_driven_on_two_setups():
    srv = _server()
    sid = make_session(srv._conn, car=CAR)
    _measure(srv._conn, sid, BASE, ago=900)
    a = [_lap(srv._conn, sid, n) for n in (1, 2)]
    _measure(srv._conn, sid, STIFF, ago=500)
    b = [_lap(srv._conn, sid, n) for n in (3, 4)]

    out = _call(srv.label_laps, lap_ids=f"{a[0]}-{b[-1]}",
                setup_name="claude_v1", session_id=sid)
    assert out["ok"] is False, out
    assert sorted(out["laps_by_measured_setup"].values()) == \
        sorted([a, b]), out
    assert all(l["setup_name"] == "" for l in _laps(srv._conn, sid).values())
    print("  a range across the garage stop is refused whole")


def test_label_laps_keeps_a_name_to_the_setup_it_was_measured_on():
    srv = _server()
    sid = make_session(srv._conn, car=CAR)
    _measure(srv._conn, sid, BASE, ago=900)
    a = [_lap(srv._conn, sid, n) for n in (1, 2)]
    _measure(srv._conn, sid, STIFF, ago=500)
    b = [_lap(srv._conn, sid, n) for n in (3, 4)]

    first = _call(srv.label_laps, lap_ids=",".join(map(str, a)),
                  setup_name="baseline", session_id=sid)
    assert first["laps_labelled"] == 2, first
    out = _call(srv.label_laps, lap_ids=",".join(map(str, b)),
                setup_name="baseline", session_id=sid)
    assert out["measured_on_a_different_setup"] == b, out
    assert out["ok"] is False, out
    labels = _laps(srv._conn, sid)
    assert [labels[i]["setup_name"] for i in b] == ["", ""]


def test_label_laps_on_unmeasured_laps_works_as_before():
    srv = _server()
    sid = make_session(srv._conn, car=CAR)
    ids = [_lap(srv._conn, sid, n) for n in (1, 2)]
    out = _call(srv.label_laps, lap_ids=",".join(map(str, ids)),
                setup_name="v1", session_id=sid)
    assert out["laps_labelled"] == 2, out


def test_a_comparison_on_one_measured_setup_says_nothing_changed():
    srv = _server()
    sid = make_session(srv._conn, car=CAR)
    _measure(srv._conn, sid, BASE, ago=900)
    db.set_session_setup(srv._conn, sid, "baseline")
    a = [db.get_lap(srv._conn, _lap(srv._conn, sid, n)) for n in (1, 2)]
    # "Loaded" the new setup; AC ignored it, and the values never moved.
    db.set_session_setup(srv._conn, sid, "claude_v1")
    b = [db.get_lap(srv._conn, _lap(srv._conn, sid, n)) for n in (3, 4)]
    out = srv._measured_setups(a, b)
    assert out.get("same_measured_setup") is True, out
    assert "did not reach the car" in out["measured_setup_warning"]
    print("  baseline vs claude_v1 on identical values is flagged")


def test_a_comparison_says_what_actually_changed():
    srv = _server()
    sid = make_session(srv._conn, car=CAR)
    _measure(srv._conn, sid, BASE, ago=900)
    a = [db.get_lap(srv._conn, _lap(srv._conn, sid, n)) for n in (1, 2)]
    _measure(srv._conn, sid, {**STIFF, "FUEL": 40.0}, ago=500)
    b = [db.get_lap(srv._conn, _lap(srv._conn, sid, n)) for n in (3, 4)]
    out = srv._measured_setups(a, b)
    assert "same_measured_setup" not in out, out
    assert out["setup_changes"] == [
        {"entry": "ARB_REAR", "baseline": 2.0, "candidate": 5.0}], out


def test_a_side_on_two_setups_is_reported():
    srv = _server()
    sid = make_session(srv._conn, car=CAR)
    _measure(srv._conn, sid, BASE, ago=900)
    a = [db.get_lap(srv._conn, _lap(srv._conn, sid, 1))]
    _measure(srv._conn, sid, STIFF, ago=500)
    a.append(db.get_lap(srv._conn, _lap(srv._conn, sid, 2)))
    unmeasured = {**a[0], "setup_fp": ""}
    out = srv._measured_setups(a, [unmeasured])
    assert "setup_changes" not in out and "same_measured_setup" not in out
    assert "no measured setup" in out["measured_setup_note"], out
    out = srv._measured_setups(a, a[1:])
    assert "more than one measured setup" in out["measured_setup_note"], out


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
