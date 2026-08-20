"""Turning a lap's samples into the numbers an engineer reasons about.

slip_balance is the one number this tool exists to produce -- positive means
the front is sliding more, and the setup advice that follows depends on its
sign. AC occasionally emits a wheelSlip in the tens of thousands, and a
single such sample moved a balance of 1.4 to 6002, so what gets filtered out
and what survives is load-bearing in both directions.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402

from ac_race_engineer import analysis  # noqa: E402

CLEAN = {"slip_fl": 1.4, "slip_fr": 1.4, "slip_rl": 0.5, "slip_rr": 0.5}


def _corner(spike_at=None, spike=None):
    """16 samples around an apex, optionally with one glitched tick."""
    out = []
    for i in range(16):
        src = spike if (spike and i == spike_at) else CLEAN
        out.append({"norm_pos": i / 16, "speed_kmh": 100.0, "gear": 3,
                    "brake": 0.0, "gas": 1.0, "steer": 0.5, **src})
    return out


def test_a_single_spike_does_not_decide_the_corners_balance():
    spike = {"slip_fl": 30007.881, "slip_fr": 1.4,
             "slip_rl": 0.5, "slip_rr": 0.5}
    stats = analysis._corner_stats(_corner(8, spike), 8, 15)
    assert stats["slip_samples_dropped"] == 1
    assert abs(stats["front_slip"] - 1.4) < 0.001, stats["front_slip"]
    assert abs(stats["slip_balance"] - 0.9) < 0.001, stats["slip_balance"]
    print("  spike dropped, balance", stats["slip_balance"])


def test_glitched_samples_are_dropped_never_clamped():
    """Substituting the ceiling would be inventing data.

    A wheelSlip of 30007 doesn't mean "slid a lot", it means the sample
    isn't describing tyre behavior at all.
    """
    assert analysis._sane_slip(30007.881, 1.4) is None
    assert analysis._sane_slip(float("inf"), 1.0) is None
    assert analysis._sane_slip(float("nan"), 1.0) is None
    assert analysis._sane_slip(1.4, 1.4) == 1.4
    print("  glitches return None rather than a clamped value")


def test_a_genuine_big_slide_survives_the_filter():
    """The property most easily lost by tightening the ceiling.

    With SLIP_SANE_MAX at 2.0 every other test in this file still passes
    while a real 3.1 front slide is silently discarded -- and discarding
    the moments the car actually slid biases the balance toward
    understeer, which is the recommendation-flipping direction.
    """
    assert analysis._sane_slip(3.1, 3.1) == 3.1
    assert analysis._sane_slip(2.5, 3.0) == 2.75
    assert analysis.SLIP_SANE_MAX > 3.1
    print("  a genuine 3.1 slide is kept")


def test_how_much_was_thrown_away_is_reported():
    """A count alone reads the same for three 30007s and three 51s.

    Only one of those says the ceiling is in the right place, and the
    threshold is asserted rather than derived -- so this is the field
    evidence for whether it is.
    """
    spike = dict(CLEAN, slip_fl=30007.881, steer=4021.5)
    stats = analysis._corner_stats(_corner(8, spike), 8, 15)
    assert stats["slip_samples_dropped"] == 1
    assert stats["slip_dropped_peak"] == 30007.9, stats
    assert stats["slip_coverage_pct"] == 93.8, stats
    print("  peak dropped slip:", stats["slip_dropped_peak"])


def test_steering_comes_from_the_samples_the_filter_kept():
    """The tick that emits wheelSlip=30007 is not one to trust for steer.

    Reporting peak_steer_norm = 4021 from a sample already judged
    not-data contradicts the field's own -1..1 definition.
    """
    spike = dict(CLEAN, slip_fl=30007.881, steer=4021.5)
    stats = analysis._corner_stats(_corner(8, spike), 8, 15)
    assert stats["peak_steer_norm"] == 0.5, stats
    print("  glitched steer excluded:", stats["peak_steer_norm"])


def test_steer_is_reported_as_a_fraction_of_lock():
    """AC normalizes steerAngle to -1..1, so 0.5 is half lock.

    The old peak_steer_deg name invited reading it as half a degree.
    """
    stats = analysis._corner_stats(_corner(), 8, 15)
    assert "peak_steer_deg" not in stats
    assert stats["peak_steer_norm"] == 0.5
    print("  peak_steer_norm =", stats["peak_steer_norm"])


def test_lap_summary_flags_when_the_balance_rests_on_little_data():
    base = {"speed_kmh": 100.0, "steer": 0.1, "gas": 0.5, "brake": 0.0,
            "norm_pos": 0.0, "gear": 3, "acc_lat": 0.5, "acc_lon": -0.2,
            "ride_f": 0.05, "ride_r": 0.06, "tyres_out": 0, **CLEAN}
    for w in ("fl", "fr", "rl", "rr"):
        base[f"press_{w}"] = 26.0
        base[f"core_{w}"] = 80.0

    samples = []
    for i in range(240):
        s = dict(base, norm_pos=i / 240)
        s["speed_kmh"] = 60.0 if 110 < i < 130 else 180.0
        if i == 120:
            s = dict(s, slip_fl=99999.0)
        samples.append(s)

    out = analysis.lap_summary(
        {"id": 1, "car": "x", "track": "mugello", "track_config": "",
         "lap_time_ms": 114000, "valid": 1, "setup_name": "claude_v2"},
        samples)
    assert out["setup"] == "claude_v2", out["setup"]
    if out["corners"]:
        assert out["slip_quality"] is not None
        assert out["slip_quality"]["peak_dropped_slip"] == 99999.0
    print("  slip_quality surfaced alongside the lap's setup")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
