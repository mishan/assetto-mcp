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
-- tyres_out is here on purpose: it has existed since the first schema, and
-- the v11 track-limits backfill short-circuits without it. A fixture that
-- omitted it made every migration test pass with the backfill never
-- running -- which is exactly the path a real database does take.
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

    The samples matter: the v11 backfill re-derives track limits from them,
    so a fixture without any exercises a path no real database takes.
    `tyres_out` maps lap number -> the list of per-sample wheels-off
    counts for that lap. Laps absent from it get 40 clean samples. Keyed by
    lap number rather than positional because a fixture usually cares about
    one dirty lap in a run of clean ones.
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


def test_laps_stored_before_the_outlier_rule_are_flagged_on_upgrade():
    """The 10:22 lap is still in the user's database.

    The rule was only ever applied at write time, so shipping it fixed
    nothing for data already recorded. Since v11 it sets `outlier` rather
    than hiding the lap: the lap stays stored, stays readable and stays
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

    v11 recomputes the first two from evidence that is still there. The
    third -- a pit visit -- was never recorded anywhere else, so a lap
    excluded only for that would come out of the migration looking like a
    clean flying lap and start polluting every comparison. That is the
    exact failure this schema change exists to stop, so it must not be the
    thing the change causes.
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
    """Sebring 129, in migration form.

    A clean lap stored `valid = 0` because three wheels touched a flat kerb.
    The samples proving it never left the track were there the whole time,
    and v11 reads them: the lap comes back readable and comparable without
    anyone re-driving it. This is the case that justifies the whole schema
    change, so it is pinned rather than assumed.
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
    """A file stamped past v4 without `complete`, which does exist.

    The v11 pass reasons about why old laps were excluded and wants
    `complete` for it. Guarding one query and then naming the column
    directly in the next raised OperationalError mid-migration -- and a
    raise inside _migrate aborts every later step AND leaves user_version
    un-bumped, so the database is stuck below the current schema forever
    and the server will not start at all. Nothing here may assume a column
    it has not checked for.
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
    """The 10:22 lap, which is both, and the reasons are not exclusive.

    Being a gross outlier used to count as explaining an old exclusion --
    so the lap was left unpitted, and since outliers are usable under the
    new model, a wall-clock pit time walked into lap-time comparisons. The
    exact lap that motivated the outlier rule in the first place.
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
            print("  a slow old exclusion stays out of lap-time maths")
        finally:
            conn.close()


def test_a_slow_lap_that_ran_wide_is_still_given_back():
    # Explained by the old track-limits rule, so it is re-admitted even
    # though it is also slow -- the conservative rule must not swallow the
    # case the whole migration exists for.
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
# The one thing every analysis was blind to: norm_pos says where the car is
# ALONG the lap, nothing said where it was across it. carCoordinates was
# being read 25 times a second and discarded, so no lap before this
# migration has a driving line and none ever can.

V8_COLUMNS = ("pos_x", "pos_y", "pos_z", "heading", "pitch", "roll",
              "tc_active", "abs_active")
V9_COLUMNS = ("wear_fl", "wear_fr", "wear_rl", "wear_rr", "damage")


def test_the_new_sample_columns_arrive_on_an_upgraded_database():
    """The failure this whole module exists for: ALTER, not CREATE.

    Both migrations, not just v8. CREATE TABLE IF NOT EXISTS silently does
    nothing on a database that already has the table, so a step that is
    missing from _migrate looks exactly like one that worked -- on a fresh
    database, which is the only kind the rest of the suite builds. The
    driver's database is not fresh, and it is the only one that matters.
    """
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


# The samples table exactly as it stood at v7 -- the last schema before
# columns started being added to it. A historical snapshot on purpose: it is
# the shape of the database the driver has actually been recording into, and
# the only fixture that can tell a migration that ran from one that was
# forgotten. The V0_SCHEMA stub above cannot, because its samples table is
# two columns and never had these.
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
    """The generalisation, so the next migration cannot be forgotten.

    SAMPLE_COLUMNS is what store_lap writes against. A column that reaches
    it without reaching _migrate works on every fresh database -- which is
    the only kind the rest of the suite builds -- and takes an
    OperationalError on the first lap of the next session on the only
    database that matters.
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

    ALTER TABLE on a table that does not exist raises, and one raise takes
    every later step with it AND leaves user_version un-bumped -- so the
    database is stuck below the current schema permanently, not just this
    once. This is the real shape: sessions and laps present, samples never
    created, which is what a database that only ever held imported rows
    looks like.

    Every ALTER site goes through _add_column, which no-ops on a missing
    table, so there is no longer an unguarded one to find -- including the
    v1 step on `laps`, which an earlier draft of this docstring called out
    as still latent.
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
        except (sqlite3.Error, ValueError):
            pass
        else:
            raise AssertionError("an over-long sample tuple was accepted")
        print("  short tuple padded with NULL, over-long tuple refused")
        conn.close()


def test_a_field_dropped_in_the_middle_is_refused_not_padded():
    """Padding was applied to any short tuple, so a tuple short for the
    wrong reason stored silently against shifted columns.

    Measured before the fix, on a full-width tuple missing `steer`: gear
    7000, rpm 1, tyres_out 297.63 (a world coordinate), damage NULL, and no
    error anywhere. Every value after the gap was one column out.

    Only the widths a real layout actually had are padded now.
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

    The lap row goes in first and the samples follow, so anything that
    raises in between left the lap inserted and the transaction open. Two
    things then go wrong at once: whoever commits next adopts a lap with no
    telemetry -- which reads as a lap driven and reported as having no
    samples -- and until then the open transaction holds a write lock every
    other connection blocks on. The collector catches exceptions from this
    and carries on, which is precisely the caller that would do it.
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
