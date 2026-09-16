"""The line map: what it draws, what it refuses, and what it leaves out.

A map is read at a glance, which makes it the easiest place to be wrong
without anyone noticing. A chord across the circuit, a "best" lap that was
an out-lap, turn numbers from another session, a setup name that closed
the page's script block -- each looks like a drawing and not like a bug.
"""

import atexit
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module, temp_db  # noqa: E402

from assetto_mcp import analysis, db, line_map, turns  # noqa: E402

_COLS = db.SAMPLE_COLUMNS[1:]

# Three corners, as (centre, half width, peak lateral g) -- enough for the
# corner detector to number something, so the turn marks have a source.
_CORNERS = ((0.2, 0.04, 2.0), (0.5, 0.05, 1.8), (0.8, 0.04, 2.2))


def _dicts(positions, step_ms=40, x0=0.0, placed=True, corners=_CORNERS):
    """Samples on a 500 m circle at the given track positions."""
    out = []
    for k, pos in enumerate(positions):
        s = {c: 0.0 for c in _COLS}
        lat = dip = 0.0
        for c_pos, half, g in corners:
            d = abs(pos - c_pos)
            if d < half:
                shape = math.cos(d / half * math.pi / 2)
                lat, dip = g * shape, 0.5 * shape
        a = pos * 2 * math.pi
        s.update(t_ms=k * step_ms, norm_pos=pos,
                 speed_kmh=220.0 * (1 - dip), gas=0.0 if dip > 0.2 else 1.0,
                 rpm=5000.0 if dip > 0.2 else 7000.0,
                 brake=0.8 if dip > 0.2 else 0.0, gear=3 if dip > 0.2 else 5,
                 acc_lat=lat, acc_lon=-1.0 if dip > 0.2 else 0.2,
                 steer=0.3 if lat else 0.0, tyres_out=0,
                 slip_fl=1.4 if lat else 0.2, slip_fr=1.4 if lat else 0.2,
                 slip_rl=0.5 if lat else 0.2, slip_rr=0.5 if lat else 0.2,
                 core_fl=80.0, core_fr=85.0, core_rl=90.0, core_rr=95.0,
                 press_fl=26.0, press_fr=27.0, press_rl=28.0, press_rr=29.0,
                 pos_x=x0 + 500.0 * math.cos(a), pos_y=0.0,
                 pos_z=500.0 * math.sin(a))
        if not placed:
            s["pos_x"] = s["pos_y"] = s["pos_z"] = None
        out.append(s)
    return out


def _lap_positions(n=2000, lead_in=0, spill=0):
    """One lap, optionally with the samples either side of the line.

    `lead_in` samples still read the last metres of the lap before, `spill`
    the first metres of the lap after -- what the collector really stores
    at each end.
    """
    return ([0.995 + 0.001 * i for i in range(lead_in)]
            + [i / n for i in range(n)]
            + [0.001 * (i + 1) for i in range(spill)])


def _store(conn, sid, number, ms, placed=True, samples=True, x0=0.0,
           corners=_CORNERS, **flags):
    rows = [tuple(s[c] for c in _COLS)
            for s in _dicts(_lap_positions(), x0=x0, placed=placed,
                            corners=corners)]
    return db.store_lap(conn, sid, number, ms, True,
                        rows if samples else [], **flags)


# --- the trace ----------------------------------------------------------


def test_a_lap_straddling_the_line_is_drawn_without_a_chord():
    """Both ends trimmed, and a lap whose first sample reads 0.999 survives.

    Cutting at the lap's highest position trims the end only. A lap that
    opens with a sample from the previous lap has its highest position
    first, and was cut down to that one sample and dropped.
    """
    line = line_map.trace(_dicts(_lap_positions(lead_in=5, spill=5)))
    assert line is not None
    p = line["p"]
    assert p[0] < 0.01 and p[-1] > 0.99, (p[:3], p[-3:])
    assert all(b >= a for a, b in zip(p, p[1:])), "the line goes backwards"
    print(f"  {len(p)} points, {p[0]} to {p[-1]}")


def test_a_lap_that_began_mid_circuit_keeps_what_was_driven():
    positions = [0.6 + 0.4 * i / 800 for i in range(800)] + [
        0.001 * (i + 1) for i in range(20)]
    line = line_map.trace(_dicts(positions))
    assert line["p"][0] >= 0.6 and line["p"][-1] > 0.99, (
        line["p"][0], line["p"][-1])


def test_points_are_thinned_by_time_not_by_count():
    """Retention already thins old laps; a stride would thin them again."""
    fresh = line_map.trace(_dicts(_lap_positions(), step_ms=40))
    thinned = line_map.trace(_dicts(_lap_positions(), step_ms=160))
    assert abs(len(fresh["p"]) - 1000) <= 1, len(fresh["p"])
    assert len(thinned["p"]) == 2000, len(thinned["p"])
    print(f"  25 Hz kept {len(fresh['p'])}, 6 Hz kept all "
          f"{len(thinned['p'])}")


def test_a_fragment_is_not_a_line():
    assert line_map.trace(_dicts([i / 1000 for i in range(20)])) is None
    assert line_map.trace(_dicts(_lap_positions(), placed=False)) is None


# --- which laps, and which is best --------------------------------------


def test_the_best_line_is_the_fastest_lap_that_counted():
    """Not an out-lap, not a pit lap, not one that ran wide."""
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            _store(conn, sid, 1, 60000, out_lap=True)
            _store(conn, sid, 2, 61000, pitted=True)
            wide = _store(conn, sid, 3, 62000)
            conn.execute("UPDATE laps SET invalid = 1 WHERE id = ?", (wide,))
            slow = _store(conn, sid, 4, 90000)
            quick = _store(conn, sid, 5, 88000)
            data = line_map.build(conn, sid)
        finally:
            conn.close()
    assert data["best"] == quick, (data["best"], quick)
    by_id = {l["id"]: l for l in data["laps"]}
    assert by_id[slow]["usable"] and by_id[wide]["wide"]
    out_lap = data["laps"][0]
    assert out_lap["out"] and not out_lap["usable"], out_lap
    assert "out-lap" in out_lap["why"], out_lap["why"]
    print(f"  best is lap {data['best']}, not the 60s out-lap")


def test_laps_without_position_are_listed_not_drawn():
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            drawn = _store(conn, sid, 1, 90000)
            old = _store(conn, sid, 2, 90000, placed=False)
            empty = _store(conn, sid, 3, 90000, samples=False)
            data = line_map.build(conn, sid)

            only_old = make_session(conn)
            _store(conn, only_old, 1, 90000, placed=False)
            try:
                line_map.build(conn, only_old)
                raise AssertionError("a session with no positions drew a map")
            except ValueError as e:
                assert "schema v8" in str(e), e
        finally:
            conn.close()
    assert [l["id"] for l in data["laps"]] == [drawn]
    why = {s["lap_id"]: s["why"] for s in data["skipped"]}
    assert "schema v8" in why[old], why
    assert "no telemetry samples" in why[empty], why


def test_laps_from_different_layouts_are_refused():
    """Their coordinates have different origins; one map would be nonsense."""
    with temp_db() as path:
        conn = db.connect(path)
        try:
            a = _store(conn, make_session(conn, track="mugello"), 1, 90000)
            b = _store(conn, make_session(conn, track="rt_suzuka"), 1, 90000)
            try:
                line_map.build(conn, lap_ids=[a, b])
                raise AssertionError("two layouts were drawn as one")
            except ValueError as e:
                assert "same car and layout" in str(e), e
            try:
                line_map.build(conn, lap_ids=[a, 999999])
                raise AssertionError("a missing lap was ignored")
            except ValueError as e:
                assert "999999" in str(e), e
        finally:
            conn.close()


def test_laps_across_sessions_draw_without_turn_numbers():
    """Each session numbers its own turns, so neither numbering is drawn."""
    with temp_db() as path:
        conn = db.connect(path)
        try:
            a = _store(conn, make_session(conn), 1, 90000)
            b = _store(conn, make_session(conn), 1, 89000, x0=3.0)
            data = line_map.build(conn, lap_ids=[b, a])
        finally:
            conn.close()
    assert [l["id"] for l in data["laps"]] == [a, b]
    assert data["turns"] == [] and "per session" in data["turns_note"]
    assert data["unnumbered"] == [] and data["detection"] is None
    # The corners are still found -- against each lap's own load -- and
    # none of them claims a turn number no session gave it.
    assert all(l["corners"] for l in data["laps"])
    assert all(c["turn"] is None for l in data["laps"] for c in l["corners"])
    assert data["air"] is None, "one session's conditions over two"
    assert line_map.default_name(data) == f"line-map-laps-{a}-{b}.html"


def test_turn_numbers_are_the_sessions_own():
    """The same numbering lap_summary gets, not a second copy of it."""
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            for n in range(1, 4):
                _store(conn, sid, n, 90000 + n)
            data = line_map.build(conn, sid)
            cmap = turns.corner_map_from(conn,
                                         turns.basis_lap_ids(conn, sid))
        finally:
            conn.close()
    assert data["turns"], "the synthetic corners were not numbered"
    assert [t["turn"] for t in data["turns"]] == [
        t["turn"] for t in cmap["turns"]]
    assert [t["apex_pos"] for t in data["turns"]] == [
        t["apex_pos"] for t in cmap["turns"]]
    assert data["turns_from_laps"] == cmap["basis_lap_ids"]
    print(f"  {len(data['turns'])} turns: "
          + ", ".join(f"{t['turn']}@{t['apex_pos']}" for t in data["turns"]))


def test_each_lap_shows_what_the_detector_saw_on_it():
    """The corners, the trace and the bar are the ones lap_summary uses."""
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            ids = [_store(conn, sid, n, 90000 + n) for n in range(1, 4)]
            data = line_map.build(conn, sid)
            cmap = turns.corner_map_from(conn,
                                         turns.basis_lap_ids(conn, sid))
            samples = db.get_samples(conn, ids[0])
        finally:
            conn.close()
    ref = cmap["reference"]["reference"]
    corners = analysis.detect_corners(samples, ref)
    analysis.label_corners(corners, cmap["turns"])
    lap = data["laps"][0]
    assert [c["turn"] for c in lap["corners"]] == [c["turn"] for c in corners]
    assert [c["apex"] for c in lap["corners"]] == [
        c["apex_pos"] for c in corners]
    assert lap["thr"] == data["detection"]["threshold_g"], (
        lap["thr"], data["detection"])
    assert len(lap["lat"]) == len(lap["p"])
    # The trace peaks inside the corner it produced, not somewhere else.
    peak = max(range(len(lap["lat"])), key=lambda i: abs(lap["lat"][i]))
    assert any(c["entry"] <= lap["p"][peak] <= c["exit"]
               for c in lap["corners"]), (lap["p"][peak], lap["corners"])
    print(f"  bar {lap['thr']} g; corners "
          + ", ".join(f"{c['turn']}@{c['apex']}" for c in lap["corners"]))


def test_a_corner_one_lap_invented_is_shown_and_not_numbered():
    """The case the view exists for: the detector saw something once."""
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            for n in range(1, 4):
                _store(conn, sid, n, 90000 + n)
            odd = _store(conn, sid, 4, 90500,
                         corners=_CORNERS + ((0.65, 0.03, 2.0),))
            data = line_map.build(conn, sid)
        finally:
            conn.close()
    assert [t["turn"] for t in data["turns"]] == ["T1", "T2", "T3"]
    assert any(abs(u["apex_pos"] - 0.65) < 0.02 for u in data["unnumbered"]), \
        data["unnumbered"]
    lap = next(l for l in data["laps"] if l["id"] == odd)
    stray = [c for c in lap["corners"] if abs(c["apex"] - 0.65) < 0.02]
    assert stray and stray[0]["turn"] is None, lap["corners"]
    print(f"  lap {odd}: extra corner at {stray[0]['apex']}, unnumbered")


def test_balance_is_front_minus_rear_slip_and_only_counts_cornering():
    """The corner metric's sign, point by point, and a glitch is not data."""
    got = analysis.sample_slip_balance(
        {"slip_fl": 1.4, "slip_fr": 1.2, "slip_rl": 0.5, "slip_rr": 0.3})
    assert abs(got - 0.9) < 1e-9, got
    assert analysis.sample_slip_balance(
        {"slip_fl": 999.0, "slip_fr": 1.2, "slip_rl": 0.5,
         "slip_rr": 0.3}) is None

    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            for n in range(1, 4):
                _store(conn, sid, n, 90000 + n)
            data = line_map.build(conn, sid, every_ms=0)
        finally:
            conn.close()
    lap = data["laps"][0]
    loaded = [b for b, g in zip(lap["bal"], lap["lat"])
              if abs(g) >= lap["thr"]]
    idle = [b for b, g in zip(lap["bal"], lap["lat"]) if g == 0.0]
    assert loaded and all(abs(b - 0.9) < 1e-6 for b in loaded), loaded[:5]
    assert idle and all(b == 0.0 for b in idle)
    # Straights are left out of both the lap's median and the shared scale,
    # or 0.0 from every straight would pull both towards neutral.
    assert lap["bal_med"] == 0.9 and data["balance_scale"] == 0.9, (
        lap["bal_med"], data["balance_scale"])


def test_the_tyres_are_carried_point_by_point():
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            _store(conn, sid, 1, 90000)
            lap = line_map.build(conn, sid)["laps"][0]
        finally:
            conn.close()
    for key, want in (("cfl", 80.0), ("crr", 95.0), ("pfr", 27.0),
                      ("prl", 28.0)):
        assert len(lap[key]) == len(lap["p"]), key
        assert set(lap[key]) == {want}, (key, set(lap[key]))


def _geared(seq):
    """Samples from (gear, rpm) pairs, 40 ms apart, walking round the lap."""
    return [{"gear": g, "rpm": r, "t_ms": k * 40, "norm_pos": k / 1000}
            for k, (g, r) in enumerate(seq)]


def test_a_shift_is_read_across_the_neutral_ac_reports_mid_change():
    """6 -> N -> 5 on the wire is one downshift, not two changes."""
    got = line_map.shifts(_geared(
        [(3, 7400)] * 5 + [(0, 7300)] + [(4, 6300)] * 5 +       # up
        [(0, 6000)] * 2 + [(3, 7100)] * 3))                     # down
    assert [(s["from"], s["to"]) for s in got] == [(3, 4), (4, 3)], got
    up, down = got
    assert (up["rpm_before"], up["rpm_after"]) == (7400, 6300), up
    assert up["pos"] == 0.004, up        # the last sample still in third
    assert (down["rpm_before"], down["rpm_after"]) == (6300, 7100), down


def test_neutral_held_for_a_stop_is_not_a_shift():
    """A spin, or a stop: whatever gear comes next is not a change from before."""
    got = line_map.shifts(_geared(
        [(2, 6000)] * 5 + [(0, 900)] * 30 + [(1, 3000)] * 5 +
        [(-1, 2000)] * 3 + [(1, 3200)] * 2))
    assert got == [], got


def test_reverse_ends_the_gear_sequence():
    """A spin that went second, reverse, first is not a downshift."""
    got = line_map.shifts(_geared([(2, 6000)] * 3 + [(-1, 1200)] * 2 +
                                  [(1, 3000)] * 3))
    assert got == [], got


def test_a_press_on_a_repeated_lap_number_is_not_guessed_at():
    """A lap abandoned before the line takes the next lap's number.

    Two laps then share a number, and nothing in a press says which of them
    it was pressed on. It keeps its place on the track and no lap.
    """
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            _store(conn, sid, 2, 40000, complete=False)
            _store(conn, sid, 2, 90000)
            db.add_note(conn, sid, 1, 0.5, "understeer", 120.0)
            data = line_map.build(conn, sid)
        finally:
            conn.close()
    (note,) = data["notes"]
    assert note["lap_id"] is None and note["ambiguous"] is True, note


def test_two_long_lap_lists_do_not_share_a_file_name():
    """Ends and a count are not a name: the earlier export was overwritten."""
    def named(ids):
        return line_map.default_name(
            {"from_laps": True, "session_ids": [1],
             "laps": [{"id": i} for i in ids]})
    spread, packed = (1, 10, 20, 30, 40, 50, 60), (1, 2, 3, 4, 5, 6, 60)
    assert named(spread) != named(packed), named(spread)
    assert named(spread) == named(spread), "the same laps must name one file"


def test_the_gearing_table_reads_every_counted_lap():
    laps = [line_map.shifts(_geared(
        [(3, rpm)] * 3 + [(0, 0)] + [(4, 6200)] * 3 +
        [(0, 0)] + [(2, 7600)] * 3)) for rpm in (7300, 7400, 7500)]
    g = line_map.gearing(laps, 7700)
    assert g["ceiling_rpm"] == 7700 and g["max_gear"] == 4, g
    assert g["up"] == [{"from": 3, "to": 4, "n": 3, "rpm_med": 7400,
                        "rpm_lo": 7300, "rpm_hi": 7400}], g["up"]
    assert g["down"] == [{"from": 4, "to": 2, "n": 3, "rpm_med": 7600,
                          "rpm_max": 7600}], g["down"]


def test_a_lap_carries_its_revs_shifts_and_time_at_the_ceiling():
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            for n in range(1, 3):
                _store(conn, sid, n, 90000 + n)
            data = line_map.build(conn, sid)
        finally:
            conn.close()
    lap = data["laps"][0]
    assert len(lap["rpm"]) == len(lap["p"])
    # Three corners, each a drop to third and back to fifth.
    assert [(s["from"], s["to"]) for s in lap["shifts"]] == [(5, 3), (3, 5)] * 3
    assert data["gearing"]["ceiling_rpm"] == 7000, data["gearing"]
    # The synthetic car sits at 7000 flat out on every straight, so every
    # straight is time at the ceiling.
    assert len(lap["limiter"]) >= 3, lap["limiter"]


def _travel_rows(n=4000, bumpy=(0.40, 0.45), lap_count=0):
    """Suspension rows every 20 ms: slow load transfer, and a rough stretch.

    The slow part is 20 mm of travel swinging at a quarter hertz -- weight
    moving, which the bump map must not report. The rough stretch adds
    2 mm at 12 Hz, which it must.
    """
    rows = []
    for k in range(n):
        t, pos = k * 20, k / n
        v = 0.030 + 0.020 * math.sin(2 * math.pi * 0.25 * t / 1000)
        if bumpy[0] <= pos < bumpy[1]:
            v += 0.002 * math.sin(2 * math.pi * 12 * t / 1000)
        rows.append({"lap_count": lap_count, "t_ms": t, "spline": pos,
                     **{k2: v for k2 in line_map.TRAVEL_KEYS}})
    return rows


def test_the_bump_map_finds_the_rough_stretch_and_not_the_weight_moving():
    prof = line_map.bump_profile(_travel_rows(), line_map.TRAVEL_KEYS,
                                 "spline", slices=100)
    rough = [v for i, v in enumerate(prof) if 41 <= i < 44]
    # Not the first or last stretch: the window there can only look one way,
    # so it lags like a trailing one -- an eighth of a second at the line.
    smooth = [v for i, v in enumerate(prof) if 0 < i < 35 or 50 < i < 99]
    # 2 mm of sine is 1.41 mm RMS; the window takes a little of it.
    assert all(1.1 < v < 1.6 for v in rough), rough
    assert max(smooth) < 0.4, max(smooth)
    print(f"  rough {min(rough):.2f}-{max(rough):.2f} mm, smooth at most "
          f"{max(smooth):.2f} mm")


def test_off_track_uses_the_track_limits_rule():
    def run(n_out):
        s = [{"t_ms": k * 40, "norm_pos": k / 100, "tyres_out": 0}
             for k in range(60)]
        for k in range(20, 20 + n_out):
            s[k]["tyres_out"] = db.TRACK_LIMITS_WHEELS
        return line_map.off_track(s)
    assert run(5) == [{"from": 0.2, "to": 0.24}], run(5)
    # Two samples, 80 ms: a glitch by the rule that scores the lap.
    assert run(2) == [], run(2)


def test_presses_suspension_and_the_surface_reach_the_page_data():
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            first = _store(conn, sid, 1, 90000)
            _store(conn, sid, 2, 90100)
            db.store_suspension_batch(conn, sid, "app", _travel_rows())
            db.add_note(conn, sid, 0, 0.5, "understeer", 120.0)
            db.add_note(conn, sid, 7, 0.2, "oversteer", 90.0)
            db.add_note(conn, None, 0, 0.3, "braking", 80.0)
            data = line_map.build(conn, sid)
        finally:
            conn.close()
    lap1, lap2 = data["laps"]
    assert lap1["surface_src"] == "app" and lap2["surface_src"] == "ride"
    assert len(data["surface"]["rms_mm"]) == line_map.SURFACE_SLICES
    assert data["surface"]["sources"] == ["app", "ride"]
    assert len(lap1["rf"]) == len(lap1["p"]) and len(lap1["y"]) == len(lap1["p"])
    assert lap1["off"] == [] and lap1["lockups"] == []
    placed = {(n["tag"], n["lap_id"]) for n in data["notes"]}
    # Pressed with no lap complete: lap 1. Pressed on a lap never stored:
    # kept, with no lap to sit on. Pressed with no session: counted, not placed.
    assert placed == {("understeer", first), ("oversteer", None)}, placed
    assert data["orphan_notes"] == 1


def _rival_rows(car=1, lap=2, n=500, speed=150.0, timed=True,
                positions=True, teleport_at=None):
    """An opponent lap round a 500 m circle, as the bridge stores it."""
    rows = []
    for k in range(n):
        pos, a = k / n, k / n * 2 * math.pi
        brake = k % 50 < 3
        rows.append({
            "car_index": car, "lap_count": lap, "spline": pos,
            "speed_kmh": 999.9 if k == teleport_at else speed,
            "gear": 4, "gas": 0.0 if brake else 1.0,
            "brake": 0.8 if brake else 0.0,
            "t_ms": k * 48 if timed else None,
            "pos_x": 500 * math.cos(a) if positions else None,
            "pos_y": 0.0 if positions else None,
            "pos_z": 500 * math.sin(a) if positions else None})
    return rows


def _rival_session(conn, rows, name="Ben B"):
    sid = make_session(conn)
    db.set_fuel_basis(conn, sid, track_length_m=3000.0)
    _store(conn, sid, 1, 90000)
    db.store_rival_batch(conn, sid, [{"car_index": rows[0]["car_index"],
                                      "driver_name": name,
                                      "car_model": "ks_mazda_mx5_cup",
                                      "lap_count": rows[0]["lap_count"]}],
                         rows)
    return sid


def test_integrated_time_is_distance_over_speed():
    tm = line_map.integrate_time([108.0] * 1000, 3000.0)   # 30 m/s
    assert abs(tm[-1] - 99900) <= 1, tm[-1]     # 999 steps of 3 m


def _js_function(name: str) -> str:
    """One function's source, lifted out of the page by matching its braces.

    Crude on purpose: a brace inside a string literal would break it, and
    that is the right failure -- this exists to notice when the page's copy
    of an algorithm moves, and a silent partial match would defeat it.
    """
    src = line_map.TEMPLATE.read_text(encoding="utf-8")
    start = src.index(f"function {name}(")
    i, depth = src.index("{", start), 0
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


def test_the_pages_integrate_matches_the_one_that_fills_its_other_side():
    """The gap trace subtracts a JS estimate from a Python one.

    When an opponent's lap has no clock, gapTrace() compares `mine`, which
    the page integrates in JavaScript, against `theirs`, which
    integrate_time() produced here. Its comment -- "both laps are timed the
    same way so a method's bias falls out" -- is only true while the two
    implementations agree, and nothing but this test says they do. A tuning
    change to either one would tilt every gap on the page rather than fail
    anything.
    """
    # The same convention as the Lua app's optional interpreter: absent on
    # the gaming PC, where skipping is right, and a failure in CI, where a
    # skip would be a green build over an untested page. GitHub's ubuntu and
    # windows runners both ship node.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lua_harness
    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        if lua_harness.strict():
            raise RuntimeError(
                "node is not installed, so the page's integrate() would go "
                f"unchecked against integrate_time(). {lua_harness.STRICT_ENV} "
                "is set, which means this is CI, where that skip is a "
                "failure.")
        lua_harness.skip("node is not installed")
        return

    track = 3000.0
    # Varying speed, a stop (under the 1 m/s floor both sides apply), and
    # gaps of one and two steps -- everything the loop branches on.
    speeds = [None if i in (7, 20, 21) else
              0.5 if 30 <= i < 34 else
              60.0 + 90.0 * math.sin(i / 7.0) ** 2
              for i in range(60)]
    script = (f"const TRACK_M={track}, RG={len(speeds)};\n"
              + _js_function("integrate")
              + f"\nconsole.log(JSON.stringify(integrate({json.dumps(speeds)})));")
    run = subprocess.run([node, "-e", script], capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    js = json.loads(run.stdout)
    py = line_map.integrate_time(speeds, track)

    assert len(js) == len(py) == len(speeds), (len(js), len(py))
    # integrate_time rounds each step to whole ms and the page does not, so
    # they may differ by the rounding and by nothing else.
    worst = max(abs(a - b) for a, b in zip(js, py))
    assert worst <= 1, f"the page and integrate_time disagree by {worst} ms"
    assert py[-1] > 1000, py[-1]
    print(f"  the page's integrate() tracks integrate_time() to {worst:.3f} ms "
          f"over {py[-1]} ms of lap")


def test_an_empty_step_is_road_still_covered():
    """Skipping empty steps ran a real two-minute lap four seconds short."""
    speeds = [108.0 if i % 2 == 0 else None for i in range(1000)]
    tm = line_map.integrate_time(speeds, 3000.0)
    assert abs(tm[-1] - 99800) <= 1, tm[-1]     # to step 998, 3 m each


def test_short_gaps_are_filled_and_long_ones_left_empty():
    got = line_map._fill_gaps([1.0, None, None, 4.0, None, None, None, 8.0])
    assert got[:4] == [1.0, 2.0, 3.0, 4.0], got
    assert got[4:7] == [None, None, None], got


def test_an_opponent_lap_on_its_own_clock_carries_its_line():
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = _rival_session(conn, _rival_rows())
            data = line_map.build(conn, sid)
        finally:
            conn.close()
    (rival,) = data["rivals"]
    assert (rival["name"], rival["car_model"]) == ("Ben B", "ks_mazda_mx5_cup")
    lap = rival["laps"][0]
    assert lap["clock"] == "timed" and lap["time_src"] == "timed", lap["clock"]
    assert len(lap["v"]) == line_map.RIVAL_GRID == len(lap["x"])
    # 500 samples 48 ms apart: a 24-second lap.
    assert abs(lap["time_ms"] - 24000) < 200, lap["time_ms"]
    assert lap["inputs"] == {"gas": True, "brake": True}
    assert data["track_length_m"] == 3000.0


def test_an_opponent_lap_with_no_clock_is_timed_from_speed_and_says_so():
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = _rival_session(conn, _rival_rows(timed=False,
                                                   positions=False,
                                                   teleport_at=100))
            lap = line_map.build(conn, sid)["rivals"][0]["laps"][0]
        finally:
            conn.close()
    assert lap["clock"] == "estimated" and lap["time_src"] == "estimated"
    assert "x" not in lap, "a lap with no positions drew a line"
    # 3 km at 150 km/h is 72 s -- and the teleport sample is not driving.
    assert abs(lap["time_ms"] - 72000) < 720, lap["time_ms"]
    assert max(v for v in lap["v"] if v is not None) < 999, "teleport kept"


def test_a_lap_of_nothing_but_teleports_is_not_integrated():
    """Filtering every sample left an empty lap, and dividing by no distance."""
    lap = line_map.rival_lap(
        [dict(r, speed_kmh=999.9) for r in _rival_rows()], 3000.0)
    assert lap["clock"] is None and lap["tm"] is None, lap["clock"]
    assert lap["v"] == [None] * line_map.RIVAL_GRID
    assert line_map._grid_lap_ms(lap) is None


def test_a_clock_that_never_moves_is_not_a_clock():
    """A car reporting a constant timestamp would time a lap at zero."""
    rows = [dict(r, t_ms=0) for r in _rival_rows()]
    lap = line_map.rival_lap(rows, 3000.0)
    assert lap["clock"] == "estimated", lap["clock"]
    assert abs(line_map._grid_lap_ms(lap) - 72000) < 720


def test_a_lap_seen_from_just_after_the_line_still_gets_a_time():
    """Requiring the end steps gave nearly a whole lap no time at all."""
    rows = [r for r in _rival_rows() if 0.01 <= r["spline"] <= 0.99]
    lap = line_map.rival_lap(rows, 3000.0)
    assert lap["clock"] == "timed", lap["clock"]
    # 500 samples 48 ms apart, less the trimmed ends, scaled back up.
    assert abs(line_map._grid_lap_ms(lap) - 24000) < 400


def test_a_recorded_lap_time_outranks_an_estimate():
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = _rival_session(conn, _rival_rows(lap=2, timed=False))
            db.store_rival_batch(conn, sid, [], _rival_rows(lap=3,
                                                            timed=False))
            conn.execute("INSERT INTO rival_laps VALUES (?,?,?,?,?)",
                         (sid, 1, 3, 71000, 0.0))
            conn.commit()
            laps = line_map.build(conn, sid)["rivals"][0]["laps"]
        finally:
            conn.close()
    assert [(l["lap_count"], l["time_src"]) for l in laps] == [
        (3, "recorded"), (2, "estimated")], laps


def test_opponents_are_left_off_a_map_of_several_sessions():
    with temp_db() as path:
        conn = db.connect(path)
        try:
            first = _store(conn, _rival_session(conn, _rival_rows()), 2, 90100)
            other = _store(conn, make_session(conn), 1, 90200)
            data = line_map.build(conn, lap_ids=[first, other])
        finally:
            conn.close()
    assert data["rivals"] == [], data["rivals"]


def test_your_laps_carry_their_clock_for_the_time_gap():
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            _store(conn, sid, 1, 90000)
            lap = line_map.build(conn, sid, every_ms=0)["laps"][0]
        finally:
            conn.close()
    assert lap["tm"][:3] == [0, 40, 80], lap["tm"][:3]


# --- the page -----------------------------------------------------------


def _page(setup_name="baseline"):
    with temp_db() as path:
        conn = db.connect(path)
        try:
            sid = make_session(conn)
            _store(conn, sid, 1, 90000, setup_name=setup_name)
            return line_map.render(line_map.build(conn, sid))
        finally:
            conn.close()


def _embedded(page):
    m = re.search(r"const DATA = (.*?);\n", page)
    assert m, "no data in the page"
    return json.loads(m.group(1))


def test_a_setup_name_cannot_break_out_of_the_page():
    """A setup name is whatever the driver typed."""
    name = "v2</script><script>alert(1)</script>"
    page = _page(name)
    assert page.count("</script>") == 1, "the data closed the script block"
    assert _embedded(page)["laps"][0]["setup"] == name


def test_the_page_fetches_nothing():
    """Opened from disk, it must not tell anyone it was opened."""
    page = _page()
    assert not re.search(r"https?://|url\(|@import", page), re.search(
        r".{40}(?:https?://|url\(|@import).{40}", page)
    assert page.startswith("<!doctype html>")
    assert "<title>Mugello Driving Lines</title>" in page


# --- the tool -----------------------------------------------------------

_SERVER = None


def _server():
    global _SERVER
    if _SERVER is None:
        d = tempfile.mkdtemp(prefix="ac-line-map-")
        os.environ["ASSETTO_MCP_DATA"] = d
        os.environ["AC_DOCS_DIR"] = d
        os.environ["ASSETTO_MCP_BRIDGE_PORT"] = "0"
        os.environ["ASSETTO_MCP_NO_AUTOSTART"] = "1"
        import importlib
        _SERVER = importlib.import_module("assetto_mcp.server")
        atexit.register(_SERVER._bridge.stop)
    return _SERVER


def _call(fn, **kw):
    return json.loads(getattr(fn, "fn", fn)(**kw))


def test_export_line_map_writes_the_file_and_says_where():
    srv = _server()
    sid = make_session(srv._conn)
    lap = _store(srv._conn, sid, 1, 90000)
    out = _call(srv.export_line_map, session_id=sid)
    path = Path(out["path"])
    assert path.is_file(), out
    assert path.parent == srv.DATA_DIR / "exports", path
    assert out["laps_drawn"] == 1 and out["best_lap_id"] == lap, out
    assert out["url"].startswith("file:"), out["url"]

    missing = _call(srv.export_line_map, lap_ids=[999999])
    assert "no lap with id 999999" in missing["error"], missing
    print(f"  wrote {path.name}")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
