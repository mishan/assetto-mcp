"""Delta-time by track position, and lateral-g corner detection.

Both exist because of the same failure: a lap was 558ms slower with every
detected corner equal or faster. The time had gone somewhere the analysis
could not see -- a fast sweeper the speed-minima detector excluded by
construction, on ground no corner metric covered.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ac_race_engineer import analysis  # noqa: E402


def _lap(n=1200, lap_ms=113000, corners=((0.15, 0.05, 2.4),
                                         (0.45, 0.04, 1.2),
                                         (0.70, 0.08, 2.8),
                                         (0.88, 0.05, 2.2)),
         slow_from=None, slow_to=None, slow_factor=1.0):
    """A synthetic lap: four corners, optionally slower over one stretch.

    `corners` is (centre, half_width, peak_lat_g). The third is deliberately
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


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        print(f"\n{name}")
        try:
            fn()
            print("  PASS")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL: {e}")
    print(f"\n{'all passed' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
