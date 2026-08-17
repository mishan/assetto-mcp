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
    q = ("SELECT laps.*, sessions.car, sessions.track, sessions.track_config"
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
        " sessions.tyre_compound, sessions.air_temp, sessions.road_temp"
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
