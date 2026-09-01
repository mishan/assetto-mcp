"""Keeping the database bounded without throwing laps away.

A lap costs about 0.4 MB, almost all of it the 25Hz sample rows: roughly
12 MB per hour of driving, so a few hundred hours is a few gigabytes sitting
in the driver's home directory forever.

The obvious answer -- delete old sessions -- is the wrong one here. The
whole point of storing laps is that a reference lap from three months ago is
still the thing a change is being measured against, and a driver who comes
back to a circuit after a season away wants the run they did last time.

So nothing is deleted. Instead the oldest sessions are **thinned**: their
samples are decimated, keeping every Nth, and the stride is recorded on the
lap so a coarse trace can say it is coarse rather than reading as a lap
driven at 5Hz. Lap rows, lap times, setup attribution and the track-limits
evidence are computed at full resolution before any thinning and are never
touched, so what a thinned lap loses is trace detail, not its existence.

`sample_stride` is load-bearing for that promise, not decoration:
db.backfill_excursions refuses to re-score a lap whose stride is above 1,
because recomputing from a decimated trace would replace a real measurement
with a worse one -- at stride 8 an excursion under about 640ms disappears
and the lap becomes permanently "clean".

Order of sacrifice, worst-value-per-byte first:
  1. rival samples (other cars, only useful during the session they were in)
  2. suspension samples (already capped per session, and huge at 333Hz)
  3. car samples, oldest sessions first, by progressively coarser stride
"""

import os
import sqlite3

# Stride ladder. A lap is thinned one step at a time, so a session is only
# taken to 1-in-16 once every older session is already there.
#
# The ladder has a floor, and that floor is why the budget is a target
# rather than a guarantee: lap rows and their derived facts are never
# deleted, so a database of nothing but fully-thinned laps still grows,
# slowly, forever. Stopping at 8 made that bite sooner than it needed to --
# every further session added an eighth of its samples on top of an already
# over-budget file. 32 is about 0.8Hz, which is still enough to place a lap
# on the circuit and see roughly where the driver lifted, and it buys a
# factor of four before the floor is real. Below that a trace answers
# nothing and is not worth the bytes it still costs.
STRIDE_LADDER = (2, 4, 8, 16, 32)

DEFAULT_BUDGET_BYTES = 2 * 1024 ** 3  # 2 GB

# Thinning rewrites and then reclaims, so the file has to be over budget by
# enough to be worth a VACUUM. Without this, every session start on a
# database sitting just over the line would do a full rewrite.
BUDGET_SLACK = 0.05


def budget_bytes() -> int:
    """The size the database is allowed to reach, from the environment."""
    from . import config
    raw = config.env("MAX_DB_BYTES")
    if not raw:
        return DEFAULT_BUDGET_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_BYTES
    # 0 or negative disables thinning entirely -- a deliberate "I have the
    # disk, keep everything" rather than a typo that silently wipes traces.
    return value if value > 0 else 0


def db_bytes(db_path) -> int:
    """Size of the database including its write-ahead log."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(str(db_path) + suffix)
        except OSError:
            pass
    return total


def storage_report(conn, db_path) -> dict:
    """What the database holds and what it costs."""
    def scalar(sql, *args):
        row = conn.execute(sql, args).fetchone()
        return (row[0] if row and row[0] is not None else 0)

    total = db_bytes(db_path)
    budget = budget_bytes()
    laps = scalar("SELECT COUNT(*) FROM laps")
    thinned = scalar("SELECT COUNT(*) FROM laps WHERE sample_stride > 1")
    report = {
        "path": str(db_path),
        "bytes": total,
        "size": _human(total),
        "budget": _human(budget) if budget else "unlimited",
        "sessions": scalar("SELECT COUNT(*) FROM sessions"),
        "laps": laps,
        "laps_full_resolution": laps - thinned,
        "laps_thinned": thinned,
        "rows": {
            "samples": scalar("SELECT COUNT(*) FROM samples"),
            "suspension_samples":
                scalar("SELECT COUNT(*) FROM suspension_samples"),
            "rival_samples": scalar("SELECT COUNT(*) FROM rival_samples"),
        },
    }
    if budget:
        report["percent_of_budget"] = round(100.0 * total / budget, 1)
    if thinned:
        report["note"] = (
            f"{thinned} lap(s) have had their traces decimated to stay "
            f"within the budget. Their lap times, setups and track-limits "
            f"evidence are unaffected; only the sample resolution is lower. "
            f"Raise ASSETTO_MCP_MAX_DB_BYTES to stop thinning more.")
    return report


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def _thin_lap(conn, lap_id: int, current: int, target: int) -> int:
    """Decimate one lap's samples to `target` stride. Returns rows removed.

    Keeps samples whose index is a multiple of the step, counting in
    recorded order. Deleting by `t_ms % k` instead would bias towards
    whatever the sampling jitter happened to land on, and would delete
    unevenly across a lap where the tick rate wobbled.
    """
    if target <= current:
        return 0
    step = target // current
    rows = [r[0] for r in conn.execute(
        "SELECT rowid FROM samples WHERE lap_id = ? ORDER BY t_ms",
        (lap_id,))]
    doomed = [r for i, r in enumerate(rows) if i % step]

    # All of it, or none of it. The deletes go out in chunks and the stride
    # is stamped at the end, so a failure in between left a lap decimated
    # and still marked stride 1 -- and those partial deletes did not even
    # stay uncommitted, because the caller's next heartbeat commits (
    # renew_recorder uses `with conn`). A lap that has quietly lost seven
    # eighths of its trace while claiming full resolution is then eligible
    # for re-scoring, which reads the gaps as clean track.
    conn.execute("SAVEPOINT thin_lap")
    try:
        for i in range(0, len(doomed), 500):
            chunk = doomed[i:i + 500]
            conn.execute(
                "DELETE FROM samples WHERE rowid IN (%s)"
                % ",".join("?" * len(chunk)), chunk)
        # Stamped even when nothing was removed -- a lap of 0 or 1 samples
        # would otherwise be re-queried by every future pass forever.
        conn.execute("UPDATE laps SET sample_stride = ? WHERE id = ?",
                     (target, lap_id))
    except Exception:
        conn.execute("ROLLBACK TO thin_lap")
        raise
    finally:
        conn.execute("RELEASE thin_lap")
    return len(doomed)


def enforce_budget(conn, db_path, budget: int | None = None) -> dict:
    """Bring the database back under budget. Returns what it did.

    Never deletes a lap, a session, or anything computed from them. Runs in
    stages and stops as soon as it is under, so an occasional big session
    costs one cheap pass rather than a full rewrite of everything.
    """
    if budget is None:
        budget = budget_bytes()
    actions: list[str] = []
    before = db_bytes(db_path)
    if not budget or before <= budget * (1 + BUDGET_SLACK):
        return {"acted": False, "bytes": before, "size": _human(before),
                "budget": _human(budget) if budget else "unlimited"}

    # Progress is *estimated* from rows removed rather than measured by
    # stat()ing the file. Deleted pages sit on the freelist and in the WAL
    # until a VACUUM, so the file does not shrink as we go: an implementation
    # that re-measured after each step read "no progress" every time and ran
    # to the bottom of the ladder however little it needed to remove. And
    # VACUUMing after each step to get an honest number would rewrite the
    # whole database once per rung.
    per_row = _bytes_per_sample_row(conn, before)
    to_free = before - budget
    freed = 0

    def enough() -> bool:
        return freed >= to_free

    # 1. Rival samples from every session but the newest. They answer "how
    # did I compare to the car ahead" and only while that race is the one
    # being discussed; nobody comes back to them a month later.
    # The protected session is the newest by the same ordering the
    # thinning uses. Picking MAX(id) here while ordering by started_at
    # below would protect one session and thin from the other end.
    newest = conn.execute(
        "SELECT id FROM sessions ORDER BY started_at DESC, id DESC"
        " LIMIT 1").fetchone()
    newest_id = newest["id"] if newest else -1
    cur = conn.execute("DELETE FROM rival_samples WHERE session_id != ?",
                       (newest_id,))
    if cur.rowcount > 0:
        freed += cur.rowcount * per_row
        actions.append(f"dropped {cur.rowcount} rival sample(s) from older "
                       f"sessions")

    # 2. Suspension samples outside the newest session. Already capped at 20
    # laps per session, but at 333Hz those are the heaviest rows here.
    if not enough():
        cur = conn.execute(
            "DELETE FROM suspension_samples WHERE session_id != ?",
            (newest_id,))
        if cur.rowcount > 0:
            freed += cur.rowcount * per_row
            actions.append(f"dropped {cur.rowcount} suspension sample(s) "
                           f"from older sessions")

    # 3. Thin car samples, oldest session first, one stride step at a time,
    # so a session only goes to 1-in-8 once every older one is already there.
    thinned = set()
    removed = 0
    for target in STRIDE_LADDER:
        if enough():
            break
        laps = conn.execute(
            "SELECT laps.id, laps.sample_stride FROM laps"
            " JOIN sessions ON sessions.id = laps.session_id"
            " WHERE laps.sample_stride < ? AND laps.session_id != ?"
            " ORDER BY sessions.started_at ASC, laps.id ASC",
            (target, newest_id)).fetchall()
        for row in laps:
            gone = _thin_lap(conn, row["id"], row["sample_stride"], target)
            if gone:
                removed += gone
                freed += gone * per_row
                thinned.add(row["id"])
            if enough():
                break
    if thinned:
        actions.append(f"thinned {len(thinned)} lap trace(s), removing "
                       f"{removed} sample(s); lap times, setups and "
                       f"track-limits evidence kept")

    conn.commit()
    if actions:
        _reclaim(conn, actions)
    after = db_bytes(db_path)
    out = {
        "acted": bool(actions),
        "actions": actions,
        "bytes_before": before,
        "bytes": after,
        "size": _human(after),
        "budget": _human(budget),
        "under_budget": after <= budget,
    }
    if not out["under_budget"]:
        # Reaching the floor of the ladder without getting under is not a
        # failure to be silent about: the budget is smaller than the data
        # that is deliberately never thinned, and the answer is the driver's
        # to make. (It can also mean the per-row estimate was pessimistic
        # and the next pass will finish the job, which is harmless.)
        out["note"] = (
            "Still over budget after thinning everything eligible. The "
            "current session's traces are never thinned, and no lap is ever "
            "deleted, so this is the floor. Raise ASSETTO_MCP_MAX_DB_BYTES, "
            "or move the old database aside if you want a clean start.")
    return out


def _bytes_per_sample_row(conn, total_bytes: int) -> float:
    """How much file a sample row costs, measured rather than assumed.

    Sample rows are ~95% of the database, so attributing all of it to them
    is close enough to decide when to stop thinning, and it tracks the real
    column count instead of a constant that rots when a migration adds a
    channel. Falls back to a floor so a nearly empty database cannot make
    this zero and turn the stopping condition into "never stop".
    """
    rows = conn.execute("SELECT COUNT(*) c FROM samples").fetchone()["c"]
    if not rows:
        return 1.0
    return max(total_bytes / rows, 1.0)


def _reclaim(conn, actions: list) -> None:
    """Give the freed pages back to the filesystem.

    Both halves matter. Without VACUUM the pages sit on the freelist and the
    file never shrinks; without the WAL checkpoint the deletions sit in the
    -wal file, which counts towards the size just as much. Measuring one and
    reclaiming the other is how thinning ran to the bottom of the ladder and
    still reported no progress.
    """
    try:
        conn.execute("VACUUM")
    except sqlite3.OperationalError:
        actions.append("could not VACUUM (another connection is "
                       "mid-transaction); space is reclaimed next time")
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass
