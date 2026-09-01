"""SQLite storage for telemetry sessions, laps, and samples.

Kept deliberately boring: one writer (the collector thread), many readers
(MCP tool calls). WAL mode makes that safe.

Schema changes go through _migrate(), keyed on PRAGMA user_version. Adding a
column to an existing table with CREATE TABLE IF NOT EXISTS silently does
nothing on a database that already exists, so every column added after the
first release needs an explicit ALTER here.
"""

import sqlite3
import time
from pathlib import Path

from . import analysis

# Bump when the schema changes and add a matching step in _migrate().
SCHEMA_VERSION = 10

# How long a recorder's claim survives without a heartbeat before another
# instance may take it. Generously more than HEARTBEAT_SECONDS in
# collector.py, because the cost of the two errors is not symmetric: taking
# over too eagerly gives two collectors writing the same laps, while taking
# over late costs a few seconds of a session nobody was recording anyway.
RECORDER_STALE_SECONDS = 15.0

# How long a session may go without a heartbeat and still be called live.
# Same asymmetry, other direction: this decides whether the overlay says
# "recording" and whether a driver's note has somewhere to go, so it wants
# slack for a stalled disk or a paused process.
SESSION_STALE_SECONDS = 30.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    started_at REAL NOT NULL,
    car TEXT NOT NULL,
    track TEXT NOT NULL,
    track_config TEXT NOT NULL DEFAULT '',
    tyre_compound TEXT NOT NULL DEFAULT '',
    air_temp REAL,
    road_temp REAL,
    setup_name TEXT NOT NULL DEFAULT '',
    -- Enough to compute fuel per lap without being told the track. Length
    -- comes from the AI spline; km_per_liter from the car's own
    -- fuel_cons.ini, read through CSP so an encrypted data.acd is no
    -- obstacle. Nullable: both arrive from the in-game app, and a missing
    -- basis must read as unknown rather than as a plausible default.
    track_length_m REAL,
    km_per_liter REAL,
    -- Tank capacity and AC's fuel-usage multiplier, both from the static
    -- shared memory page. The multiplier is the one a league changes: at
    -- 50% or 200% every fuel figure moves by that factor, and nothing read
    -- it, so a plan for a 200% session was quietly half the fuel needed.
    -- 1.0 is 100%; 0 is a real setting and not the same as unknown, which
    -- is why every read of these goes through IS NULL rather than falsy.
    max_fuel_liters REAL,
    fuel_rate REAL,
    -- When the collector recording this session last said it was alive.
    -- started_at never moves, so it cannot answer "is this still going?" --
    -- and the process that has to answer it is usually not the process
    -- recording. Every other instance shares this file and nothing else.
    -- NULL means a session written before v10, where the only evidence
    -- available is the last stored lap.
    last_seen_at REAL
);

-- setup_name is per-lap, not per-session: the tuning loop changes setup in
-- the pits and keeps driving, so stamping it on the session would relabel
-- laps that were driven on the previous setup.
CREATE TABLE IF NOT EXISTS laps (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    lap_number INTEGER NOT NULL,
    lap_time_ms INTEGER NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1,
    completed_at REAL NOT NULL,
    setup_name TEXT NOT NULL DEFAULT '',
    -- 0 for a lap that never reached the finish line: a crash, a reset to
    -- the pits, or recording stopped mid-lap. Those samples used to be
    -- discarded, which meant the single most interesting lap of a session
    -- -- the one that ended in the barrier -- was the only one guaranteed
    -- not to be recorded.
    complete INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS samples (
    lap_id INTEGER NOT NULL REFERENCES laps(id),
    t_ms INTEGER NOT NULL,          -- ms since lap start
    norm_pos REAL NOT NULL,         -- 0..1 along track spline
    speed_kmh REAL NOT NULL,
    gas REAL NOT NULL,
    brake REAL NOT NULL,
    steer REAL NOT NULL,
    gear INTEGER NOT NULL,
    rpm INTEGER NOT NULL,
    acc_lat REAL NOT NULL,
    acc_lon REAL NOT NULL,
    slip_fl REAL NOT NULL, slip_fr REAL NOT NULL,
    slip_rl REAL NOT NULL, slip_rr REAL NOT NULL,
    press_fl REAL NOT NULL, press_fr REAL NOT NULL,
    press_rl REAL NOT NULL, press_rr REAL NOT NULL,
    core_fl REAL NOT NULL, core_fr REAL NOT NULL,
    core_rl REAL NOT NULL, core_rr REAL NOT NULL,
    ride_f REAL NOT NULL, ride_r REAL NOT NULL,
    tyres_out INTEGER NOT NULL,
    -- World position. norm_pos says where the car is ALONG the lap; these
    -- say where it is across it, which is the whole of what a driving line
    -- is and the one thing every earlier analysis was blind to. Nullable
    -- on purpose: laps recorded before v8 have no position and never will,
    -- and 0,0,0 would be a claim that the car was at the track origin.
    pos_x REAL, pos_y REAL, pos_z REAL,
    -- Body attitude. roll settles the body-control arguments that ride
    -- height and anti-roll bars have only let us reason about indirectly.
    heading REAL, pitch REAL, roll REAL,
    -- What the electronics are actually doing, rather than what the setup
    -- screen says they are set to. A TC level means nothing without knowing
    -- whether it ever intervenes.
    tc_active REAL, abs_active REAL,
    -- Tyre wear, per corner. Separate from carDamage, which is bodywork --
    -- a distinction worth keeping straight, because wear is the one that
    -- happens on every lap of every session and underpins any stint or
    -- strategy question.
    wear_fl REAL, wear_fr REAL, wear_rl REAL, wear_rr REAL,
    -- Bodywork damage, summed across AC's five zones. Zero for the whole
    -- session when the server has damage disabled, which is the usual case
    -- here -- so this confirms contact when it is on and says nothing when
    -- it is off. It is not a substitute for detecting a wall from the speed
    -- trace, which works either way.
    damage REAL
);

CREATE INDEX IF NOT EXISTS idx_samples_lap ON samples(lap_id, t_ms);
CREATE INDEX IF NOT EXISTS idx_laps_session ON laps(session_id);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    session_id INTEGER,               -- NULL if collector wasn't recording
    lap_count INTEGER NOT NULL,       -- completed laps when pressed (current lap = lap_count+1)
    spline REAL NOT NULL,             -- 0..1, comparable to samples.norm_pos / corner apex_pos
    tag TEXT NOT NULL,
    speed_kmh REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_session ON notes(session_id);

-- Opponent telemetry, pushed by the in-game Lua app (the server cannot see
-- other cars: AC's shared memory is ego-only).
--
-- Online, remote cars are network-interpolated. Position, spline and speed
-- are dependable; gas/brake/gear depend on what the server transmits and the
-- CSP build exposes, so they are nullable and their liveness is measured at
-- query time rather than assumed.
CREATE TABLE IF NOT EXISTS rival_samples (
    session_id INTEGER NOT NULL,
    car_index INTEGER NOT NULL,
    lap_count INTEGER NOT NULL,     -- segments samples into laps
    spline REAL NOT NULL,           -- 0..1, comparable to samples.norm_pos
    speed_kmh REAL NOT NULL,
    gear INTEGER,
    gas REAL,
    brake REAL,
    created_at REAL NOT NULL
);
-- UNIQUE, not just an index: the Lua app posts over HTTP and will resend a
-- batch whose response it never saw. Without this a retry double-counts
-- every sample and skews the spline-bucket means it feeds.
CREATE UNIQUE INDEX IF NOT EXISTS idx_rival_samples
    ON rival_samples(session_id, car_index, lap_count, spline);

-- Rival lap times, recorded when a car's completed-lap count advances.
-- rival_drivers.last_lap_ms is overwritten every batch, so without this
-- there is no way to tell which of a rival's stored laps was their quick
-- one -- and comparing against an unknown-pace lap is worse than useless.
-- Suspension telemetry, pushed by the in-game Lua app. Stock shared memory
-- exposes none of this: no suspension travel, no wheel load, no ride height.
--
-- `source` records which tier produced the row and is not decoration. 'app'
-- is render-rate (60-144Hz), fine for ride height and load transfer but
-- aliased for damper velocity; 'worker' is a CSP physics worker at 333Hz,
-- where damper numbers are real. Mixing them silently would let a histogram
-- built from body motion be presented as damper valving.
CREATE TABLE IF NOT EXISTS suspension_samples (
    session_id INTEGER NOT NULL,
    lap_count INTEGER NOT NULL,     -- completed laps when sampled
    t_ms INTEGER NOT NULL,          -- AC physics clock, not wall time
    spline REAL NOT NULL,           -- 0..1, comparable to samples.norm_pos
    source TEXT NOT NULL DEFAULT 'app',
    brake REAL,                     -- used to infer the compression sign
    speed_kmh REAL,
    travel_fl REAL, travel_fr REAL, travel_rl REAL, travel_rr REAL,
    load_fl REAL, load_fr REAL, load_rl REAL, load_rr REAL,
    ride_f REAL, ride_r REAL,
    plank_wear REAL,
    PRIMARY KEY (session_id, lap_count, t_ms, source)
);
CREATE INDEX IF NOT EXISTS idx_suspension_lap
    ON suspension_samples(session_id, lap_count);

-- What the game itself says about the setup menu, pushed by the Lua app
-- from ac.getSetupSpinners().
--
-- This replaces unpacking data.acd by hand: the ranges are per car, come
-- from the running game, and arrive for encrypted paid mods too. It also
-- ends the units guesswork -- display_multiplier and show_clicks_mode are
-- the two conventions we previously reverse-engineered by comparing a
-- saved file against what the setup screen displayed.
CREATE TABLE IF NOT EXISTS setup_ranges (
    car TEXT NOT NULL,
    name TEXT NOT NULL,             -- matches the setup INI section name
    label TEXT NOT NULL DEFAULT '',
    min_value REAL NOT NULL,
    max_value REAL NOT NULL,
    step REAL NOT NULL DEFAULT 1,
    display_multiplier REAL,        -- stored value * this = what the UI shows
    show_clicks_mode INTEGER,       -- non-zero: the UI shows a click index
    units TEXT NOT NULL DEFAULT '',
    read_only INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (car, name)
);

-- The values actually on the car, so a loaded setup can be identified by
-- content rather than guessed at. Per session, replaced when they change.
CREATE TABLE IF NOT EXISTS setup_values (
    session_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, name)
);

CREATE TABLE IF NOT EXISTS setup_state (
    session_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL DEFAULT '',   -- legal / illegal / validating
    reason TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rival_laps (
    session_id INTEGER NOT NULL,
    car_index INTEGER NOT NULL,
    lap_count INTEGER NOT NULL,   -- the lap these samples belong to
    lap_time_ms INTEGER NOT NULL,
    recorded_at REAL NOT NULL,
    PRIMARY KEY (session_id, car_index, lap_count)
);

-- Which process is allowed to record, and whether recording is wanted at
-- all. One row, forever.
--
-- Claude Desktop launches one server per client surface, so several of
-- these processes exist at once and all of them see the same shared memory
-- and the same database file. The bridge port was the only thing they ever
-- contended for; the collector asked for nothing, which was harmless only
-- while a human had to call start_recording to begin. Autostart removed
-- that accident, and two collectors reading one game wrote every lap twice
-- under two session ids -- duplicates that survive into compare_runs as a
-- sample with zero deviation from itself, which is the one input that makes
-- a t-test certain about nothing.
--
-- `enabled` is separate from the claim and deliberately shared: stopping
-- recording is an instruction about the car, not about whichever server
-- process happened to receive it, and it used to be undone by the next
-- restart.
CREATE TABLE IF NOT EXISTS recorder (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner TEXT NOT NULL DEFAULT '',   -- host/pid/token of the holder
    claimed_at REAL,
    heartbeat_at REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    changed_at REAL
);

CREATE TABLE IF NOT EXISTS rival_drivers (
    session_id INTEGER NOT NULL,
    car_index INTEGER NOT NULL,
    driver_name TEXT NOT NULL DEFAULT '',
    car_model TEXT NOT NULL DEFAULT '',
    best_lap_ms INTEGER,
    last_lap_ms INTEGER,
    lap_count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, car_index)
);
"""

SAMPLE_COLUMNS = [
    "lap_id", "t_ms", "norm_pos", "speed_kmh", "gas", "brake", "steer",
    "gear", "rpm", "acc_lat", "acc_lon",
    "slip_fl", "slip_fr", "slip_rl", "slip_rr",
    "press_fl", "press_fr", "press_rl", "press_rr",
    "core_fl", "core_fr", "core_rl", "core_rr",
    "ride_f", "ride_r", "tyres_out",
    "pos_x", "pos_y", "pos_z",
    "heading", "pitch", "roll",
    "tc_active", "abs_active",
    "wear_fl", "wear_fr", "wear_rl", "wear_rr", "damage",
]

# Sample tuple widths that have ever been correct, newest first. store_lap
# pads a short tuple with NULL, which is right for a caller written against
# an older layout and catastrophic for a tuple that is short because a field
# was dropped in the MIDDLE -- every column after the gap shifts by one, and
# the row stores silently. Measured on a tuple missing `steer`: gear 7000,
# rpm 1, tyres_out 297.63 (a world coordinate), damage NULL, no error.
#
# So the padding applies to widths a real layout actually had, and nothing
# else. 25 is v7 (through tyres_out), 33 adds v8's position/attitude/
# electronics, 38 adds v9's wear and damage. None of these counts lap_id,
# which store_lap prepends.
SAMPLE_WIDTHS = (len(SAMPLE_COLUMNS) - 1, 33, 25)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table: str, col: str, decl: str) -> bool:
    """Add a column if the table exists and doesn't already have it.

    Guarded on the table existing because ALTER on a missing one raises,
    and a raise inside _migrate does more damage than losing that step: it
    aborts every later step too AND leaves user_version un-bumped, so the
    database is stuck below the current schema permanently rather than
    just this once. A database can legitimately be missing a table -- one
    that has only ever held imported rows, or a half-built fixture -- and
    CREATE TABLE IF NOT EXISTS runs straight after this and creates it
    complete, so there is nothing to repair.
    """
    if not _table_exists(conn, table) or col in _columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    return True


def _migrate(conn) -> list[str]:
    """Bring an existing database up to SCHEMA_VERSION. Returns a log.

    Runs before the CREATE TABLE IF NOT EXISTS block, because that block
    cannot add a column to a table that already exists -- the exact way a
    schema change silently no-ops on a real user's database while passing
    every test that starts from an empty file.
    """
    log: list[str] = []
    fresh = not _table_exists(conn, "sessions")
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    if fresh:
        # Nothing to migrate; SCHEMA below creates everything current.
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return log

    if version >= SCHEMA_VERSION:
        return log

    if version < 1:
        # v1: setup attribution moved from sessions to laps.
        if _add_column(conn, "laps", "setup_name",
                       "TEXT NOT NULL DEFAULT ''"):
            # Best available guess for history: whatever the session was
            # last stamped with. Wrong for sessions where the setup changed
            # mid-run, but strictly better than empty, and from here on laps
            # are stamped individually at write time.
            n = conn.execute(
                "UPDATE laps SET setup_name = COALESCE("
                "  (SELECT setup_name FROM sessions"
                "   WHERE sessions.id = laps.session_id), '')").rowcount
            log.append(f"laps.setup_name added; backfilled {n} lap(s) "
                       "from their session")

    if version < 2:
        # v2: re-run the outlier rule over laps stored before it existed.
        # The 10:22 pit-stop "lap" that motivated the rule was still sitting
        # in the database marked valid, still poisoning every best-lap
        # query, because validity was only ever computed at write time.
        flipped = revalidate_outlier_laps(conn)
        if flipped:
            log.append(f"re-checked stored laps: {flipped} gross outlier(s) "
                       "marked invalid (they are still readable)")

    # v3 added suspension_samples, a new table only -- CREATE TABLE IF NOT
    # EXISTS below covers it, so there is no ALTER step here. Recorded so
    # the next person can see the version was accounted for rather than
    # skipped.

    if version < 4:
        # v4: laps.complete, so an abandoned lap can be stored rather than
        # thrown away. Existing rows all reached the line by definition.
        if _add_column(conn, "laps", "complete",
                       "INTEGER NOT NULL DEFAULT 1"):
            log.append("laps.complete added; existing laps marked complete")

    if version < 5:
        # v5: the fuel basis, so liters per lap stops being a hand
        # calculation that has to be redone for every track.
        for col in ("track_length_m", "km_per_liter"):
            if _add_column(conn, "sessions", col, "REAL"):
                log.append(f"sessions.{col} added")

    if version < 6:
        # v6: the rest of the fuel basis. Tank capacity so a car whose FUEL
        # entry the game reports as read-only still has a known tank, and
        # AC's fuel-usage multiplier, which decides whether a stop is needed
        # and which nothing had ever read.
        for col in ("max_fuel_liters", "fuel_rate"):
            if _add_column(conn, "sessions", col, "REAL"):
                log.append(f"sessions.{col} added")

    if version < 7:
        # v7: max_fuel_litres -> max_fuel_liters. AC spells it LITER in the
        # car's own fuel_cons.ini (KM_PER_LITER), so the rest of the project
        # follows the game rather than the author.
        #
        # A rename rather than a fresh column: v6 shipped on a branch that
        # was already run against a real database, so the data is there
        # under the old name and dropping it would lose a tank capacity
        # nobody can re-derive without the game open.
        cols = _columns(conn, "sessions")
        if "max_fuel_litres" in cols and "max_fuel_liters" not in cols:
            conn.execute("ALTER TABLE sessions"
                         " RENAME COLUMN max_fuel_litres TO max_fuel_liters")
            log.append("sessions.max_fuel_litres renamed to max_fuel_liters")

    if version < 8:
        # v8: position, attitude and electronics activity. All of it was
        # already being read from shared memory 25 times a second and
        # discarded -- carCoordinates in particular, which is the only
        # source of lateral position and therefore of a driving line.
        #
        # Nullable, and deliberately not backfilled: there is nothing to
        # backfill from. Every lap recorded before this migration has no
        # position and cannot acquire one, so a reader has to be able to
        # tell "not recorded" from "at the origin".
        for col in ("pos_x", "pos_y", "pos_z",
                    "heading", "pitch", "roll",
                    "tc_active", "abs_active"):
            if _add_column(conn, "samples", col, "REAL"):
                log.append(f"samples.{col} added")

    if version < 9:
        # v9: tyre wear and bodywork damage. Both were mapped in the physics
        # struct from the beginning and never read. Wear is the one that
        # matters day to day -- it changes every lap whatever the server
        # settings, and no stint or pit-strategy question can be answered
        # without it.
        for col in ("wear_fl", "wear_fr", "wear_rl", "wear_rr", "damage"):
            if _add_column(conn, "samples", col, "REAL"):
                log.append(f"samples.{col} added")

    if version < 10:
        # v10: sessions.last_seen_at, and the recorder table created by
        # SCHEMA above. Both exist to answer one question a single process
        # cannot answer for itself -- is another instance recording right
        # now -- which the bridge previously guessed at from started_at and
        # a staleness window, and got wrong in both directions.
        #
        # Deliberately not backfilled from started_at: a session that ended
        # last week would then look like one being heartbeated right now.
        # NULL reads as "no heartbeat was ever recorded for this session",
        # and latest_session() falls back to the lap evidence for those.
        if _add_column(conn, "sessions", "last_seen_at", "REAL"):
            log.append("sessions.last_seen_at added")

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return log


def revalidate_outlier_laps(conn) -> int:
    """Mark already-stored gross outliers invalid. Returns rows changed.

    Only ever flips valid -> invalid, and only for laps that are far slower
    than their own session's reference. Never resurrects a lap someone (or
    the dirty-lap rule) invalidated deliberately.
    """
    flipped = 0
    for srow in conn.execute("SELECT id FROM sessions"):
        sid = srow["id"]
        times = [r["lap_time_ms"] for r in conn.execute(
            "SELECT lap_time_ms FROM laps WHERE session_id = ?", (sid,))]
        ref = analysis.outlier_reference(times)
        if ref is None:
            continue
        for lap in conn.execute(
                "SELECT id, lap_time_ms FROM laps"
                " WHERE session_id = ? AND valid = 1", (sid,)):
            if analysis.lap_is_outlier(lap["lap_time_ms"], ref):
                conn.execute("UPDATE laps SET valid = 0 WHERE id = ?",
                             (lap["id"],))
                flipped += 1
    if flipped:
        conn.commit()
    return flipped


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # store_lap can hold the write lock across a few thousand sample
    # inserts while the bridge is also writing rival batches.
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    migrations = _migrate(conn)
    conn.executescript(SCHEMA)
    if migrations:
        # sqlite3.Connection takes no custom attributes, so park the log
        # here for recording_status to report once.
        MIGRATION_LOG[str(db_path)] = migrations
    return conn


# {db_path: [what the migration did]}, populated on first connect.
MIGRATION_LOG: dict[str, list[str]] = {}


def create_session(conn, *, car, track, track_config, tyre_compound,
                   air_temp, road_temp) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO sessions (started_at, car, track, track_config,"
        " tyre_compound, air_temp, road_temp, last_seen_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (now, car, track, track_config, tyre_compound,
         air_temp, road_temp, now),
    )
    conn.commit()
    return cur.lastrowid


def touch_session(conn, session_id: int) -> None:
    """Say that the collector recording this session is still alive.

    Called on a timer rather than on lap completion, which is the whole
    point: the first lap a session stores is its first FLYING lap, because
    the out-lap has no time and is skipped. Judging liveness from stored
    laps therefore declared a session dead for the length of a garage sit
    plus an out-lap plus a flying lap -- five minutes at Sebring, longer
    at Nordschleife -- during which the overlay read "not recording" and
    every complaint tag the driver pressed was filed against nothing.
    """
    conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?",
                 (time.time(), session_id))
    conn.commit()


# --- the single recorder --------------------------------------------------


def _recorder_row(conn) -> dict:
    r = conn.execute("SELECT * FROM recorder WHERE id = 1").fetchone()
    if r is None:
        conn.execute(
            "INSERT OR IGNORE INTO recorder (id, owner, enabled, changed_at)"
            " VALUES (1, '', 1, ?)", (time.time(),))
        conn.commit()
        r = conn.execute("SELECT * FROM recorder WHERE id = 1").fetchone()
    return dict(r)


def recorder_enabled(conn) -> bool:
    """Whether recording is wanted at all, by any instance."""
    return bool(_recorder_row(conn)["enabled"])


def set_recorder_enabled(conn, on: bool) -> None:
    """Turn recording on or off for every instance, durably.

    stop_recording used to stop one process's thread. With several server
    processes alive and each autostarting a collector, that stopped
    whichever surface the driver happened to be typing into and left the
    others recording -- and the next restart undid it regardless. An
    instruction about the car belongs in the file every instance shares.
    """
    _recorder_row(conn)
    conn.execute("UPDATE recorder SET enabled = ?, changed_at = ?"
                 " WHERE id = 1", (1 if on else 0, time.time()))
    conn.commit()


def claim_recorder(conn, owner: str,
                   stale_after: float = RECORDER_STALE_SECONDS) -> dict:
    """Try to become the one process that records. Never blocks.

    Returns {"held": bool, "owner": str, "heartbeat_age": float | None}.
    `held` is the only thing a caller has to act on; the rest is for saying
    who has it instead, which is the difference between a collector that
    looks broken and one that is correctly standing aside.

    The claim is taken by a single conditional UPDATE, so two processes
    racing for a free slot cannot both win: SQLite serialises the writes and
    the loser's WHERE no longer matches.
    """
    now = time.time()
    _recorder_row(conn)
    with conn:
        conn.execute(
            "UPDATE recorder SET owner = ?, claimed_at = ?, heartbeat_at = ?"
            " WHERE id = 1 AND (owner = '' OR owner = ?"
            "                   OR heartbeat_at IS NULL"
            "                   OR heartbeat_at < ?)",
            (owner, now, now, owner, now - stale_after))
    r = _recorder_row(conn)
    beat = r["heartbeat_at"]
    return {"held": r["owner"] == owner,
            "owner": r["owner"],
            "heartbeat_age": (round(now - beat, 1)
                              if beat is not None else None)}


def renew_recorder(conn, owner: str) -> bool:
    """Re-assert the claim. False means it was taken while we weren't looking.

    A collector that has lost the claim must stop writing rather than carry
    on: the takeover only happens after RECORDER_STALE_SECONDS of silence
    from us, which means something stalled us for that long, and the other
    instance is now recording the same laps.
    """
    with conn:
        cur = conn.execute(
            "UPDATE recorder SET heartbeat_at = ? WHERE id = 1 AND owner = ?",
            (time.time(), owner))
    return cur.rowcount > 0


def release_recorder(conn, owner: str) -> None:
    """Give the claim up so another instance can take it immediately."""
    with conn:
        conn.execute("UPDATE recorder SET owner = '', heartbeat_at = NULL"
                     " WHERE id = 1 AND owner = ?", (owner,))


def get_session(conn, session_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM sessions WHERE id = ?",
                     (session_id,)).fetchone()
    return dict(r) if r else None


def store_setup_snapshot(conn, session_id: int, car: str,
                         spinners: list[dict], state: str = "",
                         reason: str = "") -> dict:
    """Record what the game says about the setup menu right now.

    Ranges are keyed on the car alone -- they are a property of the car,
    not of a session -- while values belong to the session, because that is
    what identifies which setup was loaded for these laps.
    """
    now = time.time()
    ranges, values = 0, 0
    for s in spinners:
        name = s.get("name")
        if not name:
            continue
        lo, hi = s.get("min"), s.get("max")
        if lo is not None and hi is not None:
            conn.execute(
                "INSERT INTO setup_ranges (car, name, label, min_value,"
                " max_value, step, display_multiplier, show_clicks_mode,"
                " units, read_only, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(car, name) DO UPDATE SET"
                " label=excluded.label, min_value=excluded.min_value,"
                " max_value=excluded.max_value, step=excluded.step,"
                " display_multiplier=excluded.display_multiplier,"
                " show_clicks_mode=excluded.show_clicks_mode,"
                " units=excluded.units, read_only=excluded.read_only,"
                " updated_at=excluded.updated_at",
                (car, name, s.get("label", "") or "", float(lo), float(hi),
                 # `or 1` would turn a legitimate step of 0 into 1. Zero is
                 # how a continuous entry reports itself, and rewriting it
                 # as 1 invents a grid the car does not have -- which then
                 # snaps every written value onto it.
                 float(1 if s.get("step") is None else s["step"]),
                 s.get("display_multiplier"), s.get("show_clicks_mode"),
                 s.get("units", "") or "", int(bool(s.get("read_only"))),
                 now))
            ranges += 1
        if s.get("value") is not None:
            conn.execute(
                "INSERT INTO setup_values (session_id, name, value,"
                " updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(session_id, name) DO UPDATE SET"
                " value=excluded.value, updated_at=excluded.updated_at",
                (session_id, name, float(s["value"]), now))
            values += 1
    if state:
        conn.execute(
            "INSERT INTO setup_state (session_id, state, reason, updated_at)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET state=excluded.state,"
            " reason=excluded.reason, updated_at=excluded.updated_at",
            (session_id, state, reason or "", now))
    conn.commit()
    return {"ranges": ranges, "values": values}


def _fuel_number(name: str, value, low: float, high: float,
                 zero_is_absent: bool = False) -> float | None:
    """Coerce a fuel-basis value, or say why it is not one. None stays None.

    The range checks used to live only in the bridge, so this function was
    safe exactly as long as HTTP was the only way in. Called directly with a
    numeric string -- which is what a config file, a test, or another tool
    hands you -- it raised `'>' not supported between instances of 'str' and
    'int'` from a comparison three lines down, naming neither the argument
    nor the caller's mistake.

    zero_is_absent exempts 0 from the range check for the fields where it has
    always meant "not supplied" and the caller drops it. Without it, raising
    the floor off zero would turn that documented no-op into a refusal.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, got {value!r}")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if zero_is_absent and num == 0:
        return num
    if not low <= num <= high:  # NaN fails this too
        raise ValueError(
            f"{name} must be between {low:g} and {high:g}, got {value!r}")
    return num


def set_fuel_basis(conn, session_id: int, track_length_m=None,
                   km_per_liter=None, max_fuel_liters=None,
                   fuel_rate=None) -> bool:
    """Record what fuel per lap can be derived from. Ignores missing values.

    Deliberately partial: the track length may arrive while the car's
    consumption figure does not, and overwriting a known value with None
    would lose it. Absence is not an update.

    fuel_rate is AC's fuel-usage multiplier, 1.0 for 100%. It is the one
    value here where 0 is meaningful -- a session that burns no fuel is a
    setting people use -- so it is tested against None rather than for
    truth, and a 0 is stored.

    The floors come from analysis, which is where fuel_plan checks the same
    three. A lower bound of 0 let through a track half a meter long, a car
    doing half a meter to the liter and a tank of a thousandth of a liter:
    all stored happily, all refused by fuel_plan later, so a session could
    carry a basis that was guaranteed to fail whenever anyone used it. The
    refusal belongs at the write, where the caller who supplied the number is
    still on the stack.
    """
    # Zero is exempt from the floor and dropped below: it has always meant
    # "not supplied" for these three, and turning that into a refusal would
    # reject callers this is not aimed at.
    track_length_m = _fuel_number("track_length_m", track_length_m,
                                  analysis.MIN_TRACK_LENGTH_M, 1_000_000,
                                  zero_is_absent=True)
    km_per_liter = _fuel_number("km_per_liter", km_per_liter,
                                analysis.MIN_KM_PER_LITER, 1000,
                                zero_is_absent=True)
    max_fuel_liters = _fuel_number("max_fuel_liters", max_fuel_liters,
                                   analysis.MIN_TANK_LITERS, 100_000,
                                   zero_is_absent=True)
    fuel_rate = _fuel_number("fuel_rate", fuel_rate, 0, analysis.MAX_FUEL_RATE)

    sets, args = [], []
    for col, value in (("track_length_m", track_length_m),
                       ("km_per_liter", km_per_liter),
                       ("max_fuel_liters", max_fuel_liters),
                       ("fuel_rate", fuel_rate)):
        if value is None:
            continue
        # 0 is absent for a length or a consumption figure -- both are
        # nonsense at zero -- and present for the multiplier.
        if not value and col != "fuel_rate":
            continue
        sets.append(f"{col} = ?")
        args.append(value)
    if not sets:
        return False
    args.append(session_id)
    cur = conn.execute(
        f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", args)
    conn.commit()
    return cur.rowcount > 0


def tank_liters(conn, car: str) -> tuple[float | None, str]:
    """Tank capacity for a car, and where it was read from.

    Deliberately not setup_ranges(), which excludes read_only entries
    because writing one is silently ignored. That is right for writing a
    setup and wrong for asking how big the tank is: a car whose fuel load
    the game will not let you change still has a tank, and filtering it out
    took tank_liters, stop_required_for_fuel and the note out of the fuel
    plan altogether -- while leaving a two-stint plan behind, which reads as
    a stop that was reasoned about.
    """
    row = conn.execute(
        "SELECT max_value, read_only FROM setup_ranges"
        " WHERE car = ? AND name = 'FUEL'", (car,)).fetchone()
    if row and row["max_value"]:
        return (row["max_value"],
                "the car's FUEL setup range"
                + (" (which the game reports as read-only)"
                   if row["read_only"] else ""))
    return None, "not known"


def setup_ranges(conn, car: str) -> dict:
    """{SECTION: (min, max, step)} as the game reports it, or {} if unknown.

    Read-only entries are excluded: AC reports them so the setup screen can
    grey them out, and writing one is silently ignored. Offering them as
    writable would produce a clamped, snapped, confidently-reported value
    that the car never sees.
    """
    rows = conn.execute(
        "SELECT name, min_value, max_value, step FROM setup_ranges"
        " WHERE car = ? AND read_only = 0", (car,))
    # `step or 1` would turn a legitimate 0 -- a continuous entry -- into a
    # grid of 1 and snap every value onto it.
    return {r["name"]: (r["min_value"], r["max_value"],
                        1.0 if r["step"] is None else r["step"])
            for r in rows}


def setup_display(conn, car: str) -> dict:
    """{SECTION: {units, display_multiplier, show_clicks_mode}} for a car.

    Kept separate from setup_ranges because these do not clamp anything --
    they say how a stored number appears on the setup screen. A value stored
    as 20 can read as 20 clicks or -2.0 degrees, and a report that says
    "wrote 20" without saying which is how a setup that looks fine and isn't
    gets written.
    """
    rows = conn.execute(
        "SELECT name, units, display_multiplier, show_clicks_mode"
        " FROM setup_ranges WHERE car = ?", (car,))
    return {r["name"]: {"units": r["units"] or "",
                        "display_multiplier": r["display_multiplier"],
                        "show_clicks_mode": r["show_clicks_mode"]}
            for r in rows}


def setup_range_details(conn, car: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM setup_ranges WHERE car = ? ORDER BY name", (car,))
    return [dict(r) for r in rows]


def setup_values(conn, session_id: int) -> dict:
    rows = conn.execute(
        "SELECT name, value FROM setup_values WHERE session_id = ?",
        (session_id,))
    return {r["name"]: r["value"] for r in rows}


def setup_state(conn, session_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM setup_state WHERE session_id = ?",
                     (session_id,)).fetchone()
    return dict(r) if r else None


def latest_session(conn) -> dict | None:
    """The most recently created session, whichever process created it.

    The SQLite file is shared by every server instance, so this is how a
    process that is not itself recording can still file notes and opponent
    telemetry against the session that is.

    `last_seen_at` is the collector's heartbeat and is the only field here
    that answers "is it still going". `lap_count` and `last_lap_at` describe
    what the session has produced, which is a different question and a bad
    proxy for the first one -- see touch_session.
    """
    r = conn.execute(
        "SELECT sessions.*,"
        " (SELECT COUNT(*) FROM laps WHERE laps.session_id = sessions.id)"
        "   AS lap_count,"
        " (SELECT MAX(completed_at) FROM laps WHERE laps.session_id ="
        "   sessions.id) AS last_lap_at"
        " FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def set_session_setup(conn, session_id: int, setup_name: str) -> bool:
    """Record the setup now on the car. True if a row was updated.

    AC's shared memory does not expose the setup loaded in the garage, so
    this cannot be detected -- it has to be recorded deliberately.

    This stamps the *session's current* setup, which laps completed from now
    on are tagged with. It deliberately does not touch laps already stored:
    the tuning loop changes setup in the pits and keeps driving within one
    session, so rewriting history here would relabel the baseline laps as
    the new setup and destroy the very comparison this exists to enable.
    """
    cur = conn.execute(
        "UPDATE sessions SET setup_name = ? WHERE id = ?",
        (setup_name, session_id))
    conn.commit()
    return cur.rowcount > 0


def label_unattributed_laps(conn, session_id: int, setup_name: str) -> int:
    """Fill in the setup for laps that have none. Returns how many.

    The no-rewriting rule above is about not overwriting a *known* setup
    with a different one. A lap labelled '' is not a competing claim, it is
    a gap -- the usual cause being that the driver told us which setup they
    were on after the run rather than before. Filling a blank completes a
    comparison; overwriting a name would destroy one, and this still
    refuses to do that.
    """
    cur = conn.execute(
        "UPDATE laps SET setup_name = ?"
        " WHERE session_id = ? AND (setup_name IS NULL OR setup_name = '')",
        (setup_name, session_id))
    conn.commit()
    return cur.rowcount


def session_setup(conn, session_id: int) -> str:
    r = conn.execute("SELECT setup_name FROM sessions WHERE id = ?",
                     (session_id,)).fetchone()
    return (r["setup_name"] if r else "") or ""


def store_lap(conn, session_id: int, lap_number: int, lap_time_ms: int,
              valid: bool, samples: list[tuple],
              setup_name: str | None = None, complete: bool = True) -> int:
    """Store a lap and its samples, whether or not it reached the line.

    complete=False is for a lap abandoned mid-way -- a crash, a reset to the
    pits, recording stopped. Those used to be discarded, which made the one
    lap a driver most wants to look at the only one guaranteed not to be
    recorded.

    lap_time_ms does not mean the same thing for those. A complete lap
    carries the game's official time; an incomplete one carries wall-clock
    milliseconds since the lap started, which is not a lap time and is not
    comparable to one. Anything ranking or averaging times has to exclude
    incomplete laps by name: a lap abandoned before the line turned a run
    comparison into "within noise, change +17630ms" purely by being read as
    though it were a lap.

    setup_name defaults to whatever set_session_setup last recorded for this
    session -- a snapshot taken at store time, not a live join. That is the
    whole point: the setup is copied onto the lap as it lands, so changing
    setup later tags only subsequent laps and leaves these alone.
    """
    if setup_name is None:
        setup_name = session_setup(conn, session_id)
    try:
        return _store_lap(conn, session_id, lap_number, lap_time_ms, valid,
                          samples, setup_name, complete)
    except Exception:
        # A lap and its samples are one write. Without this, anything that
        # raises between the two -- a malformed tuple, a disk error -- left
        # the lap row inserted and uncommitted, and the caller decides what
        # happens next: a later commit adopts a lap with no telemetry, and
        # until then the open transaction holds a write lock that every
        # other connection waits on. The collector catches exceptions from
        # this and keeps going, which is exactly the caller that would do
        # it.
        conn.rollback()
        raise


def _store_lap(conn, session_id, lap_number, lap_time_ms, valid, samples,
               setup_name, complete):
    cur = conn.execute(
        "INSERT INTO laps (session_id, lap_number, lap_time_ms, valid,"
        " completed_at, setup_name, complete) VALUES (?,?,?,?,?,?,?)",
        (session_id, lap_number, lap_time_ms, int(valid), time.time(),
         setup_name or "", int(complete)),
    )
    lap_id = cur.lastrowid
    placeholders = ",".join("?" * len(SAMPLE_COLUMNS))
    # A sample tuple from an older layout is padded with NULL rather than
    # refused: the trailing columns are the nullable ones added by later
    # migrations, so a caller written against v7 or v8 -- a test fixture, a
    # replay of stored rows -- still writes valid samples, and the fields it
    # never knew about read as "not recorded" rather than as zeroes.
    #
    # Only for widths a real layout actually had, though. Padding anything
    # shorter turned a field dropped in the MIDDLE into a silent write with
    # every column after the gap shifted by one: a tuple missing `steer`
    # stored gear 7000, rpm 1 and a world coordinate in tyres_out, and
    # raised nothing. That is a worse outcome than the tolerance was ever
    # worth, and the tolerance costs nothing to keep for the three widths
    # that are real.
    width = len(SAMPLE_COLUMNS) - 1          # minus lap_id, prepended below
    rows = []
    for s in samples:
        s = tuple(s)
        if len(s) != width:
            if len(s) not in SAMPLE_WIDTHS:
                raise ValueError(
                    f"sample tuple has {len(s)} fields; expected {width}"
                    f" (or {', '.join(str(w) for w in SAMPLE_WIDTHS[1:])}"
                    f" from an earlier schema). A width that is not one of"
                    f" these means a field was added or dropped in the"
                    f" middle, which would store silently against the wrong"
                    f" columns.")
            s += (None,) * (width - len(s))
        rows.append((lap_id, *s))
    conn.executemany(
        f"INSERT INTO samples ({','.join(SAMPLE_COLUMNS)})"
        f" VALUES ({placeholders})",
        rows,
    )
    conn.commit()
    return lap_id


def list_laps(conn, session_id: int | None = None, limit: int = 50):
    q = ("SELECT laps.*, sessions.car, sessions.track, sessions.track_config,"
         " laps.setup_name"
         " FROM laps JOIN sessions ON sessions.id = laps.session_id")
    args: list = []
    if session_id is not None:
        q += " WHERE session_id = ?"
        args.append(session_id)
    q += " ORDER BY laps.id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args)]


def get_lap(conn, lap_id: int) -> dict | None:
    r = conn.execute(
        "SELECT laps.*, sessions.car, sessions.track, sessions.track_config,"
        " sessions.tyre_compound, sessions.air_temp, sessions.road_temp,"
        " sessions.setup_name"
        " FROM laps JOIN sessions ON sessions.id = laps.session_id"
        " WHERE laps.id = ?", (lap_id,)).fetchone()
    return dict(r) if r else None


def get_samples(conn, lap_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM samples WHERE lap_id = ? ORDER BY t_ms", (lap_id,))
    return [dict(r) for r in rows]


def list_sessions(conn, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT sessions.*, COUNT(laps.id) AS lap_count,"
        " MIN(CASE WHEN laps.valid THEN laps.lap_time_ms END) AS best_ms"
        " FROM sessions LEFT JOIN laps ON laps.session_id = sessions.id"
        " GROUP BY sessions.id ORDER BY sessions.id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


RIVAL_SAMPLE_COLUMNS = [
    "session_id", "car_index", "lap_count", "spline", "speed_kmh",
    "gear", "gas", "brake", "created_at",
]


def store_rival_batch(conn, session_id, drivers: list[dict],
                      samples: list[dict]) -> int:
    """Upsert driver rows and append a batch of opponent samples.

    Returns the number of samples newly stored. Driver metadata (name,
    best/last lap) arrives on every batch because it changes as the session
    runs -- but it arrives stamped on every *sample*, so a full grid at 10Hz
    would otherwise mean well over a thousand redundant upserts per second.
    Collapse to one row per car first.
    """
    now = time.time()

    # Merge per field rather than letting the last entry win outright.
    #
    # Last-write-wins was correct for lap_count, which rides on every
    # sample, and wrong for everything else. The Lua app stamps the driver
    # name, car model and lap times onto the *first* sample of each car per
    # batch -- carrying them on all ten samples a second doubled the JSON it
    # serialises on the render thread -- so every later entry holds blanks.
    # Taking the last one therefore overwrote every real value with an empty
    # string or a null, which is why a whole race produced rival rows with
    # no names and no lap times.
    #
    # Merging keeps "freshest wins" where a field actually repeats, without
    # letting an absent field erase a present one.
    by_car: dict[int, dict] = {}
    for d in drivers:
        merged = by_car.setdefault(d["car_index"], {})
        for key, value in d.items():
            if value is None or value == "":
                continue          # absence is not an update
            merged[key] = value
        merged["car_index"] = d["car_index"]

    for car_index, d in by_car.items():
        prev = conn.execute(
            "SELECT lap_count, last_lap_ms FROM rival_drivers"
            " WHERE session_id = ? AND car_index = ?",
            (session_id, car_index)).fetchone()
        lap_count = d.get("lap_count", 0) or 0
        last_lap_ms = d.get("last_lap_ms")

        # A car's completed-lap counter advancing means last_lap_ms now
        # describes the lap it just finished -- which is the only moment
        # that time can be tied to a specific lap of stored samples.
        if (prev is not None and last_lap_ms and lap_count > (prev["lap_count"] or 0)):
            conn.execute(
                "INSERT OR IGNORE INTO rival_laps (session_id, car_index,"
                " lap_count, lap_time_ms, recorded_at) VALUES (?,?,?,?,?)",
                (session_id, car_index, lap_count - 1, int(last_lap_ms), now))

        conn.execute(
            "INSERT INTO rival_drivers (session_id, car_index, driver_name,"
            " car_model, best_lap_ms, last_lap_ms, lap_count, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id, car_index) DO UPDATE SET"
            " driver_name=excluded.driver_name,"
            " car_model=excluded.car_model,"
            " best_lap_ms=excluded.best_lap_ms,"
            " last_lap_ms=excluded.last_lap_ms,"
            " lap_count=excluded.lap_count,"
            " updated_at=excluded.updated_at",
            (session_id, car_index, d.get("driver_name", ""),
             d.get("car_model", ""), d.get("best_lap_ms"),
             last_lap_ms, lap_count, now))

    rows = [
        (session_id, s["car_index"], s["lap_count"], s["spline"],
         s["speed_kmh"], s.get("gear"), s.get("gas"), s.get("brake"), now)
        for s in samples
    ]
    stored = 0
    if rows:
        placeholders = ",".join("?" * len(RIVAL_SAMPLE_COLUMNS))
        before = conn.total_changes
        # OR IGNORE against the unique index: a resent batch is a no-op
        # rather than a set of duplicates that skew the resampled means.
        conn.executemany(
            f"INSERT OR IGNORE INTO rival_samples"
            f" ({','.join(RIVAL_SAMPLE_COLUMNS)})"
            f" VALUES ({placeholders})", rows)
        stored = conn.total_changes - before
    conn.commit()
    return stored


def prune_rival_samples(conn, session_id: int, keep_laps: int = 12) -> int:
    """Drop all but the most recent `keep_laps` laps per rival.

    20 cars at 10Hz is roughly 60MB an hour, in a database the user keeps
    forever. Comparisons only ever reach for a rival's quick lap, so old
    laps past a generous window are dead weight. Laps that have a recorded
    time are kept preferentially -- those are the ones worth comparing to.
    """
    removed = 0
    for row in conn.execute(
            "SELECT DISTINCT car_index FROM rival_samples WHERE session_id = ?",
            (session_id,)):
        car = row["car_index"]
        laps = [r["lap_count"] for r in conn.execute(
            "SELECT DISTINCT lap_count FROM rival_samples"
            " WHERE session_id = ? AND car_index = ?"
            " ORDER BY lap_count DESC", (session_id, car))]
        if len(laps) <= keep_laps:
            continue
        timed = {r["lap_count"] for r in conn.execute(
            "SELECT lap_count FROM rival_laps"
            " WHERE session_id = ? AND car_index = ?", (session_id, car))}
        keep = set(laps[:keep_laps]) | timed
        drop = [lap for lap in laps if lap not in keep]
        for lap in drop:
            cur = conn.execute(
                "DELETE FROM rival_samples WHERE session_id = ?"
                " AND car_index = ? AND lap_count = ?",
                (session_id, car, lap))
            removed += cur.rowcount
    if removed:
        conn.commit()
    return removed


def rival_lap_times(conn, session_id: int, car_index: int) -> dict[int, int]:
    """{lap_count: lap_time_ms} for the laps we have a time for."""
    return {r["lap_count"]: r["lap_time_ms"] for r in conn.execute(
        "SELECT lap_count, lap_time_ms FROM rival_laps"
        " WHERE session_id = ? AND car_index = ?", (session_id, car_index))}


def list_rivals(conn, session_id: int, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM rival_drivers WHERE session_id = ?"
        " ORDER BY CASE WHEN best_lap_ms IS NULL THEN 1 ELSE 0 END,"
        " best_lap_ms ASC LIMIT ?", (session_id, limit))
    return [dict(r) for r in rows]


def get_rival_lap_samples(conn, session_id: int, car_index: int,
                          lap_count: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM rival_samples WHERE session_id = ? AND car_index = ?"
        " AND lap_count = ? ORDER BY spline",
        (session_id, car_index, lap_count))
    return [dict(r) for r in rows]


def rival_lap_counts(conn, session_id: int, car_index: int) -> list[dict]:
    """Which laps we have samples for, and how well covered each one is.

    Coverage matters: a lap we only saw half of would produce a comparison
    that silently omits the corners we missed.
    """
    rows = conn.execute(
        "SELECT lap_count, COUNT(*) AS n, MIN(spline) AS lo, MAX(spline) AS hi"
        " FROM rival_samples WHERE session_id = ? AND car_index = ?"
        " GROUP BY lap_count ORDER BY lap_count", (session_id, car_index))
    return [dict(r) for r in rows]


def add_note(conn, session_id, lap_count: int, spline: float, tag: str,
             speed_kmh: float) -> int:
    cur = conn.execute(
        "INSERT INTO notes (session_id, lap_count, spline, tag, speed_kmh,"
        " created_at) VALUES (?,?,?,?,?,?)",
        (session_id, lap_count, spline, tag, speed_kmh, time.time()))
    conn.commit()
    return cur.lastrowid


SUSPENSION_COLUMNS = [
    "session_id", "lap_count", "t_ms", "spline", "source", "brake",
    "speed_kmh",
    "travel_fl", "travel_fr", "travel_rl", "travel_rr",
    "load_fl", "load_fr", "load_rl", "load_rr",
    "ride_f", "ride_r", "plank_wear",
]


def store_suspension_batch(conn, session_id: int, source: str,
                           samples: list[dict]) -> int:
    """Append suspension samples. Returns how many were newly stored.

    OR IGNORE against the primary key: the app resends a batch whose
    response it never saw, and the physics clock makes each sample
    identifiable, so a retry is a no-op instead of a duplicate that would
    skew every histogram built from it.
    """
    rows = [
        (session_id, s["lap_count"], s["t_ms"], s["spline"], source,
         s.get("brake"), s.get("speed_kmh"),
         s.get("travel_fl"), s.get("travel_fr"),
         s.get("travel_rl"), s.get("travel_rr"),
         s.get("load_fl"), s.get("load_fr"),
         s.get("load_rl"), s.get("load_rr"),
         s.get("ride_f"), s.get("ride_r"), s.get("plank_wear"))
        for s in samples
    ]
    if not rows:
        return 0
    placeholders = ",".join("?" * len(SUSPENSION_COLUMNS))
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO suspension_samples"
        f" ({','.join(SUSPENSION_COLUMNS)}) VALUES ({placeholders})", rows)
    conn.commit()
    return conn.total_changes - before


def get_suspension_samples(conn, session_id: int, lap_count: int,
                           source: str | None = None) -> list[dict]:
    """Samples for one lap, of one tier or all of them.

    Both tiers are returned by default because they carry different things:
    the worker has travel at 333Hz, the render-rate rows have the wheel
    loads and ride height that the physics API does not expose at all.
    Every row keeps its own `source` so the analysis can keep them apart --
    which it must, since they are different channels on different clocks.
    """
    q = ("SELECT * FROM suspension_samples WHERE session_id = ?"
         " AND lap_count = ?")
    args: list = [session_id, lap_count]
    if source is not None:
        q += " AND source = ?"
        args.append(source)
    q += " ORDER BY t_ms"
    return [dict(r) for r in conn.execute(q, args)]


def best_suspension_source(conn, session_id: int,
                           lap_count: int) -> str | None:
    rows = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) AS n FROM suspension_samples"
        " WHERE session_id = ? AND lap_count = ? GROUP BY source",
        (session_id, lap_count))}
    if rows.get("worker"):
        return "worker"
    return "app" if rows.get("app") else None


def suspension_lap_counts(conn, session_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT lap_count, source, COUNT(*) AS n,"
        " MIN(spline) AS lo, MAX(spline) AS hi"
        " FROM suspension_samples WHERE session_id = ?"
        " GROUP BY lap_count, source ORDER BY lap_count", (session_id,))
    return [dict(r) for r in rows]


def prune_suspension_samples(conn, session_id: int,
                             keep_laps: int = 20) -> int:
    """Keep the most recent `keep_laps` laps of suspension data.

    At 333Hz this is the highest-volume table in the database by some
    margin, and old laps are not what anyone compares against.
    """
    laps = [r["lap_count"] for r in conn.execute(
        "SELECT DISTINCT lap_count FROM suspension_samples"
        " WHERE session_id = ? ORDER BY lap_count DESC", (session_id,))]
    if len(laps) <= keep_laps:
        return 0
    cur = conn.execute(
        "DELETE FROM suspension_samples WHERE session_id = ?"
        " AND lap_count < ?", (session_id, laps[keep_laps - 1]))
    conn.commit()
    return cur.rowcount


def count_orphan_notes(conn) -> int:
    """Notes stored while nothing was recording, so unattached to a session."""
    return conn.execute(
        "SELECT COUNT(*) FROM notes WHERE session_id IS NULL").fetchone()[0]


def list_notes(conn, session_id: int | None = None, limit: int = 100):
    q = "SELECT * FROM notes"
    args: list = []
    if session_id is not None:
        q += " WHERE session_id = ?"
        args.append(session_id)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args)]
