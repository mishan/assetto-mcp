"""Re-derive every stored lap's track-limits verdict from its samples.

Run it after changing TRACK_LIMITS_WHEELS or MIN_EXCURSION_MS in
assetto_mcp/db.py. It reads the constants from the code on disk, so it
needs no server restart, and it is rare enough to be typed rather than
offered to a model as a tool.

    python scripts/rescore_track_limits.py

Nothing is deleted, and a verdict that came from the game is never
touched. Laps whose traces retention has thinned are skipped and keep the
verdict measured at full resolution.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assetto_mcp import config, db  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(config.data_dir() / "telemetry.db"))
    args = p.parse_args(argv)

    if not Path(args.db).exists():
        print(f"no database at {args.db}", file=sys.stderr)
        return 1
    conn = db.connect(args.db)
    try:
        changed = db.backfill_excursions(conn)
        flagged = db.backfill_outliers(conn)
        skipped = conn.execute(
            "SELECT COUNT(*) c FROM laps WHERE sample_stride > 1"
            " AND invalid_source = 'inferred'").fetchone()["c"]
    finally:
        conn.close()

    print(f"{args.db}\n")
    print(f"threshold: {db.TRACK_LIMITS_WHEELS} wheels off for "
          f"{db.MIN_EXCURSION_MS} ms")
    print(f"re-scored {changed} lap(s), re-flagged {flagged} outlier(s)")
    if skipped:
        print(f"{skipped} lap(s) were NOT re-scored: retention has thinned "
              f"their traces, so they keep the verdict measured at full "
              f"resolution and a changed threshold does not reach them")
    print("Nothing was deleted. A lap that no longer counts as running wide "
          "is readable and comparable again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
