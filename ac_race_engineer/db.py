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
SCHEMA_VERSION = 3

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
    setup_name TEXT NOT NULL DEFAULT ''
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
    setup_name TEXT NOT NULL DEFAULT ''
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
    tyres_out INTEGER NOT NULL
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

CREATE TABLE IF NOT EXISTS rival_laps (
    session_id INTEGER NOT NULL,
    car_index INTEGER NOT NULL,
    lap_count INTEGER NOT NULL,   -- the lap these samples belong to
    lap_time_ms INTEGER NOT NULL,
    recorded_at REAL NOT NULL,
    PRIMARY KEY (session_id, car_index, lap_count)
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
]


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


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
        if "setup_name" not in _columns(conn, "laps"):
            conn.execute("ALTER TABLE laps ADD COLUMN"
                         " setup_name TEXT NOT NULL DEFAULT ''")
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
    cur = conn.execute(
        "INSERT INTO sessions (started_at, car, track, track_config,"
        " tyre_compound, air_temp, road_temp) VALUES (?,?,?,?,?,?,?)",
        (time.time(), car, track, track_config, tyre_compound,
         air_temp, road_temp),
    )
    conn.commit()
    return cur.lastrowid


def latest_session(conn) -> dict | None:
    """The most recently created session, whichever process created it.

    The SQLite file is shared by every server instance, so this is how a
    process that is not itself recording can still file notes and opponent
    telemetry against the session that is.
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
              setup_name: str | None = None) -> int:
    """Store a completed lap and its samples.

    setup_name defaults to whatever set_session_setup last recorded for this
    session -- a snapshot taken at store time, not a live join. That is the
    whole point: the setup is copied onto the lap as it lands, so changing
    setup later tags only subsequent laps and leaves these alone.
    """
    if setup_name is None:
        setup_name = session_setup(conn, session_id)
    cur = conn.execute(
        "INSERT INTO laps (session_id, lap_number, lap_time_ms, valid,"
        " completed_at, setup_name) VALUES (?,?,?,?,?,?)",
        (session_id, lap_number, lap_time_ms, int(valid), time.time(),
         setup_name or ""),
    )
    lap_id = cur.lastrowid
    placeholders = ",".join("?" * len(SAMPLE_COLUMNS))
    conn.executemany(
        f"INSERT INTO samples ({','.join(SAMPLE_COLUMNS)})"
        f" VALUES ({placeholders})",
        [(lap_id, *s) for s in samples],
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

    by_car: dict[int, dict] = {}
    for d in drivers:
        # Last write wins: entries arrive in sample order, so this keeps the
        # freshest lap counters within the batch.
        by_car[d["car_index"]] = d

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
