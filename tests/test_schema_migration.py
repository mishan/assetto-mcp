"""Opening a database that was created by an older version.

This is the case that never shows up in a test suite by accident: every
other test starts from an empty file, where CREATE TABLE IF NOT EXISTS
happens to produce the current schema. On a database that already exists it
does nothing at all, which is how a column addition ships green and no-ops
on the only database anyone cares about.
"""

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module  # noqa: E402

from ac_race_engineer import db  # noqa: E402

# The schema as it stood before laps carried their own setup name.
V0_SCHEMA = """
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY, started_at REAL NOT NULL, car TEXT NOT NULL,
    track TEXT NOT NULL, track_config TEXT NOT NULL DEFAULT '',
    tyre_compound TEXT NOT NULL DEFAULT '', air_temp REAL, road_temp REAL,
    setup_name TEXT NOT NULL DEFAULT ''
);
CREATE TABLE laps (
    id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
    lap_number INTEGER NOT NULL, lap_time_ms INTEGER NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1, completed_at REAL NOT NULL
);
CREATE TABLE samples (lap_id INTEGER NOT NULL, t_ms INTEGER NOT NULL);
CREATE TABLE notes (
    id INTEGER PRIMARY KEY, session_id INTEGER, lap_count INTEGER NOT NULL,
    spline REAL NOT NULL, tag TEXT NOT NULL, speed_kmh REAL NOT NULL,
    created_at REAL NOT NULL
);
"""


def _v0_database(path: Path, laps) -> None:
    raw = sqlite3.connect(path)
    raw.executescript(V0_SCHEMA)
    raw.execute("INSERT INTO sessions (started_at, car, track, setup_name)"
                " VALUES (?,?,?,?)",
                (time.time(), "ks_mazda_mx5_cup", "mugello", "baseline"))
    for n, (lap_time, valid) in enumerate(laps, start=1):
        raw.execute("INSERT INTO laps (session_id, lap_number, lap_time_ms,"
                    " valid, completed_at) VALUES (1,?,?,?,?)",
                    (n, lap_time, valid, time.time()))
    raw.commit()
    raw.close()


def test_an_existing_database_gains_the_new_columns():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        _v0_database(path, [(114054, 1), (115000, 1)])

        conn = db.connect(path)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(laps)")}
        assert "setup_name" in cols, cols
        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            db.SCHEMA_VERSION
        # Backfilled from the session, which is the best guess available for
        # laps recorded before the column existed.
        setups_seen = {r["setup_name"] for r in conn.execute(
            "SELECT setup_name FROM laps")}
        assert setups_seen == {"baseline"}, setups_seen
        print("  laps.setup_name added and backfilled")
        conn.close()


def test_laps_stored_before_v4_are_marked_complete():
    """v4 adds laps.complete, and nothing pinned either half of that.

    Before v4 a lap only reached the database by crossing the line, so every
    row already there is complete by definition. Two ways that goes wrong on
    a real user's database and on no test: the ALTER never runs, so every
    query mentioning complete fails outright; or the column arrives
    defaulting to 0 and a season of finished laps reads back as abandoned.
    The column is asserted here as well as the value it backfills.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        # The last one is invalid -- an off-track lap still reached the
        # line, so complete has to be independent of valid.
        _v0_database(path, [(114054, 1), (115000, 1), (113800, 0)])

        conn = db.connect(path)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(laps)")}
        assert "complete" in cols, cols
        flags = [r["complete"] for r in conn.execute(
            "SELECT complete FROM laps ORDER BY lap_number")]
        assert flags == [1, 1, 1], flags
        print("  laps.complete added; 3 pre-existing laps marked complete")
        conn.close()


def test_laps_stored_before_the_outlier_rule_are_rechecked():
    """The 10:22 lap is still in the user's database, still marked valid.

    Validity was only ever computed at write time, so shipping the rule
    fixed nothing for data already recorded -- the lap that motivated the
    whole change kept poisoning best-lap queries.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        _v0_database(path, [(114054, 1), (622162, 1), (115000, 1)])

        conn = db.connect(path)
        rows = {r["lap_time_ms"]: r["valid"] for r in conn.execute(
            "SELECT lap_time_ms, valid FROM laps")}
        assert rows[622162] == 0, "the 10:22 lap is still marked valid"
        assert rows[114054] == 1
        assert rows[115000] == 1
        assert db.list_sessions(conn)[0]["best_ms"] == 114054
        print("  10:22 lap invalidated on upgrade; best_ms is clean")
        conn.close()


def test_revalidation_only_ever_invalidates():
    """Never resurrect a lap the dirty-lap rule set aside deliberately."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        # A perfectly normal lap that someone marked invalid (off-track).
        _v0_database(path, [(114054, 1), (115000, 0)])
        conn = db.connect(path)
        rows = {r["lap_time_ms"]: r["valid"] for r in conn.execute(
            "SELECT lap_time_ms, valid FROM laps")}
        assert rows[115000] == 0, rows
        print("  an invalid lap stays invalid")
        conn.close()


def test_migration_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        _v0_database(path, [(114054, 1), (622162, 1)])
        db.connect(path).close()
        conn = db.connect(path)
        valid = conn.execute(
            "SELECT COUNT(*) FROM laps WHERE valid = 1").fetchone()[0]
        assert valid == 1, valid
        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            db.SCHEMA_VERSION
        print("  reconnecting does not re-run the migration")
        conn.close()


def test_a_fresh_database_is_created_at_the_current_version():
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "new.db")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            db.SCHEMA_VERSION
        sid = make_session(conn)
        db.store_lap(conn, sid, 1, 114000, True, [])
        assert len(db.list_laps(conn, sid)) == 1
        print("  fresh database at version", db.SCHEMA_VERSION)
        conn.close()


def test_existing_rows_and_queries_survive_the_upgrade():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        _v0_database(path, [(114054, 1)])
        raw = sqlite3.connect(path)
        raw.execute("INSERT INTO notes (session_id, lap_count, spline, tag,"
                    " speed_kmh, created_at) VALUES (1,2,0.34,'understeer',"
                    " 120.0, ?)", (time.time(),))
        raw.commit()
        raw.close()

        conn = db.connect(path)
        assert len(db.list_laps(conn, 1)) == 1
        assert len(db.list_notes(conn, 1)) == 1
        assert db.get_lap(conn, 1)["track"] == "mugello"
        # And the tables added later are usable straight away.
        db.store_rival_batch(conn, 1, [{"car_index": 2}],
                             [{"car_index": 2, "lap_count": 1,
                               "spline": 0.5, "speed_kmh": 100.0}])
        assert len(db.list_rivals(conn, 1)) == 1
        print("  pre-existing laps, notes and new tables all work")
        conn.close()


# --- v8: position, attitude, electronics -------------------------------
#
# The one thing every analysis was blind to: norm_pos says where the car is
# ALONG the lap, nothing said where it was across it. carCoordinates was
# being read 25 times a second and discarded, so no lap before this
# migration has a driving line and none ever can.

V8_COLUMNS = ("pos_x", "pos_y", "pos_z", "heading", "pitch", "roll",
              "tc_active", "abs_active")


def test_the_new_sample_columns_arrive_on_an_upgraded_database():
    """The failure this whole module exists for: ALTER, not CREATE."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        _v0_database(path, [(114054, 1)])
        conn = db.connect(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(samples)")}
        for c in V8_COLUMNS:
            assert c in cols, f"{c} missing after upgrade"
        print(f"  {len(V8_COLUMNS)} columns added to an existing samples table")
        conn.close()


def test_a_migration_survives_a_database_with_no_samples_table():
    """A half-built database must not abort the whole migration.

    ALTER TABLE on a table that does not exist raises, and one raise takes
    every later step with it AND leaves user_version un-bumped -- so the
    database is stuck below the current schema permanently, not just this
    once. This is the real shape: sessions and laps present, samples never
    created, which is what a database that only ever held imported rows
    looks like.

    Note for later: the v1 step ALTERs `laps` unguarded and has the same
    hole. It needs a database with sessions but no laps to trigger, which
    nothing produces today, so it is a latent bug rather than a live one.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "partial.db"
        raw = sqlite3.connect(path)
        raw.executescript(
            "CREATE TABLE sessions (id INTEGER PRIMARY KEY,"
            " started_at REAL NOT NULL, car TEXT NOT NULL,"
            " track TEXT NOT NULL, track_config TEXT NOT NULL DEFAULT '',"
            " tyre_compound TEXT NOT NULL DEFAULT '',"
            " air_temp REAL, road_temp REAL, setup_name TEXT DEFAULT '');"
            "CREATE TABLE laps (id INTEGER PRIMARY KEY,"
            " session_id INTEGER NOT NULL, lap_number INTEGER NOT NULL,"
            " lap_time_ms INTEGER NOT NULL, valid INTEGER NOT NULL DEFAULT 1,"
            " completed_at REAL NOT NULL);")
        raw.commit()
        raw.close()
        conn = db.connect(path)          # must not raise
        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            db.SCHEMA_VERSION
        print("  a database with no samples table migrates to "
              f"v{db.SCHEMA_VERSION}")
        conn.close()


def test_old_samples_read_as_not_recorded_rather_than_as_the_origin():
    """NULL, not 0. A coordinate of zero is a place on the track map.

    Backfilling is impossible -- the data was never captured -- so the only
    honest value is one a reader can recognise as absent.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        _v0_database(path, [(114054, 1)])
        raw = sqlite3.connect(path)
        raw.execute("INSERT INTO samples (lap_id, t_ms) VALUES (1, 500)")
        raw.commit()
        raw.close()

        conn = db.connect(path)
        row = conn.execute(
            "SELECT pos_x, pos_z, roll FROM samples").fetchone()
        assert row[0] is None and row[1] is None and row[2] is None, tuple(row)
        print("  pre-v8 samples report NULL position, not 0,0")
        conn.close()


def test_a_short_sample_tuple_is_padded_and_a_long_one_is_refused():
    """An older writer must keep working; a mismatched one must not.

    Padding is what lets a caller written against an earlier layout store
    valid samples whose unknown fields read as absent. Silently accepting a
    tuple that is too long would instead mean the columns had shifted and
    every value after the mismatch was being written to the wrong field.
    """
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "new.db")
        sid = make_session(conn)
        short = (100, 0.5, 120.0, 1.0, 0.0, 0.1, 4, 9000, 0.5, 0.2,
                 0.4, 0.4, 0.3, 0.3, 26.0, 26.0, 26.0, 26.0,
                 85.0, 85.0, 85.0, 85.0, 0.02, 0.024, 0)
        lap_id = db.store_lap(conn, sid, 1, 114000, True, [short])
        row = conn.execute("SELECT speed_kmh, pos_x FROM samples"
                           " WHERE lap_id = ?", (lap_id,)).fetchone()
        assert row["speed_kmh"] == 120.0, tuple(row)
        assert row["pos_x"] is None, tuple(row)

        try:
            db.store_lap(conn, sid, 2, 114000, True, [short + (1,) * 20])
        except sqlite3.Error:
            pass
        else:
            raise AssertionError("an over-long sample tuple was accepted")
        print("  short tuple padded with NULL, over-long tuple refused")
        conn.close()


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
