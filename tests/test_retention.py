"""Keeping the database bounded without losing a lap.

A lap costs ~0.4 MB and a driver accumulates them forever, so something has
to give. What must not give is a lap's existence: the reference lap from
three months ago is exactly the thing a change is measured against, and a
season away from a circuit is precisely when the old run matters most.

So nothing is deleted -- the oldest sessions' traces are decimated instead,
and the lap keeps its time, its setup and its track-limits evidence at full
fidelity. These tests are almost entirely about that distinction.
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module  # noqa: E402

from assetto_mcp import db, retention  # noqa: E402

_SAMPLE = (180.0, 1.0, 0.0, 0.0, 4, 9000, 0.0, 0.0,
           0.4, 0.4, 0.3, 0.3, 26.0, 26.0, 26.0, 26.0,
           85.0, 85.0, 85.0, 85.0, 0.02, 0.024, 0)
N = 800


def _build(tmp: Path, sessions=5, laps=4):
    conn = db.connect(tmp / "t.db")
    for _ in range(sessions):
        sid = make_session(conn)
        for lap in range(1, laps + 1):
            db.store_lap(conn, sid, lap, 113000 + lap, True,
                         [(i * 40, i / N, *_SAMPLE) for i in range(N)])
    conn.commit()
    retention._reclaim(conn, [])
    return conn, tmp / "t.db"


def test_a_database_under_budget_is_left_completely_alone():
    with tempfile.TemporaryDirectory() as d:
        conn, path = _build(Path(d))
        try:
            before = retention.db_bytes(path)
            r = retention.enforce_budget(conn, path, before * 10)
            assert r["acted"] is False, r
            assert conn.execute("SELECT COUNT(*) c FROM samples"
                                ).fetchone()["c"] == 5 * 4 * N
        finally:
            conn.close()


def test_no_lap_is_ever_deleted_however_far_over_budget():
    with tempfile.TemporaryDirectory() as d:
        conn, path = _build(Path(d))
        try:
            laps_before = conn.execute(
                "SELECT COUNT(*) c FROM laps").fetchone()["c"]
            retention.enforce_budget(conn, path, 1)   # absurdly small
            laps_after = conn.execute(
                "SELECT COUNT(*) c FROM laps").fetchone()["c"]
            assert laps_after == laps_before == 20, (laps_before, laps_after)
            sessions = conn.execute(
                "SELECT COUNT(*) c FROM sessions").fetchone()["c"]
            assert sessions == 5
            print(f"  budget of 1 byte: {laps_after} laps still there")
        finally:
            conn.close()


def test_lap_times_and_track_limits_evidence_survive_thinning():
    with tempfile.TemporaryDirectory() as d:
        conn, path = _build(Path(d))
        try:
            before = {l["id"]: (l["lap_time_ms"], l["max_tyres_out"],
                                l["excursions"], l["invalid"])
                      for l in db.list_laps(conn, limit=None)}
            retention.enforce_budget(conn, path, 1)
            after = {l["id"]: (l["lap_time_ms"], l["max_tyres_out"],
                               l["excursions"], l["invalid"])
                     for l in db.list_laps(conn, limit=None)}
            assert after == before, "thinning changed a derived fact"
        finally:
            conn.close()


def test_the_newest_session_is_never_thinned():
    """It is the one being driven, and the one about to be asked about."""
    with tempfile.TemporaryDirectory() as d:
        conn, path = _build(Path(d))
        try:
            newest = conn.execute(
                "SELECT id FROM sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            retention.enforce_budget(conn, path, 1)
            strides = [r["sample_stride"] for r in conn.execute(
                "SELECT sample_stride FROM laps WHERE session_id = ?",
                (newest,))]
            assert set(strides) == {1}, strides
            counts = [r["c"] for r in conn.execute(
                "SELECT COUNT(*) c FROM samples s JOIN laps l"
                " ON l.id = s.lap_id WHERE l.session_id = ?"
                " GROUP BY s.lap_id", (newest,))]
            assert set(counts) == {N}, counts
        finally:
            conn.close()


def test_the_oldest_sessions_are_thinned_first():
    with tempfile.TemporaryDirectory() as d:
        conn, path = _build(Path(d))
        try:
            before = retention.db_bytes(path)
            retention.enforce_budget(conn, path, int(before * 0.75))
            rows = conn.execute(
                "SELECT session_id, MAX(sample_stride) s FROM laps"
                " GROUP BY session_id ORDER BY session_id").fetchall()
            strides = [r["s"] for r in rows]
            assert strides == sorted(strides, reverse=True), strides
            assert strides[0] > 1 and strides[-1] == 1, strides
            print("  strides oldest-to-newest:", strides)
        finally:
            conn.close()


def test_a_thinned_lap_says_it_is_thinned():
    # A coarse trace that does not announce itself reads as a lap driven at
    # 3Hz, and every derived rate is then quietly wrong.
    with tempfile.TemporaryDirectory() as d:
        conn, path = _build(Path(d))
        try:
            retention.enforce_budget(conn, path, 1)
            for lap in conn.execute(
                    "SELECT id, sample_stride FROM laps").fetchall():
                kept = conn.execute(
                    "SELECT COUNT(*) c FROM samples WHERE lap_id = ?",
                    (lap["id"],)).fetchone()["c"]
                expected = -(-N // lap["sample_stride"])   # ceil
                assert kept == expected, (dict(lap), kept, expected)
        finally:
            conn.close()


def test_thinning_keeps_an_even_spread_across_the_lap():
    # Deleting by "every Nth millisecond" would bias towards wherever the
    # sampling jitter happened to land and leave holes in one part of a lap.
    with tempfile.TemporaryDirectory() as d:
        conn, path = _build(Path(d), sessions=2, laps=1)
        try:
            retention.enforce_budget(conn, path, 1)
            oldest = conn.execute(
                "SELECT id FROM laps ORDER BY id ASC LIMIT 1").fetchone()["id"]
            ts = [r["t_ms"] for r in conn.execute(
                "SELECT t_ms FROM samples WHERE lap_id = ? ORDER BY t_ms",
                (oldest,))]
            gaps = {ts[i + 1] - ts[i] for i in range(len(ts) - 1)}
            assert len(gaps) == 1, f"uneven spacing: {sorted(gaps)[:5]}"
        finally:
            conn.close()


def test_a_budget_of_zero_means_keep_everything():
    with tempfile.TemporaryDirectory() as d:
        conn, path = _build(Path(d))
        try:
            r = retention.enforce_budget(conn, path, 0)
            assert r["acted"] is False, r
            assert conn.execute("SELECT COUNT(*) c FROM samples"
                                ).fetchone()["c"] == 5 * 4 * N
        finally:
            conn.close()


def test_being_unable_to_reach_the_budget_is_said_out_loud():
    with tempfile.TemporaryDirectory() as d:
        conn, path = _build(Path(d))
        try:
            r = retention.enforce_budget(conn, path, 1)
            assert r["under_budget"] is False
            assert "never thinned" in r["note"], r
            assert "no lap is ever deleted" in r["note"], r
        finally:
            conn.close()


def test_storage_report_says_what_has_been_thinned():
    with tempfile.TemporaryDirectory() as d:
        conn, path = _build(Path(d))
        try:
            clean = retention.storage_report(conn, path)
            assert clean["laps"] == 20 and clean["laps_thinned"] == 0
            assert "note" not in clean, clean

            retention.enforce_budget(conn, path, 1)
            after = retention.storage_report(conn, path)
            assert after["laps"] == 20, "reports laps, not surviving samples"
            assert after["laps_thinned"] > 0
            assert (after["laps_full_resolution"] + after["laps_thinned"]
                    == 20)
            assert "resolution" in after["note"]
            print("  ", after["size"], after["note"][:60] + "...")
        finally:
            conn.close()


def test_the_budget_can_be_set_from_the_environment():
    import os
    for var in ("ASSETTO_MCP_MAX_DB_BYTES", "AC_ENGINEER_MAX_DB_BYTES"):
        os.environ.pop(var, None)
    assert retention.budget_bytes() == retention.DEFAULT_BUDGET_BYTES
    try:
        os.environ["ASSETTO_MCP_MAX_DB_BYTES"] = "12345"
        assert retention.budget_bytes() == 12345
        os.environ["ASSETTO_MCP_MAX_DB_BYTES"] = "0"
        assert retention.budget_bytes() == 0, "0 means keep everything"
        os.environ["ASSETTO_MCP_MAX_DB_BYTES"] = "not a number"
        assert retention.budget_bytes() == retention.DEFAULT_BUDGET_BYTES, \
            "a typo must not silently disable or shrink the budget"
    finally:
        os.environ.pop("ASSETTO_MCP_MAX_DB_BYTES", None)


class _FailAfter:
    """A connection that raises on the Nth DELETE, and is otherwise real.

    sqlite3.Connection forbids assigning to .execute, so the failure is
    injected through a wrapper rather than by monkeypatching.
    """

    def __init__(self, conn, fail_on):
        self._conn = conn
        self._fail_on = fail_on
        self.deletes = 0

    def execute(self, sql, *a, **k):
        if sql.lstrip().upper().startswith("DELETE FROM SAMPLES"):
            self.deletes += 1
            if self.deletes == self._fail_on:
                raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_a_failed_thin_leaves_the_lap_whole():
    """All of it or none of it.

    The deletes go out in chunks and the stride is stamped at the end, so a
    failure between them left a lap decimated and still marked stride 1 --
    and those partial deletes did not stay uncommitted, because the
    collector's next heartbeat commits (renew_recorder uses `with conn`). A
    lap that has quietly lost most of its trace while claiming full
    resolution is then eligible for re-scoring, which reads the gaps as
    clean track.
    """
    with tempfile.TemporaryDirectory() as d:
        conn, path = _build(Path(d), sessions=2, laps=1)
        try:
            lap_id = conn.execute(
                "SELECT id FROM laps ORDER BY id ASC LIMIT 1").fetchone()["id"]
            before = conn.execute(
                "SELECT COUNT(*) c FROM samples WHERE lap_id = ?",
                (lap_id,)).fetchone()["c"]

            failing = _FailAfter(conn, fail_on=2)
            try:
                retention._thin_lap(failing, lap_id, 1, 8)
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("the failure did not propagate")
            assert failing.deletes >= 2, "the test did not reach a second chunk"

            after = conn.execute(
                "SELECT COUNT(*) c FROM samples WHERE lap_id = ?",
                (lap_id,)).fetchone()["c"]
            stride = conn.execute(
                "SELECT sample_stride s FROM laps WHERE id = ?",
                (lap_id,)).fetchone()["s"]
            assert after == before, f"partial delete survived: {after}/{before}"
            assert stride == 1, stride
            print(f"  failed mid-thin: all {before} samples still there")
        finally:
            conn.close()


def test_the_stride_ladder_goes_deeper_than_eight():
    # Stopping at 8 meant every further session added an eighth of its
    # samples on top of an already over-budget file, forever.
    assert retention.STRIDE_LADDER[-1] >= 32, retention.STRIDE_LADDER
    with tempfile.TemporaryDirectory() as d:
        conn, path = _build(Path(d), sessions=4, laps=3)
        try:
            retention.enforce_budget(conn, path, 1)
            worst = conn.execute(
                "SELECT MAX(sample_stride) s FROM laps").fetchone()["s"]
            assert worst == retention.STRIDE_LADDER[-1], worst
            assert conn.execute(
                "SELECT COUNT(*) c FROM laps").fetchone()["c"] == 12
        finally:
            conn.close()


def test_the_documented_ladder_is_the_ladder():
    """storage_report's docstring is what an assistant reads to explain it.

    It said "every 2nd, 4th or 8th sample" after the ladder was extended to
    32, so the one description a user ever sees understated how coarse
    their oldest sessions could get. Docstrings do not run, so this is the
    only thing that can notice.
    """
    # Read from source rather than importing the server, which opens a
    # database and starts a bridge as a side effect of import.
    import ast
    src = (Path(__file__).resolve().parent.parent
           / "assetto_mcp" / "server.py").read_text()
    doc = next(ast.get_docstring(n) for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef)
               and n.name == "storage_report")
    coarsest = retention.STRIDE_LADDER[-1]
    ordinal = {1: "st", 2: "nd", 3: "rd"}.get(coarsest % 10
                                              if coarsest % 100 not in
                                              (11, 12, 13) else 0, "th")
    assert f"{coarsest}{ordinal}" in doc, \
        f"docstring never mentions every {coarsest}{ordinal} sample"
    # And it does not still promise the old floor as the last step.
    assert "or 8th sample" not in doc, doc
    print(f"  docstring names every {coarsest}{ordinal} sample")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
