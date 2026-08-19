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

from . import analysis, db, setups, suspension
from .collector import Collector

AC_DOCS_DIR = Path(os.environ.get(
    "AC_DOCS_DIR", Path.home() / "Documents" / "Assetto Corsa"))
DATA_DIR = Path(os.environ.get(
    "AC_ENGINEER_DATA", Path.home() / ".ac-race-engineer"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
RANGES_DIR = DATA_DIR / "ranges"
RANGES_DIR.mkdir(exist_ok=True)

mcp = FastMCP("ac-race-engineer")
DB_PATH = DATA_DIR / "telemetry.db"
_conn = db.connect(DB_PATH)


def _sim_factory():
    from .sim_info import SimInfo
    return SimInfo()


_collector = Collector(DB_PATH, _sim_factory)

from .bridge import Bridge  # noqa: E402

BRIDGE_PORT = int(os.environ.get("AC_ENGINEER_BRIDGE_PORT", "9666"))
_bridge = Bridge(DB_PATH, _collector, BRIDGE_PORT)
_bridge.start()


def _j(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


# --- recording ---------------------------------------------------------


@mcp.tool()
def start_recording() -> str:
    """Start recording telemetry from the running Assetto Corsa session.
    Laps are stored automatically as they complete. Idempotent."""
    _collector.start()
    return _j({"status": _collector.status, "error": _collector.last_error})


def _active_session(explicit: int | None = None) -> int | None:
    """The session tools should read and write, across instances.

    Delegates to the bridge so every entry point -- MCP tools, the in-game
    app's notes, rival telemetry -- agrees on one answer. Resolving from
    _collector.session_id here instead is how tools ended up reporting "no
    active session" for a session this same process's bridge was actively
    writing to, because the app runs one server instance per client surface
    and only one of them is the one recording.
    """
    if explicit is not None:
        return explicit
    return _bridge.active_session_id()


@mcp.tool()
def recording_status() -> str:
    """Check collector state: whether it's recording, which session,
    how many laps stored so far, and any error."""
    snap = _bridge.status_snapshot()
    out = {
        "running": _collector.running,
        "status": _collector.status,
        "session_id": _collector.session_id,
        "active_session_id": snap["session_id"],
        "recording_elsewhere": snap["by_other"],
        "laps_recorded": _collector.laps_recorded,
        "error": _collector.last_error,
        "setup_name": (db.session_setup(_conn, snap["session_id"]) or None
                       if snap["session_id"] else None),
    }
    migrations = db.MIGRATION_LOG.pop(str(DB_PATH), None)
    if migrations:
        out["database_upgraded"] = migrations
    return _j(out)


@mcp.tool()
def stop_recording() -> str:
    """Stop the telemetry collector."""
    _collector.stop()
    return _j({"status": "stopped",
               "laps_recorded": _collector.laps_recorded})


# --- live state --------------------------------------------------------

AC_STATUS = {0: "off", 1: "replay", 2: "live", 3: "pause"}


@mcp.tool()
def live_snapshot() -> str:
    """Current instantaneous state from shared memory: car, track, session
    status, tyre pressures/temps right now, fuel, last/best lap times.
    Useful to confirm AC is running and see conditions."""
    from .sim_info import SimInfo
    sim = SimInfo()
    try:
        p, g, s = sim.physics, sim.graphics, sim.static
        payload = {
            "car": s.carModel,
            "track": s.track,
            "track_config": s.trackConfiguration,
            # Mapped, not indexed: a partial or corrupt shared-memory read
            # gives an out-of-range status, and an IndexError here takes the
            # whole tool call down -- on exactly the call the driver makes
            # to find out whether shared memory is readable at all.
            "status": AC_STATUS.get(g.status, f"unknown ({g.status})"),
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
        }
    finally:
        # Release the ctypes views before closing the mapping.
        p = g = s = None
        sim.close()
    return _j(payload)


# --- stored telemetry --------------------------------------------------


@mcp.tool()
def list_sessions() -> str:
    """List recorded sessions with car, track, lap count and best time."""
    return _j(db.list_sessions(_conn))


@mcp.tool()
def list_rivals(session_id: int | None = None, limit: int = 20) -> str:
    """Opponents seen in a session, best-lap order, with how much of their
    telemetry we captured.

    Requires the in-game Lua app: AC's shared memory is ego-only, so
    opponent data can only reach the server via the bridge."""
    sid = _active_session(session_id)
    if sid is None:
        return _j({"error": "no active session; pass session_id explicitly"})
    rivals = db.list_rivals(_conn, sid, limit=limit)
    if not rivals:
        return _j({"session_id": sid, "rivals": [],
                   "note": "No opponent data. The in-game app must be "
                           "running and updated to push rival telemetry."})
    for r in rivals:
        laps = _well_covered_rival_laps(sid, r["car_index"])
        # A full practice session is 25+ laps per car; listing every one for
        # every rival ran to ~55KB of context. The engineer only ever wants
        # the quick ones, so report the count and show the best few.
        r["captured_lap_count"] = len(laps)
        r["best_captured_laps"] = [
            {"lap_count": l["lap_count"], "lap_time_ms": l["lap_time_ms"],
             "samples": l["n"]}
            for l in laps[:3]
        ]
    return _j({"session_id": sid, "rivals": rivals})


def _well_covered_rival_laps(sid: int, car_index: int) -> list[dict]:
    """Rival laps we saw enough of to compare against, quickest first.

    Ordered by recorded lap time where we have one. A lap with no time sorts
    last: without it there is no way to know whether it was a flyer or an
    in-lap, and comparing against an unknown-pace lap is worse than useless.
    """
    times = db.rival_lap_times(_conn, sid, car_index)
    laps = [dict(l, lap_time_ms=times.get(l["lap_count"]))
            for l in db.rival_lap_counts(_conn, sid, car_index)
            if l["n"] >= 20 and (l["hi"] - l["lo"]) > 0.8]
    laps.sort(key=lambda l: (l["lap_time_ms"] is None,
                             l["lap_time_ms"] or 0))
    return laps


@mcp.tool()
def compare_to_rival(car_index: int, lap_id: int,
                     rival_lap_count: int | None = None,
                     session_id: int | None = None) -> str:
    """Compare one of your laps against a rival's, by track position.

    Shows where they carry more speed, and -- if the server transmits remote
    pedal inputs -- where they brake and get back on power. Use list_rivals
    to find car_index and which of their laps were captured.

    rival_lap_count defaults to their quickest captured lap."""
    sid = _active_session(session_id)
    if sid is None:
        return _j({"error": "no active session; pass session_id explicitly"})

    lap = db.get_lap(_conn, lap_id)
    if not lap:
        return _j({"error": f"no lap with id {lap_id}"})
    # Both laps must be from the same session, or this silently compares two
    # different circuits corner by corner and reports it with a straight face.
    if lap["session_id"] != sid:
        return _j({"error": f"lap {lap_id} belongs to session "
                            f"{lap['session_id']} ({lap['track']}), not "
                            f"session {sid}. Rival telemetry is only "
                            f"comparable within the session it was captured "
                            f"in."})

    laps = _well_covered_rival_laps(sid, car_index)
    if not laps:
        return _j({"error": f"no well-covered laps captured for car "
                            f"{car_index} in session {sid}"})

    chosen = None
    if rival_lap_count is None:
        chosen = laps[0]        # quickest, or longest-observed if untimed
        rival_lap_count = chosen["lap_count"]
    else:
        chosen = next((l for l in laps
                       if l["lap_count"] == rival_lap_count), None)
        if chosen is None:
            return _j({"error": f"lap_count {rival_lap_count} was not "
                                f"captured well enough for car {car_index}",
                       "available": [l["lap_count"] for l in laps]})

    rival_samples = db.get_rival_lap_samples(
        _conn, sid, car_index, rival_lap_count)
    result = analysis.compare_to_rival(
        db.get_samples(_conn, lap_id), rival_samples)
    result["my_lap"] = {"id": lap_id,
                        "time_ms": lap["lap_time_ms"],
                        "setup": lap.get("setup_name") or None}
    result["rival"] = {"car_index": car_index,
                       "lap_count": rival_lap_count,
                       "lap_time_ms": chosen["lap_time_ms"]}
    if chosen["lap_time_ms"] is None:
        result["rival"]["caution"] = (
            "No lap time recorded for this rival lap, so its pace is "
            "unknown -- it could be an in-lap. Treat speed deltas as "
            "indicative only.")
    return _j(result)


@mcp.tool()
def identify_setup(session_id: int | None = None) -> str:
    """Work out which saved setup is currently on the car.

    Compares the live values the in-game app reports from the setup menu
    against every saved setup for this car and track. The comparison is on
    content across every adjustable entry, so setups differing only in ARB
    or camber are told apart -- which shared memory alone cannot do, since
    it exposes brake bias and fuel and nothing else.

    Returns a single match, or the candidates when several are identical.
    Also reports the car's setup legality, since AC will accept a setup and
    then ignore values outside the legal range."""
    sid = _active_session(session_id)
    if sid is None:
        return _j({"error": "no active session; pass session_id explicitly"})
    session = db.get_session(_conn, sid)
    if not session:
        return _j({"error": f"no session with id {sid}"})

    values = db.setup_values(_conn, sid)
    out = setups.identify_setup(
        AC_DOCS_DIR, session["car"],
        session.get("track_config") or session["track"], values)
    out["session_id"] = sid
    out["live_values"] = len(values)
    state = db.setup_state(_conn, sid)
    if state:
        out["setup_state"] = state["state"]
        if state.get("reason"):
            out["setup_state_reason"] = state["reason"]
    if not values:
        out["what_to_check"] = (
            "The in-game app posts setup values from ac.getSetupSpinners(). "
            "No values means the app isn't running, or is an older version.")
    return _j(out)


@mcp.tool()
def setup_ranges(car: str | None = None) -> str:
    """The car's legal setup ranges, as the game itself reports them.

    Each entry carries min, max and step in the units the setup *file*
    uses, plus display_multiplier and show_clicks_mode -- the two
    conventions that make a stored value differ from what the setup screen
    shows. Camber stored as tenths of a degree and ride height stored as a
    click index are both explained by these fields.

    Populated by the in-game app; no need to unpack data.acd."""
    if car is None:
        sid = _active_session(None)
        session = db.get_session(_conn, sid) if sid else None
        if not session:
            return _j({"error": "no active session; pass car explicitly"})
        car = session["car"]
    rows = db.setup_range_details(_conn, car)
    if not rows:
        return _j({"car": car, "ranges": [],
                   "note": "nothing stored yet -- the in-game app posts "
                           "these once it sees the setup menu."})
    return _j({"car": car, "count": len(rows), "ranges": rows})


@mcp.tool()
def set_session_setup(setup_name: str, session_id: int | None = None,
                      fill_unattributed: bool = True) -> str:
    """Record the setup now on the car, so laps are attributed to it.

    AC's shared memory does not expose the setup loaded in the garage, so
    this has to be stated explicitly -- call it whenever the driver says
    they've loaded a different setup, including mid-session after a pit stop.

    Laps completed from now on are tagged with this name. Laps already
    stored under a *different* setup keep it -- relabelling those would
    destroy the A/B comparison this exists to enable. Laps stored with no
    setup at all are filled in, since a blank is a gap rather than a
    competing claim, and telling us after a run is the normal case.

    Pass fill_unattributed=False to leave even the blanks alone."""
    sid = _active_session(session_id)
    if sid is None:
        return _j({"error": "no active session; pass session_id explicitly"})
    if not db.set_session_setup(_conn, sid, setup_name):
        return _j({"error": f"no session with id {sid}"})

    laps = db.list_laps(_conn, sid, limit=500)
    blank = [l for l in laps if not (l.get("setup_name") or "")]
    labelled_now = 0
    if fill_unattributed and blank:
        labelled_now = db.label_unattributed_laps(_conn, sid, setup_name)

    kept = [l for l in laps
            if (l.get("setup_name") or "") not in ("", setup_name)]
    out = {"ok": True, "session_id": sid, "setup_name": setup_name,
           "applies_to": "laps completed from now on",
           "laps_already_stored": len(laps),
           "laps_labelled_now": labelled_now}
    if kept:
        out["left_alone"] = sorted({l["setup_name"] for l in kept})
        out["note"] = (f"{len(kept)} lap(s) already carry a different setup "
                       f"and were not touched.")
    elif labelled_now:
        out["note"] = (f"{labelled_now} previously unattributed lap(s) in "
                       f"this session are now labelled {setup_name}.")
    return _j(out)


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
    negative = oversteer tendency).

    Includes a few suspension headlines when the in-game app captured them;
    call suspension_report for the full damper histograms and ride height."""
    lap = db.get_lap(_conn, lap_id)
    if not lap:
        return _j({"error": f"no lap with id {lap_id}"})
    out = analysis.lap_summary(lap, db.get_samples(_conn, lap_id))

    # A pointer, not a replacement: lap_summary has a ~1KB budget and the
    # full suspension report is an order of magnitude bigger.
    lap_count = lap["lap_number"] - 1
    susp_samples = db.get_suspension_samples(_conn, lap["session_id"],
                                             lap_count)
    if susp_samples:
        compact = suspension.compact(suspension.summarise(susp_samples))
        if compact:
            out["suspension"] = compact
    return _j(out)


@mcp.tool()
def suspension_report(lap_id: int | None = None,
                      lap_count: int | None = None,
                      session_id: int | None = None) -> str:
    """Damper histograms, ride height and roll balance for one lap.

    Requires the in-game Lua app: stock shared memory exposes no suspension
    travel, no wheel load and no ride height at all.

    Reports which tier captured the data. Render-rate sampling is fine for
    ride height and load transfer but aliases damper velocity, so damper
    histograms from that tier are labelled as body motion rather than
    valving. A CSP physics worker (333Hz) gives real damper numbers -- see
    suspension_capture_status for whether it is running.

    Pass a lap_id from list_laps, or a raw lap_count if the lap was not
    stored (an out-lap, say)."""
    sid = _active_session(session_id)
    if lap_id is not None:
        lap = db.get_lap(_conn, lap_id)
        if not lap:
            return _j({"error": f"no lap with id {lap_id}"})
        sid = lap["session_id"]
        # laps.lap_number counts from 1; suspension rows are stamped with
        # AC's completed-lap count, which is one behind during the lap.
        lap_count = lap["lap_number"] - 1
    if sid is None:
        return _j({"error": "no active session; pass session_id explicitly"})
    if lap_count is None:
        return _j({"error": "pass either lap_id or lap_count"})

    samples = db.get_suspension_samples(_conn, sid, lap_count)
    if not samples:
        captured = db.suspension_lap_counts(_conn, sid)
        return _j({"error": f"no suspension data for lap_count {lap_count} "
                            f"in session {sid}",
                   "captured_laps": [
                       {"lap_count": r["lap_count"], "source": r["source"],
                        "samples": r["n"]} for r in captured[-10:]],
                   "hint": ("If this list is empty the in-game app either "
                            "isn't running or predates suspension capture.")})

    out = suspension.summarise(samples)
    out["session_id"] = sid
    out["lap_count"] = lap_count
    if lap_id is not None:
        out["lap_id"] = lap_id
    return _j(out)


@mcp.tool()
def suspension_capture_status() -> str:
    """Whether suspension data is arriving, and at what fidelity.

    Call this when suspension_report says there is no data, or when damper
    numbers look implausible. Explains the difference between the two
    capture tiers and what to do about it."""
    sid = _active_session()
    out = {"session_id": sid}
    if sid is None:
        out["error"] = "no active session"
        return _j(out)

    laps = db.suspension_lap_counts(_conn, sid)
    by_source: dict[str, int] = {}
    for r in laps:
        by_source[r["source"]] = by_source.get(r["source"], 0) + r["n"]
    out["samples_by_source"] = by_source
    out["laps_captured"] = len({r["lap_count"] for r in laps})

    if not laps:
        out["status"] = "no suspension data in this session"
        out["what_to_check"] = [
            "Is the Race Engineer app enabled in the in-game apps sidebar?",
            "Is it the current version? Suspension capture was added after "
            "the first release -- re-run install-windows.bat to update it.",
            "Does the app's status window show a bridge connection?",
        ]
    elif "worker" in by_source:
        out["status"] = "physics worker running: real damper velocity (333Hz)"
    else:
        out["status"] = ("render-rate capture only: ride height and load "
                         "transfer are trustworthy, damper histograms show "
                         "body motion rather than valving")
        out["why"] = (
            "The app tries to start a CSP physics worker for 333Hz damper "
            "data and falls back to render-rate sampling when it can't. "
            "The usual reason is multiplayer: CSP does not allow scripts on "
            "the physics thread in an online session, because that thread "
            "decides what the car does. Damper histograms are therefore a "
            "single-player feature. Ride height, wheel loads and roll "
            "balance never used the worker and are unaffected online.")
        out["to_get_damper_data"] = (
            "Run the same car and track in a solo practice session. The "
            "app's status window shows the tier: worker, online, "
            "or render-rate fallback.")
    return _j(out)


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


@mcp.tool()
def delta_by_position(lap_id_a: int, lap_id_b: int,
                      segments: int = 20) -> str:
    """Where lap_b gained or lost time against lap_a, along the whole track.

    The delta trace every telemetry tool shows: cumulative time differenced
    by track position. Unlike compare_laps this covers the ground between
    corners, so time lost on a straight, on an exit, or in a fast sweeper
    no corner detector flagged still shows up somewhere.

    Read gain_ms per segment -- that is where the gap opened. The
    cumulative figure only tells you it exists."""
    a, b = db.get_lap(_conn, lap_id_a), db.get_lap(_conn, lap_id_b)
    if not a or not b:
        return _j({"error": "one or both lap ids not found"})
    # Layout matters as much as track: 'mugello' and 'mugello_osrw' are the
    # same folder and different circuits, so norm_pos 0.6 is not the same
    # corner in both. Comparing them produces a delta trace that looks
    # entirely plausible and means nothing.
    if (a.get("track"), a.get("track_config")) != \
            (b.get("track"), b.get("track_config")):
        def _name(l):
            cfg = l.get("track_config") or "(default layout)"
            return f"{l.get('track')}/{cfg}"
        return _j({"error": f"different track layouts: {_name(a)} vs "
                            f"{_name(b)}; positions are not comparable"})
    return _j(analysis.delta_by_position(
        a, db.get_samples(_conn, lap_id_a),
        b, db.get_samples(_conn, lap_id_b), segments=segments))


# --- in-game app bridge ------------------------------------------------


@mcp.tool()
def get_driver_notes(session_id: int | None = None, limit: int = 50,
                     all_sessions: bool = False) -> str:
    """Complaint tags the driver pressed in-game while driving (understeer,
    oversteer, braking, traction, note). Each has a spline position (0..1)
    directly comparable to corner apex_pos values from lap_summary, plus the
    lap_count when pressed (current lap = lap_count + 1). Correlate these
    with telemetry to know which corners the driver is unhappy with.

    Defaults to the active session; pass all_sessions=True for everything."""
    sid = None if all_sessions else _active_session(session_id)
    notes = db.list_notes(_conn, sid, limit)
    orphans = db.count_orphan_notes(_conn)
    out = {"session_id": sid, "notes": notes}
    if orphans and not all_sessions:
        # Notes pressed while nothing was recording. Stored deliberately
        # rather than guessed into a session, but they need saying out loud
        # or they are lost in exactly the way this was meant to stop.
        out["orphaned_notes"] = (
            f"{orphans} note(s) were pressed while no session was "
            f"recording and are not attached to one. Call with "
            f"all_sessions=True to see them.")
    return _j(out)


@mcp.tool()
def send_driver_message(text: str) -> str:
    """Show a short message on the driver's in-game Race Engineer overlay
    (e.g. 'claude_v2 saved: softer front ARB, +0.5psi rears - pit and load
    it'). Keep it to a sentence or two; the driver is driving. The message
    stays up until dismissed, and is replaced by any newer message."""
    if not _bridge or _bridge.error:
        return _j({"error": _bridge.error if _bridge else "bridge not running"})
    return _j({"ok": True, "message_id": _bridge.set_message(text),
               "note": "displayed until driver dismisses it"})


@mcp.tool()
def bridge_status() -> str:
    """Health of the HTTP bridge the in-game app connects to."""
    return _j({"port": _bridge.port if _bridge else None,
               "error": _bridge.error if _bridge else "not running",
               "pending_message": _bridge.get_message() if _bridge else None})


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
            AC_DOCS_DIR, RANGES_DIR, car, track, name, values, base_setup,
            game_ranges=db.setup_ranges(_conn, car)))
    except (ValueError, FileNotFoundError) as e:
        return _j({"error": str(e)})


def main():
    mcp.run()


if __name__ == "__main__":
    main()
