"""MCP server: exposes AC telemetry and setup tools over stdio.

Run on the machine running Assetto Corsa:

    python -m ac_race_engineer.server

Environment:
    AC_DOCS_DIR   AC documents dir (default: ~/Documents/Assetto Corsa)
    AC_ENGINEER_DATA  data dir for DB + ranges (default: ~/.ac-race-engineer)
"""

import json
import os
from pathlib import Path

try:  # mcp SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP

from . import analysis, db, setups
from .collector import Collector

AC_DOCS_DIR = Path(os.environ.get(
    "AC_DOCS_DIR", Path.home() / "Documents" / "Assetto Corsa"))
DATA_DIR = Path(os.environ.get(
    "AC_ENGINEER_DATA", Path.home() / ".ac-race-engineer"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
RANGES_DIR = DATA_DIR / "ranges"
RANGES_DIR.mkdir(exist_ok=True)

mcp = FastMCP("ac-race-engineer")
_conn = db.connect(DATA_DIR / "telemetry.db")


def _sim_factory():
    from .sim_info import SimInfo
    return SimInfo()


_collector = Collector(_conn, _sim_factory)


def _j(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


# --- recording ---------------------------------------------------------


@mcp.tool()
def start_recording() -> str:
    """Start recording telemetry from the running Assetto Corsa session.
    Laps are stored automatically as they complete. Idempotent."""
    _collector.start()
    return _j({"status": _collector.status, "error": _collector.last_error})


@mcp.tool()
def recording_status() -> str:
    """Check collector state: whether it's recording, which session,
    how many laps stored so far, and any error."""
    return _j({
        "running": _collector.running,
        "status": _collector.status,
        "session_id": _collector.session_id,
        "laps_recorded": _collector.laps_recorded,
        "error": _collector.last_error,
    })


@mcp.tool()
def stop_recording() -> str:
    """Stop the telemetry collector."""
    _collector.stop()
    return _j({"status": "stopped",
               "laps_recorded": _collector.laps_recorded})


# --- live state --------------------------------------------------------


@mcp.tool()
def live_snapshot() -> str:
    """Current instantaneous state from shared memory: car, track, session
    status, tyre pressures/temps right now, fuel, last/best lap times.
    Useful to confirm AC is running and see conditions."""
    from .sim_info import SimInfo
    sim = SimInfo()
    try:
        p, g, s = sim.physics, sim.graphics, sim.static
        return _j({
            "car": s.carModel,
            "track": s.track,
            "track_config": s.trackConfiguration,
            "status": ["off", "replay", "live", "pause"][g.status],
            "tyre_compound": g.tyreCompound,
            "air_temp": round(p.airTemp, 1),
            "road_temp": round(p.roadTemp, 1),
            "fuel_l": round(p.fuel, 1),
            "speed_kmh": round(p.speedKmh, 1),
            "last_lap": g.lastTime,
            "best_lap": g.bestTime,
            "completed_laps": g.completedLaps,
            "tyre_pressures_psi": {
                w: round(p.wheelsPressure[i], 1)
                for i, w in enumerate(("fl", "fr", "rl", "rr"))},
            "tyre_core_temps_c": {
                w: round(p.tyreCoreTemperature[i], 1)
                for i, w in enumerate(("fl", "fr", "rl", "rr"))},
        })
    finally:
        sim.close()


# --- stored telemetry --------------------------------------------------


@mcp.tool()
def list_sessions() -> str:
    """List recorded sessions with car, track, lap count and best time."""
    return _j(db.list_sessions(_conn))


@mcp.tool()
def list_laps(session_id: int | None = None, limit: int = 20) -> str:
    """List recorded laps (most recent first), optionally for one session.
    Returns lap ids to use with lap_summary / compare_laps."""
    return _j(db.list_laps(_conn, session_id, limit))


@mcp.tool()
def lap_summary(lap_id: int) -> str:
    """Engineer's summary of one lap: lap time, throttle/brake/coast split,
    tyre pressures and core temps, per-corner min speed, brake points,
    and a front/rear slip balance metric (positive = understeer tendency,
    negative = oversteer tendency)."""
    lap = db.get_lap(_conn, lap_id)
    if not lap:
        return _j({"error": f"no lap with id {lap_id}"})
    return _j(analysis.lap_summary(lap, db.get_samples(_conn, lap_id)))


@mcp.tool()
def compare_laps(lap_id_a: int, lap_id_b: int) -> str:
    """Corner-by-corner comparison of two laps on the same track: min speed
    deltas, brake point deltas, and slip balance changes. Use to evaluate
    whether a setup change actually helped."""
    a, b = db.get_lap(_conn, lap_id_a), db.get_lap(_conn, lap_id_b)
    if not a or not b:
        return _j({"error": "one or both lap ids not found"})
    return _j(analysis.compare_laps(
        a, db.get_samples(_conn, lap_id_a),
        b, db.get_samples(_conn, lap_id_b)))


# --- setups ------------------------------------------------------------


@mcp.tool()
def list_setups(car: str, track: str) -> str:
    """List saved setup names for a car/track combo. Use the internal car
    folder name (e.g. 'ks_mazda_mx5_cup') and track folder name."""
    return _j(setups.list_setups(AC_DOCS_DIR, car, track))


@mcp.tool()
def read_setup(car: str, track: str, name: str) -> str:
    """Read a saved setup file. Returns each setting section and its value
    (e.g. PRESSURE_LF: 26)."""
    try:
        return _j(setups.read_setup(AC_DOCS_DIR, car, track, name))
    except FileNotFoundError as e:
        return _j({"error": str(e)})


@mcp.tool()
def write_setup(car: str, track: str, name: str, values_json: str,
                base_setup: str | None = None) -> str:
    """Write a new setup file the user can load from the in-game setup menu.

    values_json: JSON object of {SECTION: number}, e.g.
      {"PRESSURE_LF": 25, "PRESSURE_RF": 25, "ARB_FRONT": 4}
    base_setup: optional existing setup name to start from; unspecified
      sections are carried over unchanged.

    If a ranges file exists for the car, values are clamped and snapped to
    the car's legal min/max/step; otherwise they're written as-is with a
    warning (AC silently ignores out-of-range values)."""
    try:
        values = json.loads(values_json)
    except json.JSONDecodeError as e:
        return _j({"error": f"values_json is not valid JSON: {e}"})
    if not isinstance(values, dict):
        return _j({"error": "values_json must be a JSON object"})
    try:
        return _j(setups.write_setup(
            AC_DOCS_DIR, RANGES_DIR, car, track, name, values, base_setup))
    except (ValueError, FileNotFoundError) as e:
        return _j({"error": str(e)})


def main():
    mcp.run()


if __name__ == "__main__":
    main()
