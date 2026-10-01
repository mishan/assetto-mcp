"""Where the driver is in the session: position, lap N of M.

At Road Atlanta a "fuel for two laps" call came at the start of the last
lap of a 16-lap race, when two laps of fuel was plenty. The game reports
the race distance and the position; these pin how they are read.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402


def _state(**g):
    import os
    os.environ.setdefault("ASSETTO_MCP_NO_AUTOSTART", "1")
    os.environ.setdefault("ASSETTO_MCP_BRIDGE_PORT", "0")
    from assetto_mcp import server
    base = dict(session=2, position=7, completedLaps=14, numberOfLaps=16,
                sessionTimeLeft=-1.0, status=2, flag=0)
    base.update(g)
    return server._race_state(SimpleNamespace(**base))


def test_a_race_reports_lap_of_laps_and_position():
    s = _state()
    assert s == {"session_type": "race", "position": 7, "current_lap": 15,
                 "race_laps": 16, "laps_remaining": 2,
                 "session_time_left_s": None}, s


def test_a_finished_race_says_so():
    s = _state(completedLaps=16, flag=5)
    assert s["laps_remaining"] == 0 and s["finished"] is True, s
    # Not "lap 17 of 16".
    assert s["current_lap"] == 16 and s["race_laps"] == 16, s


def test_a_lapped_car_has_one_lap_left_once_the_flag_is_out():
    # A lap down on lap 15 of 16: the leader has taken the flag, so this
    # crossing is the driver's last, though the lap count says two.
    s = _state(completedLaps=14, flag=5)
    assert s["laps_remaining"] == 1 and s["checkered_flag"] is True, s
    assert "finished" not in s, s


def test_no_flag_means_no_flag_key():
    assert "checkered_flag" not in _state(), _state()


def test_a_timed_session_has_no_lap_count_but_a_clock():
    s = _state(session=0, numberOfLaps=0, sessionTimeLeft=754_000.0,
               position=0)
    assert s["race_laps"] is None and s["laps_remaining"] is None, s
    assert s["session_time_left_s"] == 754, s
    assert s["position"] is None and s["session_type"] == "practice", s


def test_the_menu_and_replays_report_no_lap_or_position():
    # The graphics page keeps the last session's numbers after the driver
    # leaves for the menu; they must not read as the race in progress.
    for status in (0, 1):
        s = _state(status=status, completedLaps=8)
        assert s["session_type"] == "race", s
        assert all(s[k] is None for k in ("position", "current_lap",
                                          "race_laps", "laps_remaining",
                                          "session_time_left_s")), s
        assert "finished" not in s, s


def test_a_paused_race_still_reports():
    assert _state(status=3)["laps_remaining"] == 2


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
