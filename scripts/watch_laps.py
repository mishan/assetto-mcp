"""Print one line each time the collector stores a lap.

The collector writes laps to the database as they complete and says nothing
about it. Anything that wants to react lap by lap -- an engineer's chat
session, a shell pipeline, a notification hook -- has to poll for them.
This is that poll, done once, with the database opened read-only so it can
run alongside a recording session without touching it.

    python scripts/watch_laps.py                  # laps of the active session
    python scripts/watch_laps.py --session 43     # one session, and later ones
    python scripts/watch_laps.py --replay         # print what is already stored first
    python scripts/watch_laps.py --interval 1     # poll every second (default 3)

Each line is one lap, space-separated key=value, in the order laps are
stored:

    LAP id=265 session=41 n=6 time=1:33.651 wide=0 out=0 pit=0

`wide` is the inferred track-limits verdict, `out` an out-lap, `pit` a lap
with a pit visit in it. Lines are flushed as they are printed, so the output
can be piped. Errors go to the same stream prefixed ERR, and the watch keeps
going -- a locked database during a write is the usual one, and it clears.

Stop it with Ctrl-C.
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assetto_mcp import config  # noqa: E402

# Resolved by the same code the server uses, so this follows both a moved
# data dir and a pre-rename one.
DB_PATH = config.data_dir() / "telemetry.db"

QUERY = (
    "select id, session_id, lap_number, lap_time_ms, invalid, out_lap, pitted "
    "from laps where session_id >= ? order by id"
)


def fmt(ms):
    if ms is None:
        return "-"
    return "%d:%02d.%03d" % (ms // 60000, ms % 60000 // 1000, ms % 1000)


def line(row):
    lap_id, session_id, n, ms, invalid, out_lap, pitted = row
    return (
        f"LAP id={lap_id} session={session_id} n={n} time={fmt(ms)} "
        f"wide={int(bool(invalid))} out={int(bool(out_lap))} pit={int(bool(pitted))}"
    )


def open_db(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    return conn


def active_session(conn):
    row = conn.execute("select max(id) from sessions").fetchone()
    return row[0] or 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--session", type=int, help="watch this session and any later one (default: the newest)")
    p.add_argument("--replay", action="store_true", help="print laps already stored before waiting for new ones")
    p.add_argument("--interval", type=float, default=3.0, help="seconds between polls (default 3)")
    p.add_argument("--db", default=str(DB_PATH))
    args = p.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"no database at {db_path}", file=sys.stderr)
        return 1

    conn = open_db(db_path)
    since = args.session if args.session is not None else active_session(conn)
    seen = set()
    if not args.replay:
        seen = {r[0] for r in conn.execute(QUERY, (since,))}
    conn.close()

    try:
        while True:
            try:
                conn = open_db(db_path)
                rows = conn.execute(QUERY, (since,)).fetchall()
                conn.close()
            except sqlite3.Error as e:
                print(f"ERR {e}", flush=True)
                time.sleep(max(args.interval, 5.0))
                continue
            for row in rows:
                if row[0] not in seen:
                    seen.add(row[0])
                    print(line(row), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
