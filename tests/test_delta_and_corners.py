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


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
