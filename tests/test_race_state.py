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
                sessionTimeLeft=-1.0)
    base.update(g)
    return server._race_state(SimpleNamespace(**base))


def test_a_race_reports_lap_of_laps_and_position():
    s = _state()
    assert s == {"session_type": "race", "position": 7, "current_lap": 15,
                 "race_laps": 16, "laps_remaining": 2,
                 "session_time_left_s": None}, s


def test_a_finished_race_says_so():
    s = _state(completedLaps=16)
    assert s["laps_remaining"] == 0 and s["finished"] is True, s


def test_a_timed_session_has_no_lap_count_but_a_clock():
    s = _state(session=0, numberOfLaps=0, sessionTimeLeft=754_000.0,
               position=0)
    assert s["race_laps"] is None and s["laps_remaining"] is None, s
    assert s["session_time_left_s"] == 754, s
    assert s["position"] is None and s["session_type"] == "practice", s


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
