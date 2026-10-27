"""The tools a client without a shell or a filesystem works a session with.

session_check at the start, wait_for_lap in the live loop, lap_trace in
place of reading the database, and the notes and handoff files. Called
through a real client session, as in test_mcp_protocol.
"""

import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import get, run_module  # noqa: E402
from test_mcp_protocol import (  # noqa: E402
    _client, _server, _session_with_laps, _wire)


def _calls(*calls):
    """Each (name, arguments) through one client: (isError, payload)."""
    async def run():
        async with _client() as (session, _):
            return [_wire(await session.call_tool(n, a)) for n, a in calls]
    out = []
    for r in asyncio.run(run()):
        text = r["content"][0]["text"]
        out.append((bool(r.get("isError")),
                    json.loads(text) if text.startswith(("{", "[")) else text))
    return out


def test_session_check_says_what_is_missing_before_the_first_lap():
    s = _server()
    sid, ids = _session_with_laps(3)
    s._bridge.app_seen_at = None
    (_, before), = _calls(("session_check", {}))
    get(s._bridge.port, "/status")
    (failed, after), = _calls(("session_check", {}))

    assert not failed, after
    assert after["session"]["id"] == sid, after
    # The laps get slower one by one, so the first is the best.
    assert after["session"]["best_clean_lap_id"] == ids[0], after
    assert any("set_session_setup" in t for t in after["to_do"]), after
    assert before["app"]["connected"] is False, before["app"]
    assert any("in-game app" in t for t in before["to_do"]), before
    assert after["app"]["connected"] is True, after["app"]
    assert 0 <= after["app"]["last_seen_s"] < 5, after["app"]
    assert not any("in-game app" in t for t in after["to_do"]), after
    assert after["storage"]["laps"] >= len(ids), after["storage"]


def test_wait_for_lap_returns_the_next_lap_and_blocks_nothing_else():
    s = _server()
    _, ids = _session_with_laps(2)
    last = ids[-1]

    def store_a_lap_soon():
        time.sleep(1.5)
        with s._conn_lock:
            _session_with_laps(1)

    async def run():
        async with _client() as (session, _):
            waiting = asyncio.ensure_future(session.call_tool(
                "wait_for_lap", {"after_lap_id": last, "timeout_s": 20}))
            await asyncio.sleep(0.3)
            t0 = time.monotonic()
            other = await session.call_tool("list_sessions", {})
            other_took = time.monotonic() - t0
            assert not waiting.done(), "returned before any lap landed"
            threading.Thread(target=store_a_lap_soon).start()
            card = await waiting
            timed_out = await session.call_tool(
                "wait_for_lap", {"after_lap_id": 10**9, "timeout_s": 1})
            return _wire(other), other_took, _wire(card), _wire(timed_out)

    other, other_took, card, timed_out = asyncio.run(run())
    assert not other.get("isError") and other_took < 5, other_took
    card = json.loads(card["content"][0]["text"])
    assert card["new_lap"] is True and card["lap_id"] > last, card
    assert card["usable_for_timing"] is True, card
    assert card["best_clean_lap_id"] is not None, card
    assert json.loads(timed_out["content"][0]["text"])["new_lap"] is False


def test_lap_trace_is_a_bounded_window_of_named_channels():
    _, ids = _session_with_laps(1)
    (ok, window), (_, whole), (refused, _) = _calls(
        ("lap_trace", {"lap_id": ids[0], "channels": ["speed_kmh", "gear"],
                       "from_m": 1000, "to_m": 1500, "step_m": 50}),
        ("lap_trace", {"lap_id": ids[0], "step_m": 1}),
        ("lap_trace", {"lap_id": ids[0], "channels": ["password"]}))

    assert not ok, window
    ms = [r["m"] for r in window["rows"]]
    assert 9 <= len(ms) <= 11 and 1000 <= ms[0] and ms[-1] <= 1500, ms
    assert set(window["rows"][0]) == {"m", "t_s", "speed_kmh", "gear"}
    assert window["from_where"] and window["to_where"], window
    # 1 m over a 5 km lap is far past the row cap, so the step coarsens.
    assert len(whole["rows"]) <= 300 and whole["step_m"] > 1, whole["step_m"]
    assert whole["step_m_requested"] == 1, whole
    assert refused, "an unknown channel was accepted"


def test_notes_stay_in_their_folders_and_keep_what_they_replace():
    s = _server()
    _session_with_laps(1)
    saved = s.NOTES_DIR, s.EXPORTS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        s.NOTES_DIR, s.EXPORTS_DIR = root / "notes", root / "exports"
        try:
            results = _calls(
                ("save_notes", {"kind": "driver", "markdown": "# v1"}),
                ("save_notes", {"kind": "driver", "markdown": "# v2"}),
                ("save_notes", {"kind": "car", "markdown": "# car"}),
                ("read_notes", {}),
                ("save_handoff", {"markdown": "# one", "name": "week"}),
                ("save_handoff", {"markdown": "# two", "name": "week.md"}),
                ("save_notes", {"kind": "car", "car": "../outside",
                                "markdown": "x"}),
                ("save_handoff", {"markdown": "x", "name": "../evil"}),
                ("read_notes", {"car": "..\\outside"}))
        finally:
            s.NOTES_DIR, s.EXPORTS_DIR = saved
        files = sorted(p.relative_to(root).as_posix()
                       for p in root.rglob("*") if p.is_file())

    first, second, car, read, one, two, *refusals = results
    assert "backup" not in first[1] and "backup" in second[1], second
    assert read[1]["driver"]["markdown"] == "# v2", read
    assert read[1]["car_notes"]["markdown"] == "# car", read
    assert read[1]["latest_handoff"] is None, read
    assert one[1]["path"].endswith("week.md"), one
    assert two[1]["path"].endswith("week-2.md"), two
    assert all(failed for failed, _ in refusals), refusals
    assert files == sorted([
        "exports/week.md", "exports/week-2.md", "notes/driver.md",
        f"notes/{Path(car[1]['path']).name}",
        f"notes/{Path(second[1]['backup']).name}"]), files


def test_a_game_without_a_feature_says_so_rather_than_guessing():
    from types import SimpleNamespace
    from assetto_mcp import db, sims
    s = _server()
    sims.GAMES["bare"] = SimpleNamespace(TITLE="Bare Racer", CAPABILITIES={})
    sid = db.create_session(s._conn, sim="bare", car="c", track="t",
                            track_config="", tyre_compound="", air_temp=20,
                            road_temp=25)
    active, s._active_session = s._active_session, lambda *_: sid
    try:
        for name, args, cap in (
                ("write_setup", ("c", "t", "x", {"PRESSURE_LF": 25}),
                 "setup_files"),
                ("list_setups", ("c", "t"), "setup_files"),
                ("send_driver_message", ("box this lap",), "messages")):
            out = json.loads(getattr(s, name)(*args))
            assert out["unsupported"] == cap, (name, out)
            assert out["error"].startswith("Bare Racer does not provide"), out
    finally:
        s._active_session = active
        del sims.GAMES["bare"]


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
