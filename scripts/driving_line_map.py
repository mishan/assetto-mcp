"""Draw every lap of a session as a driving-line map, in one HTML file.

The same map the export_line_map tool writes, for when there is no
conversation to ask for it in. What the page shows is described in
assetto_mcp/line_map.py.

    python scripts/driving_line_map.py                      # latest session
    python scripts/driving_line_map.py --session 41
    python scripts/driving_line_map.py --laps 265,266,267 -o exports/lines.html

The database is opened read-only, so this is safe to run while a session is
recording.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assetto_mcp import config, line_map  # noqa: E402

# Resolved by the same code the server uses, so this follows both a moved
# data dir and a pre-rename one.
DB_PATH = config.data_dir() / "telemetry.db"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session", type=int, help="session id (default: latest)")
    p.add_argument("--laps", help="comma-separated lap ids instead of a session")
    p.add_argument("--every-ms", type=int, default=line_map.EVERY_MS,
                   help=f"least time between drawn points "
                        f"(default {line_map.EVERY_MS})")
    p.add_argument("-o", "--out",
                   help="output .html (default: exports/ in the data "
                        "directory, where export_line_map writes too)")
    p.add_argument("--db", default=str(DB_PATH))
    args = p.parse_args(argv)

    lap_ids = None
    if args.laps:
        try:
            lap_ids = [int(x) for x in args.laps.split(",") if x.strip()]
        except ValueError:
            p.error(f"lap ids must be integers: {args.laps!r}")
        if not lap_ids:
            p.error("no lap ids given")

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"no database at {db_path}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        data = line_map.build(conn, args.session, lap_ids,
                              every_ms=args.every_ms)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        conn.close()

    # The same folder the MCP tool writes to, rather than one beside
    # wherever this was run from.
    out = line_map.write(data, args.out or config.data_dir() / "exports"
                         / line_map.default_name(data))
    print(f"{out}  ({len(data['laps'])} laps, "
          f"{out.stat().st_size // 1024} kB)")
    for s in data["skipped"]:
        print(f"  lap {s['lap_id']} not drawn: {s['why']}")
    if data["turns_note"]:
        print(f"  {data['turns_note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
