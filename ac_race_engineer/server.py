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
    # Both names, because neither is reliably the folder on disk: at some
    # circuits track_config is the whole id ("mugello_osrw") and at others
    # the bare layout ("layout_moto"), where the folder is `track`.
    out = setups.identify_setup(
        AC_DOCS_DIR, session["car"],
        session.get("track_config") or session["track"], values,
        track_folder=session["track"])
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
    tyre pressures and core temps, and a corner-by-corner breakdown.

    Each corner carries entry_pos, apex_pos, exit_pos, min speed, brake and
    throttle points, peak_lat_g, peak_steer_norm (a fraction of full lock,
    not degrees), and a front/rear slip balance (positive = understeer
    tendency, negative = oversteer).

    turn_sign groups corners by direction: corners sharing a sign turn the
    same way, which is what correlating tyre temperatures needs. It is NOT
    left/right -- AC does not document which sign is which, so do not
    describe a turn_sign of 1 as a right-hander.

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


def _live_fuel_facts() -> tuple[dict, str | None]:
    """Tank size and fuel rate straight from shared memory, or why not.

    Only consulted when the session does not already know them. AC is
    running whenever this tool is useful, and these two are static-page
    constants, so reading them here beats waiting for a value that only
    arrives if the in-game app happens to be installed.
    """
    try:
        from .sim_info import fuel_facts
        return fuel_facts(), None
    except Exception as e:  # AC not running, or not Windows
        return {}, f"{type(e).__name__}: {e}"


def _fuel_basis(sid: int, session: dict) -> tuple[dict, dict]:
    """Tank capacity and fuel rate for a session, and where each came from.

    Two things answer "how big is the tank" and they are not interchangeable.
    The car's FUEL setup range is a property of the car: it lives in
    setup_ranges, and is re-derivable from there with AC shut. static.maxFuel
    is a property of the session and is gone the moment the game is.

    sessions.max_fuel_liters is the second of those -- its schema comment says
    so -- so only a value that actually came off the static page is written
    there. Storing a range-derived tank in it bought nothing, because the
    ranges are still in the same database on the next call, and cost the
    provenance: the number came back out later labelled "the game's maxFuel",
    a source it had never been near.
    """
    tank, tank_source = db.tank_liters(_conn, session["car"])
    if tank is None and session.get("max_fuel_liters"):
        tank, tank_source = session["max_fuel_liters"], "the game's maxFuel"
    rate = session.get("fuel_rate")
    rate_source = "the game's fuel-usage assist" if rate is not None else None

    why = None
    if tank is None or rate is None:
        live, why = _live_fuel_facts()
        live_tank = live.get("max_fuel_liters")
        if tank is None and live_tank:
            tank, tank_source = live_tank, "the game's maxFuel"
        if rate is None and live.get("fuel_rate") is not None:
            rate = live["fuel_rate"]
            rate_source = "the game's fuel-usage assist"
        if live.get("fuel_rate_unusable") is not None:
            why = (f"the game reported a fuel-usage multiplier of "
                   f"{live['fuel_rate_unusable']}, which cannot be one")
        try:
            # Remember what shared memory said, so the next call does not
            # depend on AC still being open and a later plan cannot silently
            # disagree with this one. What shared memory said and nothing
            # else: `tank` may be the car's setup range, which this column
            # does not mean and setup_ranges can answer again anyway.
            db.set_fuel_basis(_conn, sid, max_fuel_liters=live_tank,
                              fuel_rate=rate)
        except ValueError:
            pass

    return ({"tank": tank, "rate": rate},
            {"tank_source": tank_source, "rate_source": rate_source,
             "why_not": why})


@mcp.tool()
def fuel_plan(race_laps: int, stops: int = 1,
              session_id: int | None = None) -> str:
    """Fuel for a race distance, and the stint splits.

    Uses the track length and the car's own km_per_liter, both read from
    the game by the in-game app -- so it works at any circuit without
    anyone looking a number up. Tank size comes from the car's setup
    ranges, or from shared memory when the game reports the fuel entry as
    one the driver cannot change.

    stops is a floor, not an instruction: a distance needing three stops is
    planned with three however few were asked for, and says so. Reports
    whether a stop is forced by fuel, which is worth knowing before
    planning around a no-stop run that was never available."""
    # The MCP argument is annotated int, which is a description of the tool
    # rather than a guarantee about the call. A negative or fractional stop
    # count used to fall through into a silently coerced plan.
    for name, value in (("race_laps", race_laps), ("stops", stops)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or value != int(value):
            return _j({"error": f"{name} must be a whole number, "
                                f"got {value!r}"})
    race_laps, stops = int(race_laps), int(stops)
    if race_laps < 1:
        return _j({"error": "race_laps must be at least 1"})
    if stops < 0:
        return _j({"error": f"stops cannot be negative, got {stops}"})

    sid = _active_session(session_id)
    if sid is None:
        return _j({"error": "no active session; pass session_id explicitly"})
    session = db.get_session(_conn, sid)
    if not session:
        return _j({"error": f"no session with id {sid}"})

    basis, source = _fuel_basis(sid, session)
    out = analysis.fuel_plan(
        race_laps, session.get("km_per_liter"),
        session.get("track_length_m"), tank_liters=basis["tank"],
        stops=stops, fuel_rate=basis["rate"])
    out["session_id"] = sid
    out["track"] = session["track"]
    out["car"] = session["car"]
    if basis["tank"] is not None:
        out["tank_liters_source"] = source["tank_source"]
    else:
        out["what_to_check"] = (
            "Tank capacity is unknown, so no stop verdict was possible. It "
            "comes from the car's FUEL setup entry once the in-game app has "
            "seen the setup menu, or from shared memory while AC is running"
            + (f". Reading it here failed: {source['why_not']}"
               if source["why_not"] else "."))
    if basis["rate"] is not None:
        out["fuel_rate_source"] = source["rate_source"]
    elif source["why_not"] and basis["tank"] is not None:
        # Only when it is not already the headline above: one cause,
        # reported once.
        out["fuel_rate_not_read"] = source["why_not"]
    return _j(out)


@mcp.tool()
def compare_runs(baseline_laps: str, candidate_laps: str,
                 include_invalid: bool = False) -> str:
    """Did a setup change actually do anything, given lap-to-lap noise?

    Pass two comma-separated lists of lap ids -- the laps before a change
    and the laps after. Every metric is judged against the driver's own
    within-run spread rather than a fixed threshold, so a small run says
    "within noise" instead of inviting a conclusion it cannot support.

    The metrics are one family, corrected together: read
    `p_value_adjusted` against 0.05 rather than each metric's own
    `p_value`. Up to eight are tested, not always eight -- a channel the
    laps never carried is not tested and is not counted, and suspension is
    the one that does not arrive from shared memory alone -- so read
    `tests_in_family` in the payload for the number the correction was
    actually made at. Only a metric may be described as having moved.

    `corner_leads` is EXPLORATORY and asserts nothing. Those p-values are
    uncorrected, roughly 5% of unchanged corner tests come back "worth a
    look", and 77.6% of fifteen-corner runs with nothing changed at all
    carried at least one. A lead says where to look when a metric moved;
    it is not a finding on its own, and must not be reported as one.

    Read `resolution` alongside any "within noise" answer: it is the
    smallest change those laps could have detected about half the time. A
    large resolution means the run was too short, not that the change did
    nothing, and `power` says how likely the change actually measured was
    to be caught at all. "Suggestive" is the third answer: the metric
    cleared 95% on its own but not the corrected level, which means run it
    again with more laps rather than that nothing happened.

    Both sides must be the same car at the same track and layout, and laps
    that were invalidated or abandoned before the line are dropped and
    named. include_invalid=True keeps them, which is almost always wrong:
    an abandoned lap's time is wall-clock elapsed, not a lap time.

    What a "within noise" is worth, measured against this driver's own
    spread and unaffected by how many corners the circuit has. A 2.2-point
    front load transfer change -- a rear anti-roll bar, against 0.3 of
    lap-to-lap noise -- is caught 29% of the time at two laps a side and
    97% at three. A 500ms lap gain, against a 0.25s spread, is caught 3%
    at two laps, 11% at three, 39% at five and 77% at eight. So two laps a
    side is a real test on a quiet channel and no test at all on lap time,
    three is the realistic minimum, and a null lap-time answer from under
    five or six laps is not evidence the change did nothing. That is not
    this tool being strict, it is what a lap time is worth as an
    instrument."""
    def ids(raw):
        out = []
        for part in str(raw).split(","):
            # Strip before the emptiness check, not after: a trailing
            # separator leaves a part that is whitespace rather than empty
            # ("1,2,\n" from a wrapped list), which is truthy, so it reached
            # int() and raised "not a lap id: '\n'" on a list that was
            # perfectly readable. Stripping here also makes removing spaces
            # up front redundant -- and removing them was itself wrong, since
            # it turned "1 2" into the lap id 12 rather than saying it could
            # not read it.
            part = part.strip()
            if part:
                try:
                    out.append(int(part))
                except ValueError:
                    raise ValueError(f"not a lap id: {part!r}")
        return out

    try:
        a_ids, b_ids = ids(baseline_laps), ids(candidate_laps)
    except ValueError as e:
        return _j({"error": str(e)})
    if not a_ids or not b_ids:
        return _j({"error": "need lap ids on both sides"})

    def fetch(lap_ids):
        out = []
        for lid in lap_ids:
            lap = db.get_lap(_conn, lid)
            if not lap:
                raise ValueError(f"no lap with id {lid}")
            out.append(lap)
        return out

    try:
        base_laps, cand_laps = fetch(a_ids), fetch(b_ids)
    except ValueError as e:
        return _j({"error": str(e)})

    # Track, layout and car, before anything is measured. norm_pos 0.6 is a
    # different corner at a different circuit and a different one again on
    # another layout of the same one, and lap times from two cars are not on
    # the same scale at all -- a Suzuka MX-5 run against a Mugello F4 run
    # came back "moved", -37.3s, stated as confidently as a real result. The
    # payload never said which track or car either side was, so there was
    # nothing in it to notice the mistake by.
    def where(lap):
        cfg = lap.get("track_config") or ""
        return (lap.get("track") or "?") + (f"/{cfg}" if cfg else "")

    def what(laps):
        return sorted({f"{where(l)} in {l.get('car') or '?'}" for l in laps})

    a_what, b_what = what(base_laps), what(cand_laps)
    if len(set(a_what + b_what)) > 1:
        return _j({"error": "these laps are not comparable: baseline is "
                            f"{', '.join(a_what)}, candidate is "
                            f"{', '.join(b_what)}. Track position, lap time "
                            f"and tyre behavior only mean the same thing "
                            f"within one car at one layout."})

    # Invalid and abandoned laps, dropped by name. Both flags were sitting
    # in the row unread: one off-track lap in a 3v3 with a true 500ms gain
    # turned it into "within noise", change -1620ms; one lap abandoned
    # before the line -- whose stored lap_time_ms is wall-clock elapsed, not
    # a lap time -- turned it into "within noise", change +17630ms.
    dropped = []

    def usable(lap, side):
        why = []
        if not lap.get("complete", 1):
            why.append("abandoned before the line, so its time is elapsed "
                       "wall clock rather than a lap time")
        if not lap.get("valid"):
            why.append("invalidated -- off track or a cut")
        if not why:
            return True
        dropped.append({"lap_id": lap["id"], "side": side,
                        "lap_number": lap.get("lap_number"),
                        "reason": "; ".join(why)})
        return False

    def loaded(laps, side):
        """Usable laps paired with their samples, in one pass.

        Separate from summarising because the corner-detection threshold has
        to be computed across every lap in the comparison before any single
        lap can be summarised.
        """
        out = []
        for lap in laps:
            if not include_invalid and not usable(lap, side):
                continue
            samples = db.get_samples(_conn, lap["id"])
            if not samples:
                dropped.append({"lap_id": lap["id"], "side": side,
                                "lap_number": lap.get("lap_number"),
                                "reason": "no telemetry samples stored"})
                continue
            out.append((lap, samples))
        return out

    base_loaded = loaded(base_laps, "baseline")
    cand_loaded = loaded(cand_laps, "candidate")

    # One bar for every lap on both sides, fixed before anything is
    # measured against it. Detecting corners per lap made the threshold a
    # property of how hard each lap was driven, so the harder ones quietly
    # dropped their lightest corners -- and a corner found on only one side
    # is not compared at all. Deriving it from both sides together is what
    # makes it a property of the comparison rather than of either run, so
    # neither side can move the bar the other is judged against.
    ref = analysis.lat_g_reference([s for _, s in base_loaded + cand_loaded])

    def summaries(pairs):
        # No `side` parameter, unlike loaded() above: every reason a lap can
        # be dropped is decided there, so by here the side a lap came from
        # no longer changes anything. Carrying it for symmetry would be a
        # parameter a reader has to check the body to find unused.
        out = []
        for lap, samples in pairs:
            s = analysis.lap_summary(lap, samples, ref)
            # Front load transfer lives in the suspension block and is the
            # quietest channel we have, so it is worth the extra query --
            # it resolves changes lap time cannot see.
            susp = db.get_suspension_samples(_conn, lap["session_id"],
                                             lap["lap_number"] - 1)
            if susp:
                compact = suspension.compact(suspension.summarise(susp))
                if compact:
                    s["suspension"] = compact
            out.append(s)
        return out

    base = summaries(base_loaded)
    cand = summaries(cand_loaded)
    if not base or not cand:
        return _j({"error": "no usable laps left on "
                            + ("both sides" if not base and not cand else
                               "the baseline side" if not base else
                               "the candidate side")
                            + " after dropping invalid and abandoned laps",
                   "excluded_laps": dropped})

    out = analysis.compare_runs(base, cand)
    if ref:
        # Reported because it decides which corners exist. A comparison
        # whose corner list looks wrong is answerable now: this is the bar
        # every lap was held to.
        out["corner_detection"] = {
            "lat_g_reference": round(ref, 3),
            "note": "one lateral-g reference across every lap on both "
                    "sides, so corner membership does not depend on how "
                    "hard an individual lap was driven",
        }
    out["track"] = where(base_laps[0])
    out["car"] = base_laps[0].get("car")
    if dropped:
        out["excluded_laps"] = dropped
    if include_invalid:
        out["warning"] = ("include_invalid=True: invalid and abandoned laps "
                          "were kept, and an abandoned lap's time is elapsed "
                          "wall clock, not a lap time")
    out["baseline_setups"] = sorted({s.get("setup") or "" for s in base})
    out["candidate_setups"] = sorted({s.get("setup") or "" for s in cand})
    return _j(out)


@mcp.tool()
def delta_by_position(lap_id_a: int, lap_id_b: int,
                      segments: int = 20) -> str:
    """Where lap_b gained or lost time against lap_a, along the whole track.

    The delta trace every telemetry tool shows: cumulative time differenced
    by track position. Unlike compare_laps this covers the ground between
    corners, so time lost on a straight, on an exit, or in a fast sweeper
    no corner detector flagged still shows up somewhere.

    Read gain_ms per segment -- that is where the gap opened. The
    cumulative figure only tells you it exists.

    `segments` is how many rows the track is divided into, up to 200 (the
    resolution of the underlying position grid); a larger request comes back
    clamped, with segments_requested saying what was asked for."""
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
        # display carries units, display_multiplier and show_clicks_mode so
        # the report can say what each written number reads as on the setup
        # screen. Ride height stored as 20 is 20 clicks, not 20mm, and a
        # report that doesn't say so is how a wrong setup looks right.
        return _j(setups.write_setup(
            AC_DOCS_DIR, RANGES_DIR, car, track, name, values, base_setup,
            game_ranges=db.setup_ranges(_conn, car),
            display=db.setup_display(_conn, car)))
    except (ValueError, FileNotFoundError) as e:
        return _j({"error": str(e)})


def main():
    mcp.run()


if __name__ == "__main__":
    main()
