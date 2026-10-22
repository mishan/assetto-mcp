"""Serve the pit wall on recorded laps, with one of them replayed as the game.

For screenshots and demos without the game running. Works on a copy of the
database, so the recording is never touched, and tidies the copy for showing
to strangers: setup names become "setup 1", "setup 2", newest first, and
presses stored with no session are dropped.

    python scripts/pit_wall_demo.py                       # latest session
    python scripts/pit_wall_demo.py --db PATH --session 41 --turn T1

The driven lap is the session's second-best clean lap, compared on the map
against the best. Opponents on the overlay map are made up: five names, at
fixed gaps along the best lap's line. Prints the pit wall's address, then
runs until stopped. scripts/shoot_pit_wall.sh takes the README's pictures
from it.
"""

import argparse
import bisect
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assetto_mcp import bridge, db  # noqa: E402

NAMES = ["A. Moreau", "K. Tanaka", "R. Silva", "J. Becker", "L. Okafor"]
# Each opponent's place on the lap, as a fraction of it ahead of the driver.
GAPS = [0.31, 0.09, -0.06, -0.22, -0.48]
WHEELS = ("fl", "fr", "rl", "rr")


def fmt(ms: int) -> str:
    return f"{ms // 60000}:{ms % 60000 / 1000:06.3f}"


def tidy_copy(src: Path, dst: Path) -> None:
    shutil.copy(src, dst)
    db.connect(dst).close()  # brings an old copy up to the current schema
    conn = sqlite3.connect(dst)
    with conn:
        names = [r[0] for r in conn.execute(
            "SELECT setup_name FROM laps WHERE setup_name != ''"
            " GROUP BY setup_name ORDER BY MAX(id) DESC")]
        for i, name in enumerate(names, 1):
            for table in ("laps", "sessions"):
                conn.execute(f"UPDATE {table} SET setup_name = ?"
                             " WHERE setup_name = ?", (f"setup {i}", name))
        conn.execute("DELETE FROM notes WHERE session_id IS NULL")
    conn.close()


class Recording:
    """Stands in for the collector, recording the demo session."""
    running, status, last_error = True, "recording", None

    def __init__(self, session_id, laps):
        self.session_id, self.laps_recorded = session_id, laps


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path,
                    default=Path.home() / ".assetto-mcp" / "telemetry.db")
    ap.add_argument("--session", type=int, help="default: the latest")
    ap.add_argument("--turn", help="zoom the map to this turn, e.g. T1")
    ap.add_argument("--port", type=int, default=19666)
    a = ap.parse_args()

    path = Path(tempfile.mkdtemp()) / "telemetry.db"
    tidy_copy(a.db, path)
    conn = db.connect(path)
    sid = a.session or db.latest_session(conn)["id"]
    s = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    laps = conn.execute(
        f"SELECT id, lap_number, lap_time_ms FROM laps WHERE session_id = ?"
        f" AND {db.USABLE_LAP_SQL} AND invalid = 0 ORDER BY lap_time_ms",
        (sid,)).fetchall()
    if len(laps) < 2:
        sys.exit(f"session {sid} has fewer than two clean laps")
    best, driven = laps[0], laps[1]
    last = conn.execute("SELECT lap_time_ms FROM laps WHERE session_id = ?"
                        " ORDER BY id DESC LIMIT 1", (sid,)).fetchone()[0]
    n_laps = conn.execute("SELECT COUNT(*) FROM laps WHERE session_id = ?",
                          (sid,)).fetchone()[0]

    def samples(lap_id):
        return conn.execute(
            "SELECT t_ms, norm_pos, pos_x, pos_z, core_fl, core_fr, core_rl,"
            " core_rr, press_fl, press_fr, press_rl, press_rr FROM samples"
            " WHERE lap_id = ? ORDER BY t_ms", (lap_id,)).fetchall()

    drv, ref = samples(driven["id"]), samples(best["id"])
    conn.close()
    drv_t, ref_pos = [r[0] for r in drv], [r[1] for r in ref]
    lap_ms = drv[-1][0]
    t0 = time.monotonic() - 45  # mid-lap from the start

    def on_ref(pos):
        return ref[min(bisect.bisect_left(ref_pos, pos % 1.0), len(ref) - 1)]

    def now():
        t = int((time.monotonic() - t0) * 1000) % lap_ms
        return t, drv[min(bisect.bisect_left(drv_t, t), len(drv) - 1)]

    def live():
        t, r = now()
        return {
            "car": s["car"], "track": s["track"], "status": "live",
            "session_type": "race", "position": 3,
            "current_lap": 8, "race_laps": 15, "laps_remaining": 8,
            "session_time_left_s": None,
            "tyre_compound": s["tyre_compound"] or "",
            "air_temp": s["air_temp"], "road_temp": s["road_temp"],
            "fuel_l": 61.4,
            "fuel_to_finish": {"verdict": "enough", "margin_laps": 1.4,
                               "basis": "measured"},
            "lap_time": fmt(t), "lap_time_ms": t,
            "last_lap": fmt(last), "best_lap": fmt(best["lap_time_ms"]),
            "delta_to_best_s": round((t - on_ref(r[1])[0]) / 1000, 2),
            "tyre_pressures_psi": {w: round(r[8 + i], 1)
                                   for i, w in enumerate(WHEELS)},
            "tyre_core_temps_c": {w: round(r[4 + i], 1)
                                  for i, w in enumerate(WHEELS)},
            "car_x": r[2], "car_z": r[3],
        }

    b = bridge.Bridge(path, Recording(sid, n_laps), a.port)
    b.live = live
    b.start()
    b._dash_db(db.set_pit_wall_focus, [best["id"], driven["id"]], a.turn,
               f"Lap {driven['lap_number']} against your best, lap"
               f" {best['lap_number']}: where the time went")
    print(f"http://127.0.0.1:{b.port}/dash", flush=True)
    # shotbox stops a session with SIGTERM; the copy still gets removed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    drivers = [{"car_index": i + 1, "driver_name": n}
               for i, n in enumerate(NAMES)]
    try:
        while True:
            pos = now()[1][1]
            b.note_cars(drivers, [
                {"car_index": i + 1, "pos_x": on_ref(pos + g)[2],
                 "pos_z": on_ref(pos + g)[3]} for i, g in enumerate(GAPS)])
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        b.stop()
        shutil.rmtree(path.parent, ignore_errors=True)


if __name__ == "__main__":
    main()
