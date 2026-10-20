"""Opening a database that was created by an older version.

Every other test starts from an empty file, where CREATE TABLE IF NOT EXISTS
produces the current schema. On a database that already exists it does
nothing, so a column added without a _migrate step passes those tests and
is never applied to a real database.
"""

import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module, temp_db  # noqa: E402

from assetto_mcp import db  # noqa: E402

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
-- tyres_out has existed since the first schema, and the v11 track-limits
-- backfill short-circuits without it, so a fixture without it would never
-- exercise the backfill a real database goes through.
CREATE TABLE samples (lap_id INTEGER NOT NULL, t_ms INTEGER NOT NULL,
                      tyres_out INTEGER NOT NULL DEFAULT 0);
CREATE TABLE notes (
    id INTEGER PRIMARY KEY, session_id INTEGER, lap_count INTEGER NOT NULL,
    spline REAL NOT NULL, tag TEXT NOT NULL, speed_kmh REAL NOT NULL,
    created_at REAL NOT NULL
);
"""


def _v0_database(path: Path, laps, tyres_out=None) -> None:
    """A pre-setup_name database, with samples.

    The v11 backfill re-derives track limits from the samples, so they have
    to be there. `tyres_out` maps lap number -> the list of per-sample
    wheels-off counts for that lap; laps absent from it get 40 clean
    samples.
    """
    raw = sqlite3.connect(path)
    raw.executescript(V0_SCHEMA)
    raw.execute("INSERT INTO sessions (started_at, car, track, setup_name)"
                " VALUES (?,?,?,?)",
                (time.time(), "ks_mazda_mx5_cup", "mugello", "baseline"))
    for n, (lap_time, valid) in enumerate(laps, start=1):
        raw.execute("INSERT INTO laps (session_id, lap_number, lap_time_ms,"
                    " valid, completed_at) VALUES (1,?,?,?,?)",
                    (n, lap_time, valid, time.time()))
        wheels = (tyres_out or {}).get(n, [0] * 40)
        raw.executemany(
            "INSERT INTO samples (lap_id, t_ms, tyres_out) VALUES (?,?,?)",
            [(n, i * 40, w) for i, w in enumerate(wheels)])
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
    """v4 adds laps.complete, and every existing lap is complete.

    Before v4 a lap only reached the database by crossing the line. If the
    ALTER never runs, every query mentioning complete fails; if the column
    defaults to 0, every finished lap reads back as abandoned.
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


def test_laps_stored_before_the_outlier_rule_are_flagged_on_upgrade():
    """A grossly slow lap recorded before the rule existed gets `outlier`.

    The rule applies at write time, so laps already stored need the
    migration. It flags rather than hides: the lap stays readable and
    comparable, and anything ranking lap times can see why it stands out.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        _v0_database(path, [(114054, 1), (622162, 1), (115000, 1)])

        conn = db.connect(path)
        rows = {r["lap_time_ms"]: dict(r) for r in conn.execute(
            "SELECT lap_time_ms, outlier, invalid FROM laps")}
        assert rows[622162]["outlier"] == 1, rows
        assert rows[114054]["outlier"] == 0 and rows[115000]["outlier"] == 0
        # Flagged, not invalidated: it did not leave the track.
        assert rows[622162]["invalid"] == 0, rows
        assert db.list_sessions(conn)[0]["best_ms"] == 114054
        print("  10:22 lap flagged as an outlier; best_ms is clean")
        conn.close()


def test_an_old_exclusion_is_never_silently_undone():
    """Pre-v11 `valid = 0` meant off-track OR pitted OR grossly slow.

    v11 recomputes off-track and slow from evidence that is still there. A
    pit visit was never recorded anywhere else, so a lap with no other
    explanation must come out as `pitted`, not as a clean flying lap.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        # 115000 is not slow enough to be an outlier and its samples show
        # no wheels off, so the only explanation left is a pit visit.
        _v0_database(path, [(114054, 1), (115000, 0)])
        conn = db.connect(path)
        rows = {r["lap_time_ms"]: dict(r)
                for r in db.list_laps(conn, limit=None)}
        excluded = rows[115000]
        assert excluded["pitted"] == 1, excluded
        usable, why = db.lap_usability(excluded)
        assert usable is False, why
        # And its telemetry is untouched -- nothing was deleted to do this.
        assert db.get_samples(conn, excluded["id"]), "samples were lost"
        print("  an unexplained old exclusion survives as `pitted`")
        conn.close()


def test_the_upgrade_gives_back_a_lap_the_old_rule_wrongly_excluded():
    """A lap stored `valid = 0` for three wheels on a kerb becomes usable.

    The samples show it never left the track, and v11 reads them, so the
    lap comes back readable and comparable.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        _v0_database(
            path,
            [(114054, 1), (126769, 0)],
            # Lap 2: three wheels over the line for a full second, which the
            # old "> 2 tyres out" rule counted as a cut and the game did not.
            tyres_out={2: [0] * 10 + [3] * 25 + [0] * 5},
        )
        conn = db.connect(path)
        rows = {r["lap_time_ms"]: dict(r)
                for r in db.list_laps(conn, limit=None)}
        given_back = rows[126769]
        assert given_back["invalid"] == 0, given_back
        assert given_back["max_tyres_out"] == 3, given_back
        assert given_back["excursions"] == 0, given_back
        # Explained by the evidence, so not written off as a pit visit.
        assert given_back["pitted"] == 0, given_back
        assert db.lap_usability(given_back) == (True, None), given_back
        print("  a lap excluded for three wheels on a kerb is back")
        conn.close()


def test_a_lap_that_really_did_cut_stays_flagged_after_the_upgrade():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        _v0_database(path, [(114054, 1), (111000, 0)],
                     tyres_out={2: [0] * 10 + [4] * 25 + [0] * 5})
        conn = db.connect(path)
        rows = {r["lap_time_ms"]: dict(r)
                for r in db.list_laps(conn, limit=None)}
        assert rows[111000]["invalid"] == 1, rows[111000]
        assert rows[111000]["excursions"] == 1, rows[111000]
        # Still usable, though -- running wide is not a reason to drop it.
        assert db.lap_usability(rows[111000])[0] is True
        conn.close()


def test_a_database_missing_a_column_it_should_have_still_upgrades():
    """A file stamped past v4 without `complete`, which does occur.

    The v11 pass wants `complete` to reason about old exclusions. A raise
    inside _migrate aborts every later step and leaves user_version
    un-bumped, so the database is stuck below the current schema and the
    server will not start. Nothing there may assume a column it has not
    checked for.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "odd.db"
        raw = sqlite3.connect(path)
        # A v7-shaped laps table -- setup_name present, as v1 added it --
        # with `complete` missing, and stamped past the version that would
        # have added it so the v4 step is skipped.
        raw.executescript(V0_SCHEMA.replace(
            "    valid INTEGER NOT NULL DEFAULT 1, completed_at REAL NOT NULL",
            "    valid INTEGER NOT NULL DEFAULT 1, completed_at REAL NOT NULL,"
            " setup_name TEXT NOT NULL DEFAULT ''"))
        raw.execute("INSERT INTO sessions (started_at, car, track)"
                    " VALUES (?,?,?)", (time.time(), "carx", "mugello"))
        # One excluded lap, or the pass returns before it gets that far.
        for n, (ms, valid) in enumerate([(114054, 1), (134000, 0)], start=1):
            raw.execute("INSERT INTO laps (session_id, lap_number,"
                        " lap_time_ms, valid, completed_at)"
                        " VALUES (1,?,?,?,?)", (n, ms, valid, time.time()))
        raw.execute("PRAGMA user_version = 7")
        raw.commit()
        raw.close()

        conn = db.connect(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == \
                db.SCHEMA_VERSION, "migration stopped short"
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(laps)")}
            assert {"complete", "invalid", "pitted"} <= cols, cols
            assert len(db.list_laps(conn, limit=None)) == 2
            print("  a database missing `complete` upgrades cleanly")
        finally:
            conn.close()


def test_a_pit_lap_that_is_also_an_outlier_stays_excluded():
    """Being an outlier does not explain away an old exclusion.

    Outliers are usable, so treating slowness as the reason would let a
    wall-clock pit time into lap-time comparisons.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        _v0_database(path, [(114054, 1), (622162, 0)])
        conn = db.connect(path)
        try:
            lap = [dict(r) for r in db.list_laps(conn, limit=None)
                   if r["lap_time_ms"] == 622162][0]
            assert lap["pitted"] == 1, lap
            # Not additionally flagged an outlier, and that is right: a pit
            # lap's time is wall clock, so there is no lap time for it to be
            # an outlier of. backfill_outliers skips pitted laps for the
            # same reason it skips them when picking the reference.
            assert lap["outlier"] == 0, lap
            usable, why = db.lap_usability(lap)
            assert usable is False, why
            assert db.list_sessions(conn)[0]["best_ms"] == 114054
            print("  a slow old exclusion stays out of lap-time math")
        finally:
            conn.close()


def test_a_slow_lap_that_ran_wide_is_still_given_back():
    # Explained by the old track-limits rule, so it is re-admitted even
    # though it is also slow.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        _v0_database(path, [(114054, 1), (126769, 0)],
                     tyres_out={2: [0] * 10 + [3] * 25 + [0] * 5})
        conn = db.connect(path)
        try:
            lap = [dict(r) for r in db.list_laps(conn, limit=None)
                   if r["lap_time_ms"] == 126769][0]
            assert lap["pitted"] == 0, lap
            assert db.lap_usability(lap) == (True, None), lap
        finally:
            conn.close()


def test_a_v11_step_does_not_depend_on_a_v12_column():
    """Migration steps run in order, and v11 calls backfill_excursions.

    That function skips laps retention has thinned, using `sample_stride`,
    which arrives in v12. Upgrading from v10 the column does not exist yet
    when v11 runs; a database that predates retention has nothing thinned,
    so its absence must be treated as stride 1, not raise.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "v10.db"
        _v0_database(path, [(114054, 1), (126769, 0)],
                     tyres_out={2: [0] * 10 + [4] * 25 + [0] * 5})
        raw = sqlite3.connect(path)
        # Everything v11 needs, stamped just below it.
        for col, decl in (("setup_name", "TEXT NOT NULL DEFAULT ''"),
                          ("complete", "INTEGER NOT NULL DEFAULT 1")):
            try:
                raw.execute(f"ALTER TABLE laps ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        raw.execute("PRAGMA user_version = 10")
        raw.commit()
        raw.close()

        conn = db.connect(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == \
                db.SCHEMA_VERSION, "migration stopped short"
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(laps)")}
            assert "sample_stride" in cols, cols
            # And v11's re-scoring still happened on the way through.
            lap = [dict(r) for r in db.list_laps(conn, limit=None)
                   if r["lap_time_ms"] == 126769][0]
            assert lap["invalid"] == 1 and lap["excursions"] == 1, lap
            assert lap["sample_stride"] == 1, lap
            print("  v10 -> v12 in one hop, with v11's backfill intact")
        finally:
            conn.close()


def test_v13_adds_its_tables_to_a_real_v12_database():
    """v13 is new tables only, so it has no ALTER step at all.

    That relies on connect() running _migrate and *then*
    executescript(SCHEMA), where CREATE TABLE IF NOT EXISTS creates them.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "v12.db"
        # Build a genuine v12 file: the current schema minus the two tables
        # v13 introduces, stamped at 12.
        raw = sqlite3.connect(path)
        v12 = db.SCHEMA
        for table in ("display_observations", "display_notes"):
            v12 = re.sub(
                rf"CREATE TABLE IF NOT EXISTS {table}\s*\(.*?\n\);",
                "", v12, flags=re.DOTALL)
        assert "display_observations" not in v12, "fixture still has v13"
        raw.executescript(v12)
        raw.execute("PRAGMA user_version = 12")
        raw.commit()
        raw.close()

        conn = db.connect(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == \
                db.SCHEMA_VERSION, "migration stopped short"
            # And they are writable, not merely present.
            db.record_display_observation(conn, "carx", "TOE_OUT_LF", 10, 0.1)
            db.record_display_note(conn, "carx", "TOE_OUT_LF", "reads negated")
            assert db.display_observations(conn, "carx") == \
                {"TOE_OUT_LF": [(10.0, 0.1)]}
            assert db.display_notes(conn, "carx")["TOE_OUT_LF"] == \
                "reads negated"
            print("  v12 -> v13: both display tables created and usable")
        finally:
            conn.close()


def test_v14_gives_old_opponent_samples_no_clock_rather_than_zero():
    """v14 adds the opponent clock and world position to a real v13 file.

    Nothing is backfilled: the app never sent either before, so an old
    sample has to read as having none, not as a clock of zero at the origin.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "v13.db"
        raw = sqlite3.connect(path)
        raw.executescript(db.SCHEMA)
        raw.executescript("""
            DROP TABLE rival_samples;
            CREATE TABLE rival_samples (
                session_id INTEGER NOT NULL, car_index INTEGER NOT NULL,
                lap_count INTEGER NOT NULL, spline REAL NOT NULL,
                speed_kmh REAL NOT NULL, gear INTEGER, gas REAL,
                brake REAL, created_at REAL NOT NULL);
            INSERT INTO rival_samples
                VALUES (1, 2, 3, 0.5, 150.0, 4, 1.0, 0.0, 0.0);
        """)
        raw.execute("PRAGMA user_version = 13")
        raw.commit()
        raw.close()

        conn = db.connect(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == \
                db.SCHEMA_VERSION, "migration stopped short"
            (old,) = db.get_rival_lap_samples(conn, 1, 2, 3)
            assert (old["t_ms"], old["pos_x"]) == (None, None), old
            db.store_rival_batch(conn, 1, [], [{
                "car_index": 2, "lap_count": 3, "spline": 0.6,
                "speed_kmh": 151.0, "t_ms": 5000,
                "pos_x": 1.0, "pos_y": 2.0, "pos_z": 3.0}])
            new = db.get_rival_lap_samples(conn, 1, 2, 3)[1]
            assert (new["t_ms"], new["pos_z"]) == (5000, 3.0), new
            print("  v13 -> v14: old samples have no clock, new ones do")
        finally:
            conn.close()


def test_v15_leaves_old_laps_unmeasured_rather_than_guessing():
    """v15 adds the measured setup to laps on an existing file.

    Nothing is backfilled. setup_values holds the session's latest values,
    not which laps were driven on them, so giving old laps that fingerprint
    would be the same guess the measurement exists to replace.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "v14.db"
        raw = sqlite3.connect(path)
        raw.executescript(V0_SCHEMA)
        raw.execute("INSERT INTO sessions (started_at, car, track)"
                    " VALUES (?, 'car', 'track')", (time.time(),))
        raw.execute("INSERT INTO laps (session_id, lap_number, lap_time_ms,"
                    " completed_at) VALUES (1, 1, 100000, ?)", (time.time(),))
        raw.commit()
        raw.close()
        conn = db.connect(path)
        try:
            lap = db.list_laps(conn, 1)[0]
            assert (lap["setup_fp"], lap["setup_changed"]) == ("", 0), lap
            assert db.get_session(conn, 1)["setup_fp"] == ""
            assert any("setup_fp" in line for line in
                       db.MIGRATION_LOG.get(str(path), [])), db.MIGRATION_LOG
            print("  v14 -> v15: old laps carry no measured setup")
        finally:
            conn.close()


def test_v17_gives_old_laps_no_burn_rather_than_none_burned():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "v16.db"
        raw = sqlite3.connect(path)
        raw.executescript(db.SCHEMA)
        raw.execute("ALTER TABLE laps DROP COLUMN fuel_used_l")
        raw.execute("INSERT INTO sessions (started_at, car, track)"
                    " VALUES (?, 'car', 'track')", (time.time(),))
        raw.execute("INSERT INTO laps (session_id, lap_number, lap_time_ms,"
                    " completed_at) VALUES (1, 1, 100000, ?)", (time.time(),))
        raw.execute("PRAGMA user_version = 16")
        raw.commit()
        raw.close()
        conn = db.connect(path)
        try:
            assert db.list_laps(conn, 1)[0]["fuel_used_l"] is None
            assert db.measured_fuel_burns(conn, 1) == []
            print("  v16 -> v17: old laps have no measured burn")
        finally:
            conn.close()


def test_display_readings_survive_a_retention_pass():
    # They are keyed on the car, not the session, so pruning a session's
    # samples must not take the car's screen readings with it.
    from assetto_mcp import retention
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.db"
        conn = db.connect(path)
        try:
            sid = make_session(conn, car="carx")
            db.record_display_observation(conn, "carx", "TOE_OUT_LF", 10, 0.1)
            sample = (180.0, 1.0, 0.0, 0.0, 4, 9000, 0.0, 0.0,
                      0.4, 0.4, 0.3, 0.3, 26.0, 26.0, 26.0, 26.0,
                      85.0, 85.0, 85.0, 85.0, 0.02, 0.024, 0)
            for lap in range(1, 4):
                db.store_lap(conn, sid, lap, 113000, True,
                             [(i * 40, i / 200, *sample) for i in range(200)])
            retention.enforce_budget(conn, path, 1)
            assert db.display_observations(conn, "carx") == \
                {"TOE_OUT_LF": [(10.0, 0.1)]}
        finally:
            conn.close()


def test_migration_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        _v0_database(path, [(114054, 1), (622162, 1)])
        db.connect(path).close()
        conn = db.connect(path)
        # The 10:22 lap is an outlier, flagged once and not re-flagged.
        outliers = conn.execute(
            "SELECT COUNT(*) FROM laps WHERE outlier = 1").fetchone()[0]
        assert outliers == 1, outliers
        assert db.backfill_excursions(conn) == 0, \
            "a second pass should find nothing to change"
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


# --- v8 and v9: position, attitude, electronics, wear, damage ----------
#
# norm_pos says where the car is along the lap; v8's world position says
# where it is across it. Laps recorded before v8 have no driving line.

V8_COLUMNS = ("pos_x", "pos_y", "pos_z", "heading", "pitch", "roll",
              "tc_active", "abs_active")
V9_COLUMNS = ("wear_fl", "wear_fr", "wear_rl", "wear_rr", "damage")


def test_the_new_sample_columns_arrive_on_an_upgraded_database():
    """v8 and v9 reach an existing samples table by ALTER, not CREATE."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        _v0_database(path, [(114054, 1)])
        conn = db.connect(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(samples)")}
        for c in V8_COLUMNS + V9_COLUMNS:
            assert c in cols, f"{c} missing after upgrade"
        print(f"  {len(V8_COLUMNS)} v8 and {len(V9_COLUMNS)} v9 columns "
              f"added to an existing samples table")
        conn.close()


# The samples table exactly as it stood at v7, the last schema before
# columns were added to it. Unlike the V0_SCHEMA stub, this is the shape of
# a real database, so it can tell a migration that ran from one that was
# forgotten.
V7_SAMPLES = """
CREATE TABLE samples (
    lap_id INTEGER NOT NULL, t_ms INTEGER NOT NULL, norm_pos REAL NOT NULL,
    speed_kmh REAL NOT NULL, gas REAL NOT NULL, brake REAL NOT NULL,
    steer REAL NOT NULL, gear INTEGER NOT NULL, rpm INTEGER NOT NULL,
    acc_lat REAL NOT NULL, acc_lon REAL NOT NULL,
    slip_fl REAL NOT NULL, slip_fr REAL NOT NULL,
    slip_rl REAL NOT NULL, slip_rr REAL NOT NULL,
    press_fl REAL NOT NULL, press_fr REAL NOT NULL,
    press_rl REAL NOT NULL, press_rr REAL NOT NULL,
    core_fl REAL NOT NULL, core_fr REAL NOT NULL,
    core_rl REAL NOT NULL, core_rr REAL NOT NULL,
    ride_f REAL NOT NULL, ride_r REAL NOT NULL, tyres_out INTEGER NOT NULL
);
"""


def test_every_sample_column_the_collector_writes_survives_an_upgrade():
    """Every column in SAMPLE_COLUMNS has a _migrate step.

    SAMPLE_COLUMNS is what store_lap writes against. A column missing from
    _migrate works on a fresh database and raises OperationalError on the
    first lap stored into an existing one.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "v7.db"
        raw = sqlite3.connect(path)
        raw.executescript(V0_SCHEMA.replace(
            "CREATE TABLE samples (lap_id INTEGER NOT NULL,"
            " t_ms INTEGER NOT NULL,\n"
            "                      tyres_out INTEGER NOT NULL DEFAULT 0);",
            ""))
        raw.executescript(V7_SAMPLES)
        raw.execute("PRAGMA user_version = 7")
        raw.commit()
        raw.close()

        conn = db.connect(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(samples)")}
        missing = [c for c in db.SAMPLE_COLUMNS if c not in cols]
        assert not missing, (
            f"{missing} are in SAMPLE_COLUMNS but no _migrate step adds "
            f"them to a database that already had a samples table")
        print(f"  all {len(db.SAMPLE_COLUMNS)} sample columns reachable "
              f"after upgrading a v7 database")
        conn.close()


def test_a_migration_survives_a_database_with_no_samples_table():
    """A half-built database must not abort the whole migration.

    ALTER TABLE on a missing table raises, which aborts every later step
    and leaves user_version un-bumped. A database that only ever held
    imported rows has sessions and laps but no samples table; every ALTER
    goes through _add_column, which no-ops on a missing table.
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

    The data was never captured, so the only honest value is one a reader
    can recognize as absent.
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
        except (sqlite3.Error, ValueError):
            pass
        else:
            raise AssertionError("an over-long sample tuple was accepted")
        print("  short tuple padded with NULL, over-long tuple refused")
        conn.close()


def test_a_field_dropped_in_the_middle_is_refused_not_padded():
    """Only widths a real layout had are padded.

    Padding any short tuple would store one with a field missing mid-tuple
    against shifted columns: every value after the gap one column out, and
    no error.
    """
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "new.db")
        sid = make_session(conn)
        full = tuple(range(len(db.SAMPLE_COLUMNS) - 1))
        assert len(full) in db.SAMPLE_WIDTHS

        # Drop `steer`, index 5 in the tuple (column 6, after lap_id).
        gapped = full[:5] + full[6:]
        assert len(gapped) not in db.SAMPLE_WIDTHS, (
            "this test needs a width no real layout ever had")
        try:
            db.store_lap(conn, sid, 1, 114000, True, [gapped])
        except ValueError as e:
            assert str(len(gapped)) in str(e), e
            print(f"  {len(gapped)}-field tuple refused: {e}")
        else:
            raise AssertionError("a mid-tuple gap was padded and stored")
        conn.close()


def test_every_historical_width_is_still_accepted():
    """The tolerance is the point; narrowing it must not remove it."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "new.db")
        sid = make_session(conn)
        full = tuple(range(len(db.SAMPLE_COLUMNS) - 1))
        for n, width in enumerate(db.SAMPLE_WIDTHS):
            lap_id = db.store_lap(conn, sid, n + 1, 114000, True,
                                  [full[:width]])
            row = conn.execute("SELECT COUNT(*) c FROM samples"
                               " WHERE lap_id = ?", (lap_id,)).fetchone()
            assert row["c"] == 1, width
        print(f"  widths {db.SAMPLE_WIDTHS} all stored")
        conn.close()


def test_a_failed_sample_write_leaves_no_half_stored_lap():
    """A lap and its samples are one write, or neither.

    The lap row goes in before the samples. A raise in between must not
    leave the lap inserted and the transaction open: whoever commits next
    would adopt a lap with no telemetry, and until then every other
    connection blocks on the write lock. The collector catches exceptions
    from this and carries on, so it would hit exactly that.
    """
    with temp_db() as path:
        conn = db.connect(path)
        sid = make_session(conn)
        good = tuple(range(len(db.SAMPLE_COLUMNS) - 1))

        before = conn.execute("SELECT COUNT(*) c FROM laps").fetchone()["c"]
        try:
            db.store_lap(conn, sid, 1, 114000, True,
                         [good, good + (1,) * 20])      # second is too long
        except Exception:
            pass
        else:
            raise AssertionError("an over-long sample tuple was accepted")

        # Nothing left behind, and nothing left open: another connection can
        # write immediately, which it could not through a held lock.
        after = conn.execute("SELECT COUNT(*) c FROM laps").fetchone()["c"]
        assert after == before, f"{after - before} half-stored lap(s)"
        assert conn.in_transaction is False, "transaction left open"

        other = db.connect(path)
        try:
            db.store_lap(other, sid, 2, 113000, True, [good])
        finally:
            other.close()
        rows = conn.execute("SELECT COUNT(*) c FROM laps").fetchone()["c"]
        assert rows == before + 1, rows
        print("  failed write rolled back; the connection is usable after")
        conn.close()


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
