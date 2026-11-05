"""Tread temperature across each tyre, and camber while driving.

The core temperature says how hot a tyre is; the spread across its tread
says why, and running camber says what the tyre actually meets in a corner.
AC publishes both in one frame for the whole car, so one side comes out
mirrored; these tests hold the reader to the setup screen's convention.
"""

import math
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module, temp_db  # noqa: E402

from telemetrist import analysis, db  # noqa: E402
from telemetrist.sims import ac  # noqa: E402

WHEELS = ("fl", "fr", "rl", "rr")


def _physics(camber_deg=(-1.2, 1.3, -1.5, 1.5),
             temp_i=(66.0, 75.0, 72.0, 78.0),
             temp_m=(67.0, 74.0, 74.0, 77.0),
             temp_o=(70.0, 72.0, 77.0, 74.0)):
    """Raw pages as the game publishes them: camber mirrored on the right,
    the left wheels' hot inner edge in tyreTempO."""
    return SimpleNamespace(camberRAD=[math.radians(c) for c in camber_deg],
                           tyreTempI=list(temp_i), tyreTempM=list(temp_m),
                           tyreTempO=list(temp_o))


def test_every_wheel_reads_camber_the_way_the_setup_screen_does():
    """Negative camber on all four reads negative on all four.

    The game's camberRAD came out about -1.2 deg on the left and +1.3 on
    the right on a car set to -1.0 everywhere; turning the right side's
    sign is what makes a number comparable with the setup.
    """
    cam = ac._camber(_physics())
    assert [round(c, 2) for c in cam] == [-1.2, -1.3, -1.5, -1.5], cam


def test_the_inner_edge_is_the_inner_edge_on_both_sides():
    """With negative camber everywhere, every tyre's inner edge is hottest.

    The game's tyreTempI was the hotter edge on the right wheels and
    tyreTempO on the left; read naively, the left tyres would look as if
    they had too little camber and the right ones too much.
    """
    s = ac._surface(_physics())
    by_wheel = {w: s[3 * i:3 * i + 3] for i, w in enumerate(WHEELS)}
    assert by_wheel["fl"] == (70.0, 67.0, 66.0), by_wheel
    assert by_wheel["fr"] == (75.0, 74.0, 72.0), by_wheel
    assert all(inner > outer for inner, _, outer in by_wheel.values())


def test_a_game_without_the_channels_records_nothing_rather_than_zeroes():
    bare = SimpleNamespace()
    assert ac._surface(bare) is None and ac._camber(bare) is None
    short = SimpleNamespace(camberRAD=[0.0, 0.0], tyreTempI=[1.0] * 4,
                            tyreTempM=[1.0] * 4, tyreTempO=[1.0] * 2)
    assert ac._surface(short) is None and ac._camber(short) is None


def _sample(t_ms, lat, speed=150.0, camber=(-1.0, -1.0, -0.8, -0.8),
            tread=None):
    """A full-width sample row: zero for every older channel."""
    row = dict.fromkeys(db.SAMPLE_COLUMNS[1:], 0.0)
    row.update(t_ms=t_ms, norm_pos=t_ms / 100000, speed_kmh=speed,
               acc_lat=lat)
    for w, c in zip(WHEELS, camber):
        row[f"camber_{w}"] = c
    for w in WHEELS:
        i, m, o = (tread or {}).get(w, (80.0, 78.0, 74.0))
        row.update({f"surf_in_{w}": i, f"surf_mid_{w}": m,
                    f"surf_out_{w}": o})
    return row


def test_the_loaded_camber_is_the_outside_wheel_in_each_direction():
    """Lateral g is positive in a left-hander: the right wheels carry it.

    A front-left that runs -1.0 in a straight line and +0.5 when loaded in
    right-handers is losing 1.5 deg to roll -- the number a camber change
    is set against.
    """
    rows = ([_sample(t, 0.0) for t in range(0, 1000, 40)]
            + [_sample(t, -1.2, camber=(0.5, -2.0, 0.2, -1.1))
               for t in range(1000, 2000, 40)]
            + [_sample(t, 1.2, camber=(-2.2, 0.4, -1.3, 0.1))
               for t in range(2000, 3000, 40)])
    got = {w: analysis.tread_and_camber(rows, w)["camber_deg"]
           for w in WHEELS}
    assert got["fl"]["straight"] == -1.0 and got["fl"]["loaded"] == 0.5, got
    assert got["fr"]["loaded"] == 0.4 and got["rr"]["loaded"] == 0.1, got
    assert got["rl"]["loaded"] == 0.2, got
    assert all(g["loaded_s"] == 1.0 for g in got.values()), got


def test_the_tread_spread_says_pressure_or_camber():
    hot_middle = {"fl": (80.0, 90.0, 78.0)}
    hot_inside = {"fl": (95.0, 85.0, 75.0)}
    a = analysis.tread_and_camber([_sample(0, 0.0, tread=hot_middle)], "fl")
    b = analysis.tread_and_camber([_sample(0, 0.0, tread=hot_inside)], "fl")
    assert a["middle_minus_edges_c"] == 11.0 and a["inner_minus_outer_c"] == 2.0
    assert b["inner_minus_outer_c"] == 20.0 and b["middle_minus_edges_c"] == 0.0
    assert a["tread_c"] == {"inner": 80.0, "middle": 90.0, "outer": 78.0}


def test_a_lap_from_before_v22_leaves_the_keys_out():
    rows = [_sample(t, 0.0) for t in range(0, 400, 40)]
    for r in rows:
        for c in db.SURFACE_CAMBER_COLUMNS:
            r[c] = None
    assert analysis.tread_and_camber(rows, "fl") == {}


def test_a_stored_lap_reports_tread_and_camber_in_its_summary():
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            rows = [_sample(t, 0.0) for t in range(0, 4000, 40)]
            lap = db.store_lap(conn, sid, 1, 90000, True,
                               [tuple(r[c] for c in db.SAMPLE_COLUMNS[1:])
                                for r in rows])
            summary = analysis.lap_summary(db.get_lap(conn, lap),
                                           db.get_samples(conn, lap))
            fl = summary["tyres"]["fl"]
            assert fl["tread_c"] == {"inner": 80.0, "middle": 78.0,
                                     "outer": 74.0}, fl
            assert fl["camber_deg"]["straight"] == -1.0, fl
        finally:
            conn.close()


def test_v22_adds_the_columns_and_leaves_old_laps_unrecorded():
    """A v21 file gains the columns; its laps read as not recorded."""
    old_cols = [c for c in db.SAMPLE_COLUMNS
                if c not in db.SURFACE_CAMBER_COLUMNS]
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            lap = db.store_lap(conn, sid, 1, 90000, True,
                               [(0,) + (0.0,) * 37])
            conn.executescript(f"""
                CREATE TABLE samples_v21 AS
                    SELECT {", ".join(old_cols)} FROM samples;
                DROP TABLE samples;
                ALTER TABLE samples_v21 RENAME TO samples;
                PRAGMA user_version = 21;
            """)
        finally:
            conn.close()
        conn = db.connect(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == \
                db.SCHEMA_VERSION
            (old,) = db.get_samples(conn, lap)
            assert all(old[c] is None for c in db.SURFACE_CAMBER_COLUMNS)
        finally:
            conn.close()


def test_the_recorded_lap_reads_negative_camber_on_every_wheel():
    """Pinned against the game, not against the hypothesis: the recorded
    911 RSR was set to negative camber all round, and in a straight line
    every wheel must read negative -- the right side included, which the
    game publishes with its sign turned."""
    import ctypes
    from statistics import mean
    from telemetrist.sim_info import SPageFilePhysics
    from telemetrist.sims import recording
    path = (Path(__file__).resolve().parent / "recordings"
            / "ac_911rsr_thompson.simrec")
    _, ticks = recording.read(path)
    straight = [[] for _ in WHEELS]
    for _, state in ticks:
        raw = state.get("physics")
        if not raw:
            continue
        p = SPageFilePhysics.from_buffer_copy(
            raw[:ctypes.sizeof(SPageFilePhysics)])
        if p.speedKmh < 60 or abs(p.accG[0]) > 0.2:
            continue
        for i, c in enumerate(ac._camber(p)):
            straight[i].append(c)
    got = [round(mean(c), 2) for c in straight]
    assert all(-1.7 < c < -1.0 for c in got), got
    print(f"  straight-line camber per wheel: {got}")


def test_the_new_channels_can_be_traced_through_a_corner():
    """Lap averages say whether a tyre is unhappy; where on the lap, and
    in which corner, needs the trace."""
    import json
    import os
    from support import lasting_dir
    d = lasting_dir("ac-tread-")
    os.environ.setdefault("TELEMETRIST_DATA", d)
    os.environ.setdefault("TELEMETRIST_BRIDGE_PORT", "0")
    os.environ.setdefault("TELEMETRIST_NO_AUTOSTART", "1")
    import importlib
    s = importlib.import_module("telemetrist.server")
    sid = make_session(s._conn)
    s._conn.execute("UPDATE sessions SET track_length_m = 2000 WHERE id = ?",
                    (sid,))
    s._conn.commit()
    rows = [_sample(t, 0.0) for t in range(0, 4000, 40)]
    lap = db.store_lap(s._conn, sid, 1, 90000, True,
                       [tuple(r[c] for c in db.SAMPLE_COLUMNS[1:])
                        for r in rows])
    for c in ("camber_fl", "surf_in_fl", "surf_mid_rr", "surf_out_fr"):
        assert c in s.TRACE_CHANNELS, c
    out = json.loads(s.lap_trace(lap, channels=["camber_fl", "surf_in_fl"]))
    assert out["rows"][0]["camber_fl"] == -1.0, out
    print("  camber and tread temperature come out of lap_trace")


def test_the_loaded_tread_spread_is_not_cancelled_by_the_other_way():
    """A front-left that cooks its outer edge in right-handers -- where it
    is loaded -- and its inner edge in left-handers averages to a flat
    spread over the lap. Read where the wheel carries the corner, it
    shows."""
    rights = [_sample(t, -1.2, tread={"fl": (75.0, 80.0, 95.0)})
              for t in range(0, 1000, 40)]
    lefts = [_sample(t, 1.2, tread={"fl": (95.0, 80.0, 75.0)})
             for t in range(1000, 2000, 40)]
    out = analysis.tread_and_camber(rights + lefts, "fl")
    assert out["inner_minus_outer_c"] == 0.0, out
    assert out["loaded_inner_minus_outer_c"] == -20.0, out
    print("  loaded spread -20 C where the lap average reads 0")


def test_loaded_time_survives_thinning_and_leaves_out_gaps():
    """Retention keeps every Nth sample of old laps; a count of loaded
    samples would fall N-fold, time in the loaded state does not."""
    full = [_sample(t, -1.2) for t in range(0, 2000, 40)]
    thinned = full[::8]
    a = analysis.tread_and_camber(full, "fl")["camber_deg"]
    b = analysis.tread_and_camber(thinned, "fl")["camber_deg"]
    assert abs(a["loaded_s"] - b["loaded_s"]) <= 0.3, (a, b)
    assert a["loaded_s"] >= 1.9, a
    # Ten seconds the recording missed are not ten seconds loaded.
    gap = ([_sample(t, 0.0) for t in range(0, 1000, 40)]
           + [_sample(t, -1.2) for t in range(11000, 12000, 40)])
    c = analysis.tread_and_camber(gap, "fl")["camber_deg"]
    assert c["loaded_s"] <= 1.1, c
    print(f"  loaded {a['loaded_s']} s full, {b['loaded_s']} s thinned")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
