"""Delta-time by track position, and lateral-g corner detection.

Both exist because of the same failure: a lap was 558ms slower with every
detected corner equal or faster. The time had gone somewhere the analysis
could not see -- a fast sweeper the speed-minima detector excluded by
construction, on ground no corner metric covered.
"""

import math
import sys
from pathlib import Path

# tests/ for `support`, and the repo root for `ac_race_engineer`. The other
# modules get the root as a side effect of importing support, which does the
# same insert; this one is the only file that needs to say so itself, and
# saying it explicitly is why `python tests/test_delta_and_corners.py` works
# from a clean interpreter with nothing installed.
_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from support import run_module  # noqa: E402

from ac_race_engineer import analysis  # noqa: E402


def _lap(n=1200, lap_ms=113000, corners=((0.15, 0.05, 2.4),
                                         (0.45, 0.04, 1.2),
                                         (0.70, 0.08, 2.8),
                                         (0.88, 0.05, 2.2)),
         slow_from=None, slow_to=None, slow_factor=1.0):
    """A synthetic lap: four corners, optionally slower over one stretch.

    `corners` is (center, half_width, peak_lat_g). The third is deliberately
    a fast sweeper -- wide, high lateral g, only a shallow speed dip -- which
    is the shape the old speed-minima detector could not see.
    """
    out = []
    t = 0.0
    for i in range(n):
        pos = i / n
        lat = 0.0
        dip = 0.0
        for c_pos, half, g in corners:
            d = abs(pos - c_pos)
            if d < half:
                shape = math.cos(d / half * math.pi / 2)
                lat = g * shape * (1 if c_pos < 0.6 else -1)
                # A fast sweeper barely slows the car; slow corners do.
                dip = max(dip, (0.55 if g < 2.5 else 0.12) * shape)
        speed = 215.0 * (1.0 - dip)

        step = lap_ms / n
        if slow_from is not None and slow_from <= pos < slow_to:
            step *= slow_factor
        t += step

        out.append({
            "norm_pos": pos, "t_ms": t, "speed_kmh": speed,
            "gas": 0.0 if dip > 0.3 else 1.0,
            "brake": 0.9 if dip > 0.3 else 0.0,
            "steer": 0.4 if lat else 0.0, "gear": 3 if dip > 0.3 else 5,
            "acc_lat": lat, "acc_lon": -1.0 if dip > 0.3 else 0.2,
            "slip_fl": 1.4 if lat else 0.2, "slip_fr": 1.4 if lat else 0.2,
            "slip_rl": 0.5 if lat else 0.2, "slip_rr": 0.5 if lat else 0.2,
            "press_fl": 28.0, "press_fr": 27.0,
            "press_rl": 29.0, "press_rr": 28.0,
            "core_fl": 102.0, "core_fr": 93.0,
            "core_rl": 94.0, "core_rr": 89.0,
            "ride_f": 0.018, "ride_r": 0.023,
        })
    return out


def _meta(lap_id, ms):
    return {"id": lap_id, "lap_time_ms": ms, "valid": 1, "car": "c",
            "track": "mugello", "track_config": "osrw", "setup_name": "s"}


# --- corner detection ---------------------------------------------------


def test_a_fast_sweeper_is_detected():
    """The Arrabbiata case: high lateral g, only a shallow speed dip.

    Taken at ~93% of top speed, so the old detector's `speed >= 0.92*vmax`
    guard threw it away before prominence was even considered.
    """
    corners = analysis.detect_corners(_lap())
    apexes = [c["apex_pos"] for c in corners]
    assert len(corners) == 4, apexes
    assert any(abs(a - 0.70) < 0.05 for a in apexes), apexes

    sweeper = min(corners, key=lambda c: abs(c["apex_pos"] - 0.70))
    assert sweeper["min_speed_kmh"] > 180, sweeper
    assert sweeper["peak_lat_g"] > 2.0, sweeper


def test_corners_carry_direction_and_extent():
    corners = analysis.detect_corners(_lap())
    for c in corners:
        assert c["entry_pos"] < c["apex_pos"] < c["exit_pos"], c
        assert c["turn_sign"] in (1, -1)
    # Corners 3 and 4 were built turning the other way.
    assert corners[0]["turn_sign"] != corners[-1]["turn_sign"]


def test_opposite_turns_are_two_corners_not_one():
    """An esse must not merge into a single region straddling the flip."""
    esse = _lap(corners=((0.50, 0.05, 2.0), (0.60, 0.05, 2.0)))
    # Force the second to turn the other way by placing it past 0.6.
    corners = analysis.detect_corners(esse)
    assert len(corners) == 2, [c["apex_pos"] for c in corners]
    assert corners[0]["turn_sign"] != corners[1]["turn_sign"]


def test_a_lap_with_no_cornering_reports_none():
    flat = _lap(corners=())
    assert analysis.detect_corners(flat) == []


def test_a_twitch_is_not_a_corner():
    """A brief spike of lateral g is a kerb or a correction, not a corner."""
    twitchy = _lap(corners=((0.30, 0.002, 2.5),))
    assert analysis.detect_corners(twitchy) == []


# --- brake points -------------------------------------------------------


def _braking_lap(corners=((0.50, 0.03, 2.2),), brakes=((0.400, 0.485),),
                 n=1200):
    """Corners and braking zones specified independently.

    The other fixtures derive `brake` from the speed dip, which puts braking
    inside the corner by construction -- exactly the assumption that hid this
    bug. Here the pedal trace is given explicitly, so it can start where a
    driver actually brakes: on the straight, before the car is loaded up.
    """
    out = []
    for i in range(n):
        pos = i / n
        lat = 0.0
        for c_pos, half, g in corners:
            d = abs(pos - c_pos)
            if d < half:
                lat = g * math.cos(d / half * math.pi / 2)
        braking = any(lo <= pos < hi for lo, hi in brakes)
        # Slowest at the apex, so the apex lands in the middle of the corner.
        speed = 210.0 - 45.0 * abs(lat)
        out.append({
            "norm_pos": pos, "t_ms": i * 40.0, "speed_kmh": speed,
            "gas": 0.0 if braking else 1.0,
            "brake": 0.9 if braking else 0.0,
            "steer": 0.4 if lat else 0.0, "gear": 3 if lat else 5,
            "acc_lat": lat, "acc_lon": -1.2 if braking else 0.2,
            "slip_fl": 1.4 if lat else 0.2, "slip_fr": 1.4 if lat else 0.2,
            "slip_rl": 0.5 if lat else 0.2, "slip_rr": 0.5 if lat else 0.2,
        })
    return out


def test_the_brake_point_is_where_braking_began_not_where_the_car_turned_in():
    """The brake point used to collapse onto turn-in.

    Drivers brake in a straight line, before lateral load builds -- so at the
    corner's entry, braking is already underway. Searching forward from the
    entry for the first pedal application therefore found the sample the
    search started on: this corner brakes from 0.400 and turns in at ~0.477,
    and it reported 0.477 as the brake point. The 8% of the lap where the car
    was actually being slowed was invisible, and compare_laps was
    differencing turn-in points under the name "brake point".
    """
    corners = analysis.detect_corners(_braking_lap())
    assert len(corners) == 1, corners
    c = corners[0]
    assert abs(c["brake_point_pos"] - 0.400) < 0.005, c
    assert c["brake_point_pos"] < c["entry_pos"] - 0.05, c
    print(f"  braking from {c['brake_point_pos']}, turn-in at "
          f"{c['entry_pos']}, apex {c['apex_pos']}")


def test_pedal_modulation_does_not_split_the_braking_zone():
    """A driver easing off and back on is one brake application, not two."""
    lap = _braking_lap(brakes=((0.400, 0.430), (0.4325, 0.485)))
    gap = [s for s in lap if 0.430 <= s["norm_pos"] < 0.4325]
    assert 0 < len(gap) <= analysis.BRAKE_ZONE_GAP_SAMPLES, len(gap)

    c = analysis.detect_corners(lap)[0]
    assert abs(c["brake_point_pos"] - 0.400) < 0.005, c


def test_braking_that_starts_after_turn_in_is_still_found():
    """Trail-braking a corner entered off the pedal must still be reported."""
    c = analysis.detect_corners(_braking_lap(brakes=((0.490, 0.499),)))[0]
    assert abs(c["brake_point_pos"] - 0.490) < 0.005, c
    assert c["brake_point_pos"] > c["entry_pos"], c


def test_a_corner_taken_without_braking_reports_no_brake_point():
    """Flat through a sweeper: there is no brake point, and inventing one
    would put a phantom entry in every comparison against a lap that braked."""
    c = analysis.detect_corners(_braking_lap(brakes=()))[0]
    assert c["brake_point_pos"] is None, c


def test_two_corners_do_not_steal_each_others_brake_zone():
    """Linked corners, with only a flick off the pedal between them.

    The backward walk tolerates modulation, so with a gap this short nothing
    in the pedal trace separates the two applications -- the second corner
    would keep walking back through the first one's braking and report the
    first corner's brake point as its own. The previous corner's exit is what
    stops it.
    """
    lap = _braking_lap(corners=((0.20, 0.02, 2.2), (0.45, 0.03, 2.2)),
                       brakes=((0.100, 0.2133), (0.2167, 0.440)))
    corners = analysis.detect_corners(lap)
    assert len(corners) == 2, [c["apex_pos"] for c in corners]

    first, second = corners
    assert abs(first["brake_point_pos"] - 0.100) < 0.005, first
    # Its own zone, not the one 12% of a lap earlier.
    assert second["brake_point_pos"] > first["exit_pos"], (first, second)
    assert abs(second["brake_point_pos"] - 0.2167) < 0.01, second
    print(f"  brake points {first['brake_point_pos']} and "
          f"{second['brake_point_pos']}, exits {first['exit_pos']} and "
          f"{second['exit_pos']}")


# --- delta by position --------------------------------------------------


def test_delta_finds_time_lost_between_corners():
    """The whole point: loss on ground no corner covers."""
    fast = _lap(lap_ms=113000)
    slow = _lap(lap_ms=113000, slow_from=0.60, slow_to=0.75, slow_factor=1.30)
    slow_ms = int(slow[-1]["t_ms"])

    out = analysis.delta_by_position(_meta(1, 113000), fast,
                                     _meta(2, slow_ms), slow)
    assert "error" not in out, out
    worst = out["worst_losses"][0]
    assert 0.55 <= worst["from"] <= 0.75, worst
    assert worst["gain_ms"] > 100, worst

    # Segments outside the slow stretch should be ~flat.
    quiet = [r for r in out["segments"] if r["to"] <= 0.55]
    assert all(abs(r["gain_ms"]) < 20 for r in quiet), quiet
    print(f"  loss localised to {worst['from']}-{worst['to']}, "
          f"{worst['gain_ms']}ms")


def test_delta_is_zero_for_a_lap_against_itself():
    lap = _lap()
    out = analysis.delta_by_position(_meta(1, 113000), lap,
                                     _meta(2, 113000), lap)
    assert all(abs(r["gain_ms"]) < 1e-6 for r in out["segments"]), out
    assert abs(out["segments"][-1]["cumulative_ms"]) < 1e-6


def test_cumulative_matches_the_lap_time_difference():
    fast = _lap(lap_ms=113000)
    slow = _lap(lap_ms=113000, slow_from=0.20, slow_to=0.40, slow_factor=1.5)
    slow_ms = int(slow[-1]["t_ms"])
    out = analysis.delta_by_position(_meta(1, 113000), fast,
                                     _meta(2, slow_ms), slow)
    final = out["segments"][-1]["cumulative_ms"]
    # Within a segment's worth of resolution of the true difference.
    assert abs(final - (slow_ms - 113000)) < 60, (final, slow_ms - 113000)
    print(f"  cumulative {final}ms vs true {slow_ms - 113000}ms")


def test_a_spin_does_not_invent_a_phantom_gain():
    """norm_pos going backwards must not make time flow backwards.

    A spin loses ground *and* time: the car slews back down the track and
    then has to re-cover it. Interpolating naively through the reversal
    reads the recovery as progress already made, which shows up as a large
    negative delta -- a gain -- at precisely the point the driver lost the
    most. The worst possible place for the tool to be confidently wrong.
    """
    lap = _lap()
    spun = _lap()
    SPIN_MS = 3000.0
    for i, s in enumerate(spun):
        if 600 <= i < 640:             # slewing backwards, burning time
            s["norm_pos"] -= 0.03 * (i - 599) / 40
            s["t_ms"] += SPIN_MS * (i - 599) / 40
        elif i >= 640:                 # rejoins, permanently down on time
            s["t_ms"] += SPIN_MS
    out = analysis.delta_by_position(_meta(1, 113000), lap,
                                     _meta(2, int(spun[-1]["t_ms"])), spun)
    assert "error" not in out, out
    assert all(r["gain_ms"] > -50 for r in out["segments"]), \
        [r for r in out["segments"] if r["gain_ms"] <= -50]
    # The loss is real and must persist to the end, not wash out.
    assert out["segments"][-1]["cumulative_ms"] > SPIN_MS * 0.8, out["segments"][-1]
    print(f"  spin costs {out['segments'][-1]['cumulative_ms']}ms, no phantom gain")


def test_partial_overlap_is_refused():
    full = _lap()
    part = [s for s in _lap() if s["norm_pos"] < 0.3]
    out = analysis.delta_by_position(_meta(1, 113000), full,
                                     _meta(2, 113000), part)
    assert "error" in out, out


def _flat_lap(extra_ms=0, after=0.96, n=1200):
    """A lap where all the extra time is spent after `after`."""
    out, t = [], 0.0
    for i in range(n):
        pos = i / n
        t += 40.0 + (extra_ms / (n * (1 - after)) if pos > after else 0.0)
        out.append({"norm_pos": pos, "t_ms": int(t), "speed_kmh": 150.0,
                    "acc_lat": 0.1, "brake": 0.0, "gas": 1.0})
    return out


def test_time_lost_at_the_very_end_of_the_lap_is_reported():
    """The failure this whole feature exists to prevent, at the boundary.

    Sampling stops when completedLaps ticks over, so the last sample is
    always short of position 1.0 -- and dropping a segment because its
    *endpoint* was uncovered discarded the final twentieth of every lap.
    That is the last corner and the run to the line. It reported the gap in
    the total, then showed every segment flat, with track_covered_pct at
    99.5 saying everything was fine.
    """
    sa, sb = _flat_lap(0), _flat_lap(8000)
    la = {"id": 1, "lap_time_ms": sa[-1]["t_ms"]}
    lb = {"id": 2, "lap_time_ms": sb[-1]["t_ms"]}
    out = analysis.delta_by_position(la, sa, lb, sb, segments=20)

    assert len(out["segments"]) == 20, len(out["segments"])
    worst = out["worst_losses"][0]
    assert worst["from"] >= 0.9, worst
    assert worst["gain_ms"] > 5000, worst
    # And the part genuinely beyond the last sample is named, not hidden.
    assert out["unaccounted_ms"] > 0, out["unaccounted_ms"]
    assert abs(out["accounted_ms"] + out["unaccounted_ms"]
               - out["total_delta_ms"]) < 1.0, out
    print(f"  {worst['gain_ms']}ms found at {worst['from']}-{worst['to']}, "
          f"{out['unaccounted_ms']}ms beyond the last sample")


def test_a_partial_segment_says_which_span_it_measured():
    sa, sb = _flat_lap(0), _flat_lap(4000)
    la = {"id": 1, "lap_time_ms": sa[-1]["t_ms"]}
    lb = {"id": 2, "lap_time_ms": sb[-1]["t_ms"]}
    out = analysis.delta_by_position(la, sa, lb, sb, segments=20)
    partial = [r for r in out["segments"] if "measured_from" in r]
    assert partial, "no segment reported a narrowed span"
    for r in partial:
        assert r["from"] <= r["measured_from"] <= r["measured_to"] <= r["to"]
    print(f"  {len(partial)} segment(s) reported their measured span")


def test_gains_are_gains_and_not_the_smallest_losses():
    """Bottom-three of the same list reported a lap that was slower
    everywhere as having three "biggest gains"."""
    sa, sb = _flat_lap(0), _flat_lap(4000)
    la = {"id": 1, "lap_time_ms": sa[-1]["t_ms"]}
    lb = {"id": 2, "lap_time_ms": sb[-1]["t_ms"]}
    out = analysis.delta_by_position(la, sa, lb, sb, segments=20)
    assert out["biggest_gains"] == [], out["biggest_gains"]
    print("  a lap slower everywhere reports no gains")


def test_segments_argument_cannot_divide_by_zero():
    """It reaches this straight from an MCP tool argument."""
    sa, sb = _flat_lap(0), _flat_lap(1000)
    la = {"id": 1, "lap_time_ms": sa[-1]["t_ms"]}
    lb = {"id": 2, "lap_time_ms": sb[-1]["t_ms"]}
    for n in (0, -5):
        out = analysis.delta_by_position(la, sa, lb, sb, segments=n)
        assert out["segments"], n
    print("  segments=0 and negative handled")


def test_segments_asks_for_a_row_count_and_gets_it():
    """It is an MCP tool argument, so it has to mean what it says.

    `step = fine // segments` meant it did not: 7 returned 8 rows, 300
    returned 200, and nothing in the output said the request had been
    reinterpreted.
    """
    sa, sb = _flat_lap(0), _flat_lap(4000)
    la = {"id": 1, "lap_time_ms": sa[-1]["t_ms"]}
    lb = {"id": 2, "lap_time_ms": sb[-1]["t_ms"]}

    for n in (1, 3, 7, 20, 199, 200, 500):
        out = analysis.delta_by_position(la, sa, lb, sb, segments=n)
        rows = out["segments"]
        expected = min(n, 200)          # 200 grid spans is as fine as it gets
        assert len(rows) == expected, (n, len(rows))
        # The rows still tile the whole lap, edge to edge, in order.
        assert rows[0]["from"] == 0.0 and rows[-1]["to"] == 1.0, (n, rows[0],
                                                                  rows[-1])
        for a, b in zip(rows, rows[1:]):
            assert a["to"] == b["from"], (n, a, b)
            assert a["from"] < a["to"], (n, a)
        # A request that could not be honoured says so instead of quietly
        # returning a different number of rows.
        assert out.get("segments_requested") == (n if n > expected else None)
    print("  1, 3, 7, 20, 199, 200 and 500 all return the row count asked for")


# --- lateral-g glitches -------------------------------------------------


def _corner_lap(spike=None, spike_at=600, spike_len=6, g=1.1,
                apexes=(0.15, 0.40, 0.65, 0.90), width=0.0006):
    out, n = [], 1200
    for i in range(n):
        pos = i / n
        lat = sum(g * math.exp(-((pos - a) ** 2) / width) for a in apexes)
        speed = 200 - 60 * abs(lat)
        if spike is not None and spike_at <= i < spike_at + spike_len:
            lat = spike
        out.append({"norm_pos": pos, "t_ms": i * 40, "speed_kmh": speed,
                    "acc_lat": lat, "brake": 0.0, "gas": 1.0,
                    "steer": 0.2, "gear": 4,
                    "slip_fl": 1.0, "slip_fr": 1.0,
                    "slip_rl": 0.5, "slip_rr": 0.5})
    return out


def test_a_lateral_g_spike_does_not_become_a_corner():
    """One glitched sample used to decide the whole lap.

    The threshold is a fraction of the lap's own peak lateral g, so a spike
    raises the bar above every real corner: a six-sample 9g artefact on a
    1.1g road-car lap left exactly one "corner" -- the artefact -- with a
    fabricated peak_lat_g. Smaller spikes were reported as an extra corner.
    """
    baseline = len(analysis.detect_corners(_corner_lap()))
    assert baseline == 4, baseline
    for spike in (2.0, 3.0, 4.5, 9.0, 30.0, float("inf"), float("nan")):
        got = len(analysis.detect_corners(_corner_lap(spike)))
        assert got == baseline, f"{spike}g spike gave {got} corners"
    print(f"  {baseline} corners found regardless of spike magnitude")


def test_an_impossible_reading_is_rejected_however_long_it_lasts():
    """The two defences cover different failures and both are needed.

    The median filter removes anything shorter than a corner, whatever its
    magnitude. It cannot remove a *sustained* bad reading -- a stuck channel
    after a reset -- because by duration that looks exactly like a corner.
    That is what the magnitude ceiling is for: no car in AC pulls 50g, so a
    long run of it is broken telemetry, not the hardest corner of the lap.
    Without the ceiling it would set the threshold and hide every real one.
    """
    long_run = analysis.CORNER_MIN_SAMPLES * 3
    clean = len(analysis.detect_corners(_corner_lap()))
    for bad in (50.0, 500.0, float("inf"), float("nan")):
        got = analysis.detect_corners(_corner_lap(bad, spike_len=long_run))
        assert len(got) == clean, f"{bad} for {long_run} samples -> {got}"
        assert all(c["peak_lat_g"] < analysis.LAT_G_SANE_MAX for c in got)
    print(f"  a {long_run}-sample impossible reading stays out of the "
          f"corner list")


def test_a_real_sustained_corner_is_not_filtered_away():
    """The filter works on duration, so it must not eat short real corners.

    Anything at or above CORNER_MIN_SAMPLES is a corner by this module's own
    definition and has to survive.
    """
    long_enough = len(analysis.detect_corners(
        _corner_lap(3.0, spike_len=analysis.CORNER_MIN_SAMPLES + 4)))
    assert long_enough == 5, long_enough
    assert (analysis.LAT_G_MEDIAN_WINDOW // 2
            < analysis.CORNER_MIN_SAMPLES), (
        "the median filter is wide enough to remove a real corner")
    print(f"  a {analysis.CORNER_MIN_SAMPLES + 4}-sample load is kept")


def test_cars_of_every_grip_level_still_find_their_corners():
    for g in (1.1, 1.6, 3.0):
        corners = analysis.detect_corners(_corner_lap(g=g))
        assert len(corners) == 4, (g, len(corners))
        assert corners[0]["peak_lat_g"] > g * 0.8, corners[0]
    print("  road, GT3 and formula grip levels all detected")


# --- the threshold must not depend on how hard the lap was driven -------
#
# The bug these were written for, seen from the car: comparing five Suzuka
# laps against two, seven corners of seventeen were reported as found on
# only one side. The threshold is a fraction of the lap's own 99th-
# percentile lateral g, and across that run peak_lat_g ran 2.78 to 3.42 --
# a 23% swing in the bar, which took every corner near it in and out of
# existence. A corner absent from a lap is not compared on that lap, and
# the corners nearest the threshold are the light ones, which is where a
# roll-stiffness change shows up first.


def _pair_of_efforts():
    """The same circuit driven at two intensities.

    Identical corners but for the third, which is harder on one lap. That
    alone moves a per-lap threshold enough to change which of the *other*
    corners clear it.
    """
    shape = ((0.15, 0.05, 2.4), (0.45, 0.04, 1.2), (0.88, 0.05, 2.2))
    easy = _lap(corners=shape + ((0.70, 0.08, 2.6),))
    hard = _lap(corners=shape + ((0.70, 0.08, 3.6),))
    return easy, hard


def test_a_shared_reference_finds_the_same_corners_on_both_laps():
    """The fix, stated as the property that has to hold.

    Not "the light corner survives" -- that depends on where the constants
    sit. The invariant is that two laps of the same circuit yield the same
    corners, so a comparison between them is drawn on all of them.

    This is what fails if reference_peak_g is ignored, which is the mutation
    that reverts the fix while leaving every other test passing.
    """
    easy, hard = _pair_of_efforts()

    solo_easy = len(analysis.detect_corners(easy))
    solo_hard = len(analysis.detect_corners(hard))
    assert solo_easy != solo_hard, (
        "premise gone: these laps no longer disagree per-lap, so this test "
        f"is not exercising the bug ({solo_easy} vs {solo_hard})")

    ref = analysis.lat_g_reference([easy, hard])
    a = [c["apex_pos"] for c in analysis.detect_corners(easy, ref)]
    b = [c["apex_pos"] for c in analysis.detect_corners(hard, ref)]
    assert len(a) == len(b), (a, b)
    for x, y in zip(a, b):
        assert abs(x - y) < 0.02, (a, b)
    print(f"  per-lap: {solo_easy} vs {solo_hard} corners. "
          f"shared reference {ref:.2f}g: {len(a)} vs {len(b)}")


def test_compare_laps_holds_both_laps_to_one_bar():
    """End to end, because the reference is only useful if it is threaded.

    detect_corners taking the argument means nothing if compare_laps still
    calls it twice with nothing.
    """
    easy, hard = _pair_of_efforts()
    out = analysis.compare_laps(_meta(1, 113000), easy,
                                _meta(2, 113480), hard)
    assert len(out["corners"]) == 4, out["corners"]
    print(f"  {len(out['corners'])} corners matched across efforts")


def test_one_wild_lap_does_not_move_the_reference():
    """Median, not mean or max.

    One lap with a big correction on it, or one lap driven far harder than
    the rest, must not raise the bar for the whole run -- that would be the
    original bug with extra steps, since the run's marginal corners would
    vanish from every lap at once instead of from one.
    """
    normal = [_lap() for _ in range(4)]
    wild = _lap(corners=((0.15, 0.05, 5.5), (0.45, 0.04, 1.2),
                         (0.70, 0.08, 2.8), (0.88, 0.05, 2.2)))
    without = analysis.lat_g_reference(normal)
    with_ = analysis.lat_g_reference(normal + [wild])
    assert abs(with_ - without) < 0.05, (without, with_)
    print(f"  {without:.2f}g -> {with_:.2f}g with a 5.5g lap in the set")


def test_an_inlap_neither_moves_the_reference_nor_gains_corners():
    """A borrowed threshold must not conjure corners out of a flat lap.

    The reference makes the bar lower for a quiet lap, which is the point --
    but a lap with no cornering load at all has no corners regardless, and
    promoting its noise would be worse than the bug being fixed. The guard
    is on the lap's own peak and must stay there.
    """
    flat = _lap(corners=())
    ref = analysis.lat_g_reference([_lap(), _lap(), flat])
    assert ref is not None and ref > 1.0, ref
    assert analysis.detect_corners(flat, ref) == []
    # And it contributed nothing: dropping it leaves the same reference.
    assert analysis.lat_g_reference([_lap(), _lap()]) == ref
    print(f"  reference {ref:.2f}g, flat lap still has 0 corners")


def test_no_reference_when_nothing_corners():
    """None, not zero: callers fall back to per-lap rather than to no bar."""
    assert analysis.lat_g_reference([_lap(corners=()),
                                     _lap(corners=())]) is None
    assert analysis.lat_g_reference([]) is None


def test_a_lap_read_on_its_own_is_unchanged():
    """The single-lap path has no run to borrow from and must not regress."""
    assert (len(analysis.detect_corners(_lap(), None))
            == len(analysis.detect_corners(_lap())) == 4)


# --- the headline accelerations are filtered too ------------------------
#
# detect_corners has dropped impossible acceleration since a 9g burst
# invented a corner. lap_summary's own peak_lat_g and peak_braking_g did
# not, and they are compare_runs metrics -- so the same signal was cleaned
# for one purpose and passed through raw for another.
#
# Seen live at Sebring: one ~10g lateral spike reported peak_lat_g as 6.32g
# averaged over two laps against a real 2.4, and inflated the metric's noise
# estimate until compare_runs' resolution for it reached 15g. The lap was
# not merely misreported; the channel stopped being able to detect anything.


def _spike(samples, field, value, at=600, n=3):
    """A short burst of impossible physics, the shape AC actually emits."""
    for s in samples[at:at + n]:
        s[field] = value
    return samples


def test_a_lateral_spike_does_not_reach_the_headline_figure():
    clean = analysis.lap_summary(_meta(1, 113000), _lap())
    spiked = analysis.lap_summary(_meta(1, 113000),
                                  _spike(_lap(), "acc_lat", 10.0))

    assert spiked["peak_lat_g"] == clean["peak_lat_g"], (
        f"the 10g spike reached peak_lat_g: {spiked['peak_lat_g']}")
    assert spiked["peak_lat_g"] < analysis.LAT_G_SANE_MAX, spiked
    assert spiked["accel_samples_dropped"] == 3, spiked
    assert clean["accel_samples_dropped"] is None, clean
    print(f"  10g spike dropped, peak stays {spiked['peak_lat_g']}g, "
          f"{spiked['accel_samples_dropped']} samples reported")


def test_a_braking_spike_does_not_reach_the_headline_figure():
    """peak_braking_g took a raw min() and had no ceiling at all."""
    clean = analysis.lap_summary(_meta(1, 113000), _lap())
    spiked = analysis.lap_summary(_meta(1, 113000),
                                  _spike(_lap(), "acc_lon", -12.0))
    assert spiked["peak_braking_g"] == clean["peak_braking_g"], spiked
    assert spiked["peak_braking_g"] > -analysis.LON_G_SANE_MAX, spiked
    print(f"  -12g spike dropped, braking stays "
          f"{spiked['peak_braking_g']}g")


def test_a_spike_is_dropped_not_clamped():
    """Clamping would report the ceiling as though the car had pulled it.

    The distinction matters: 6.0g in a payload is a claim about the car,
    and a model reading it has no way to tell it from a measurement.
    """
    spiked = analysis.lap_summary(_meta(1, 113000),
                                  _spike(_lap(), "acc_lat", 10.0))
    assert spiked["peak_lat_g"] != analysis.LAT_G_SANE_MAX, spiked


def test_a_lap_of_nothing_but_glitches_reports_none():
    """No number is better than a wrong one when every sample is a glitch."""
    lap = _spike(_lap(), "acc_lat", 99.0, at=0, n=1200)
    out = analysis.lap_summary(_meta(1, 113000), lap)
    assert out["peak_lat_g"] is None, out["peak_lat_g"]
    assert out["accel_samples_dropped"] == 1200, out


def test_both_channels_are_held_to_the_same_bar_as_corner_detection():
    """One signal, one definition of believable.

    The bug was two code paths disagreeing about what counts as physics.
    If these constants ever diverge, that disagreement is back.
    """
    assert analysis.LAT_G_SANE_MAX == analysis.LON_G_SANE_MAX
    assert _lap(corners=()) is not None  # the helper still builds a flat lap
    lat = analysis._sane_channel(_spike(_lap(), "acc_lat", 10.0),
                                 "acc_lat", analysis.LAT_G_SANE_MAX)
    assert all(abs(v) <= analysis.LAT_G_SANE_MAX for v in lat)


# --- the driving line ---------------------------------------------------


def _placed(samples, x0=0.0, z0=0.0, rough_at=None):
    """Give a synthetic lap a world position: a circle of radius 500m.

    Offsetting the whole circle stands in for a wider line -- every slice
    is then a known distance from the reference lap, which is exactly what
    separation_m has to recover.
    """
    for s in samples:
        a = s["norm_pos"] * 2 * math.pi
        s["pos_x"] = x0 + 500.0 * math.cos(a)
        s["pos_y"] = 0.0
        s["pos_z"] = z0 + 500.0 * math.sin(a)
        s["ride_f"] = 0.060
        if rough_at is not None and abs(s["norm_pos"] - rough_at) < 0.02:
            # A stretch where the car is being thrown about vertically.
            s["ride_f"] = 0.060 + 0.020 * math.sin(s["norm_pos"] * 900)
    return samples


def test_a_lap_without_position_says_so_instead_of_guessing():
    """Pre-v8 laps cannot be backfilled and must not pretend otherwise."""
    out = analysis.driving_line(_meta(1, 113000), _lap())
    assert out["has_position"] is False, out
    assert "cannot be backfilled" in out["error"], out
    assert "line" not in out, out


def test_a_comparison_lap_with_no_samples_is_an_error_not_a_silent_skip():
    """"I asked for a comparison and got none" and "I did not ask" must not
    produce the same payload.

    The branch was gated on the truthiness of other_samples, so a lap whose
    telemetry was never stored fell straight through it: no comparison, and
    no comparison_error either. A reader cannot tell that from a
    single-lap call, and the answer to "was that a wider line" would have
    been silence.
    """
    mine = _placed(_lap())
    out = analysis.driving_line(_meta(1, 113000), mine, 20,
                                _meta(2, 113500), [])
    assert "compared_with" not in out, out
    assert "no telemetry samples stored" in out["comparison_error"], out

    # And not asking still says nothing, which is the case it was confused
    # with.
    alone = analysis.driving_line(_meta(1, 113000), mine, 20)
    assert "comparison_error" not in alone, alone
    print(f"  {out['comparison_error']}")


def test_two_laps_that_never_overlap_say_so_rather_than_going_quiet():
    """The same ambiguity one level further in.

    Both laps carry position, so the checks above pass, and then no slice
    holds both -- two partial laps that stopped in different places. gaps
    comes back empty and the payload used to contain neither compared_with
    nor comparison_error, which is the state those checks exist to prevent.
    """
    first_half = [s for s in _placed(_lap()) if s["norm_pos"] < 0.4]
    second_half = [s for s in _placed(_lap()) if s["norm_pos"] > 0.6]

    out = analysis.driving_line(_meta(1, 113000), first_half, 20,
                                _meta(2, 113500), second_half)
    assert "compared_with" not in out, out
    assert "no slice of track in common" in out["comparison_error"], out
    print(f"  {out['comparison_error'][:72]}...")


def test_the_line_follows_the_car_round_the_track():
    out = analysis.driving_line(_meta(1, 113000), _placed(_lap()), points=40)
    assert out["has_position"] is True
    assert out["slices_measured"] == 40, out["slices_measured"]
    assert out["slices_empty"] == 0, out["slices_empty"]
    xs = [p["x"] for p in out["line"]]
    zs = [p["z"] for p in out["line"]]
    # A circle: both coordinates span roughly the diameter.
    assert max(xs) - min(xs) > 900, (min(xs), max(xs))
    assert max(zs) - min(zs) > 900, (min(zs), max(zs))
    print(f"  {out['slices_measured']} slices, x spans "
          f"{max(xs) - min(xs):.0f}m")


def test_separation_recovers_a_known_offset_between_two_lines():
    """The number that answers "was that a wider line, and by how much"."""
    mine = _placed(_lap())
    wider = _placed(_lap(), x0=3.0)      # three metres across, all the way
    out = analysis.driving_line(_meta(1, 113000), mine, 40,
                                _meta(2, 113400), wider)
    comp = out["compared_with"]
    assert abs(comp["mean_separation_m"] - 3.0) < 0.2, comp
    assert abs(comp["max_separation_m"] - 3.0) < 0.5, comp
    assert all("separation_m" in p for p in out["line"]), out["line"][0]
    print(f"  3.0m offset recovered as {comp['mean_separation_m']}m mean")


def test_the_bump_map_finds_the_rough_stretch():
    """ride_f_range_mm is the bump proxy, and has to point at the bumps."""
    out = analysis.driving_line(_meta(1, 113000),
                                _placed(_lap(), rough_at=0.62), points=50)
    worst = out["roughest_sections"][0]
    assert abs(worst["pos"] - 0.62) < 0.04, out["roughest_sections"][:3]
    smooth = [p for p in out["line"]
              if p and abs(p["pos"] - 0.2) < 0.05][0]
    assert worst["ride_f_range_mm"] > 10 * smooth["ride_f_range_mm"], (
        worst, smooth)
    print(f"  roughest slice at {worst['pos']} "
          f"({worst['ride_f_range_mm']}mm of movement)")


def test_a_slice_the_car_never_reached_is_empty_not_invented():
    """A gap in a line must not be drawn through somewhere the car wasn't."""
    samples = _placed(_lap())
    kept = [s for s in samples if not (0.4 <= s["norm_pos"] < 0.5)]
    out = analysis.driving_line(_meta(1, 113000), kept, points=20)
    missing = [i for i, p in enumerate(out["line"]) if p is None]
    assert missing == [8, 9], missing
    assert out["slices_empty"] == 2, out["slices_empty"]


def test_the_point_count_is_clamped_rather_than_trusted():
    for asked, expect in ((0, 100), (5, 10), (99999, 200), (40, 40)):
        out = analysis.driving_line(_meta(1, 113000), _placed(_lap()), asked)
        assert out["points"] == expect, (asked, out["points"])


def test_a_flat_list_of_samples_is_refused_rather_than_answered():
    """The call-site mistake this function is easiest to make.

    Every other function here takes a flat list of samples; this one takes
    one entry per lap. Given the flat version, each "lap" is a single dict,
    len(dict) < 50 skips all of them, and the answer is None -- which
    detect_corners reads as "no shared bar" and silently falls back to the
    per-lap threshold. That is precisely the behaviour the shared reference
    exists to remove, arrived at from a call that looked like it worked.
    """
    laps = [_lap(), _lap()]
    assert analysis.lat_g_reference(laps) is not None

    try:
        analysis.lat_g_reference(_lap())      # flat, the likely typo
    except TypeError as e:
        assert "one entry per lap" in str(e), e
        print(f"  refused: {str(e)[:60]}...")
    else:
        raise AssertionError("a flat sample list was accepted")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
