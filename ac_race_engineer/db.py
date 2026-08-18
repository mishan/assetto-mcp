"""SQLite storage for telemetry sessions, laps, and samples.

Kept deliberately boring: one writer (the collector thread), many readers
(MCP tool calls). WAL mode makes that safe.
"""

import sqlite3
import time
from pathlib import Path

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

CREATE TABLE IF NOT EXISTS laps (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    lap_number INTEGER NOT NULL,
    lap_time_ms INTEGER NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1,
    completed_at REAL NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_rival_samples
    ON rival_samples(session_id, car_index, lap_count, spline);

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


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


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
    """Stamp which setup a session was run on. True if a row was updated.

    AC's shared memory does not expose the setup loaded in the garage, so
    this cannot be detected -- it has to be recorded deliberately.
    """
    cur = conn.execute(
        "UPDATE sessions SET setup_name = ? WHERE id = ?",
        (setup_name, session_id))
    conn.commit()
    return cur.rowcount > 0


def store_lap(conn, session_id: int, lap_number: int, lap_time_ms: int,
              valid: bool, samples: list[tuple]) -> int:
    cur = conn.execute(
        "INSERT INTO laps (session_id, lap_number, lap_time_ms, valid,"
        " completed_at) VALUES (?,?,?,?,?)",
        (session_id, lap_number, lap_time_ms, int(valid), time.time()),
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
         " sessions.setup_name"
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

    Returns the number of samples stored. Driver metadata (name, best/last
    lap) arrives on every batch because it changes as the session runs.
    """
    now = time.time()
    for d in drivers:
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
            (session_id, d["car_index"], d.get("driver_name", ""),
             d.get("car_model", ""), d.get("best_lap_ms"),
             d.get("last_lap_ms"), d.get("lap_count", 0), now))

    rows = [
        (session_id, s["car_index"], s["lap_count"], s["spline"],
         s["speed_kmh"], s.get("gear"), s.get("gas"), s.get("brake"), now)
        for s in samples
    ]
    if rows:
        placeholders = ",".join("?" * len(RIVAL_SAMPLE_COLUMNS))
        conn.executemany(
            f"INSERT INTO rival_samples ({','.join(RIVAL_SAMPLE_COLUMNS)})"
            f" VALUES ({placeholders})", rows)
    conn.commit()
    return len(rows)


def list_rivals(conn, session_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM rival_drivers WHERE session_id = ?"
        " ORDER BY CASE WHEN best_lap_ms IS NULL THEN 1 ELSE 0 END,"
        " best_lap_ms ASC", (session_id,))
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


def list_notes(conn, session_id: int | None = None, limit: int = 100):
    q = "SELECT * FROM notes"
    args: list = []
    if session_id is not None:
        q += " WHERE session_id = ?"
        args.append(session_id)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args)]
