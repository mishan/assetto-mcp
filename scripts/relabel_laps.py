"""Correct the setup_name on laps that were labelled wrong.

set_session_setup deliberately refuses to relabel a lap that already
carries a different setup name, because a late correction applied to the
wrong half of an A/B split silently destroys the comparison it exists to
enable. That guard is right whenever the existing label came from the
driver.

It is wrong in exactly one case: the label was written by mistake in the
first place. set_session_setup applies to "laps completed from now on" as
well as backfilling blanks, so calling it *before* the driver loads the
new setup stamps the old name onto the run that follows. That is how laps
87-90 came to be labelled claude_toe_v1 while running claude_press_v1.

This is the escape hatch, kept out of the MCP surface on purpose: it is
rare, it is destructive, and it should require someone to type it.

    python scripts/relabel_laps.py 87,88,89,90 claude_press_v1
    python scripts/relabel_laps.py 87,88,89,90 claude_press_v1 --apply

Without --apply it only shows what it would do.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Same resolution order as server.py, so this follows a moved data dir.
DATA_DIR = Path(os.environ.get(
    "AC_ENGINEER_DATA", Path.home() / ".ac-race-engineer"))
DB_PATH = DATA_DIR / "telemetry.db"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("lap_ids", help="comma-separated lap ids, e.g. 87,88,89,90")
    p.add_argument("setup_name", help="the name these laps were actually run on")
    p.add_argument("--apply", action="store_true",
                   help="write the change; without this it is a dry run")
    p.add_argument("--db", default=str(DB_PATH))
    args = p.parse_args(argv)

    try:
        lap_ids = [int(x) for x in args.lap_ids.split(",") if x.strip()]
    except ValueError:
        p.error(f"lap ids must be integers: {args.lap_ids!r}")
    if not lap_ids:
        p.error("no lap ids given")

    if not Path(args.db).exists():
        print(f"no database at {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" * len(lap_ids))
        rows = conn.execute(
            f"SELECT id, session_id, lap_number, lap_time_ms, valid, "
            f"setup_name FROM laps WHERE id IN ({placeholders}) ORDER BY id",
            lap_ids).fetchall()

        found = {r["id"] for r in rows}
        missing = [i for i in lap_ids if i not in found]
        if missing:
            print(f"no such lap(s): {missing}", file=sys.stderr)
            return 1

        # A lap belonging to a different session than the rest is the shape
        # of a typo in the lap list, and relabelling it would be the exact
        # damage the guard in set_session_setup exists to prevent.
        sessions = {r["session_id"] for r in rows}
        if len(sessions) > 1:
            print(f"refusing: laps span sessions {sorted(sessions)}. Run one "
                  f"session at a time.", file=sys.stderr)
            return 1

        print(f"{args.db}\n")
        changing = 0
        for r in rows:
            was = r["setup_name"] or "(none)"
            mark = " " if was == args.setup_name else "*"
            if mark == "*":
                changing += 1
            secs = r["lap_time_ms"] / 1000.0
            print(f" {mark} lap {r['id']:>4}  session {r['session_id']}  "
                  f"#{r['lap_number']:<3} {secs:8.3f}s  "
                  f"{'valid' if r['valid'] else 'INVALID':<7}  "
                  f"{was}  ->  {args.setup_name}")

        if not changing:
            print(f"\nall {len(rows)} lap(s) already read {args.setup_name}; "
                  f"nothing to do")
            return 0

        if not args.apply:
            print(f"\ndry run: {changing} of {len(rows)} lap(s) would change. "
                  f"Re-run with --apply to write.")
            return 0

        with conn:
            conn.execute(
                f"UPDATE laps SET setup_name = ? WHERE id IN ({placeholders})",
                [args.setup_name] + lap_ids)
        print(f"\nrelabelled {changing} lap(s) to {args.setup_name}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
