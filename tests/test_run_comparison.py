"""Judging a setup change against the driver's own repeatability.

Lap time is the noisiest channel on the car. Measured spread across four
laps of an unchanged setup ran 0.3-0.6s, so a change worth less than about
half a second cannot be seen in a short run however well it is driven --
while front load transfer moved 2.2 points for a rear anti-roll bar change
against under 0.3 of noise. Same laps, wildly different resolving power.

These tests are about not overclaiming: a difference smaller than the noise
must come back "within noise", and the run's resolution must be reported so
a null result can be told apart from a run that was simply too short.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402

from ac_race_engineer import analysis  # noqa: E402


def _lap(lap_ms, balance, load_pct, fl_temp=100.0, corners=None):
    """A lap summary carrying only the fields compare_runs reads."""
    return {
        "lap_time_ms": lap_ms,
        "overall_slip_balance": balance,
        "top_speed_kmh": 215.0,
        "peak_lat_g": 2.9,
        "time_coasting_pct": 6.5,
        "tyres": {"fl": {"core_temp_avg": fl_temp, "pressure_end": 28.5}},
        "suspension": {"front_load_transfer_pct": load_pct},
        "corners": corners or [],
    }


def test_a_real_change_on_a_quiet_channel_is_seen_in_two_laps():
    """The rear ARB case: load transfer 58.0 -> 55.8, noise under 0.3."""
    base = [_lap(113400, 1.06, 58.1), _lap(113600, 1.07, 57.9)]
    cand = [_lap(113300, 0.95, 55.7), _lap(113500, 0.96, 55.9)]
    out = analysis.compare_runs(base, cand)

    lt = out["metrics"]["front_load_transfer_pct"]
    assert lt["verdict"] == "moved", lt
    assert lt["change"] < -2, lt
    print(f"  load transfer {lt['baseline']} -> {lt['candidate']} "
          f"(band {lt['noise_band']}) -> {lt['verdict']}")


def test_the_same_two_laps_cannot_resolve_lap_time():
    """The point of the whole exercise: same run, different resolving power."""
    base = [_lap(113400, 1.06, 58.1), _lap(113600, 1.07, 57.9)]
    cand = [_lap(113300, 0.95, 55.7), _lap(113500, 0.96, 55.9)]
    out = analysis.compare_runs(base, cand)

    lt = out["metrics"]["lap_time_ms"]
    assert lt["verdict"] == "within noise", lt
    # And it must say how big a change it *could* have seen.
    assert lt["resolution"] > 200, lt
    print(f"  lap time moved {lt['change']}ms but needed "
          f"{lt['resolution']}ms to be sure")


def test_noise_alone_never_reads_as_a_change():
    """Two runs of the same car must not manufacture a result."""
    base = [_lap(113100, 1.05, 58.0), _lap(113600, 1.02, 58.3),
            _lap(113300, 1.08, 57.8)]
    cand = [_lap(113500, 1.04, 58.2), _lap(113200, 1.07, 57.9),
            _lap(113700, 1.03, 58.1)]
    out = analysis.compare_runs(base, cand)
    moved = [k for k, m in out["metrics"].items()
             if m.get("verdict") == "moved"]
    assert not moved, moved
    assert "nothing moved beyond noise" in out["summary"]
    print("  identical cars, 0 metrics reported as changed")


def test_a_single_lap_a_side_refuses_rather_than_guesses():
    out = analysis.compare_runs([_lap(113400, 1.06, 58.1)],
                                [_lap(112900, 0.95, 55.7)])
    for key in ("lap_time_ms", "front_load_transfer_pct"):
        m = out["metrics"][key]
        assert "2 laps" in m["verdict"], m
        # The measured difference is still reported -- it just isn't judged.
        assert "change" in m
    print("  one lap a side:", out["metrics"]["lap_time_ms"]["verdict"])


def test_a_very_repeatable_run_still_needs_a_real_difference():
    """Without a floor, a freakishly consistent run calls anything a change."""
    base = [_lap(113400, 1.0600, 58.00), _lap(113400, 1.0601, 58.00)]
    cand = [_lap(113400, 1.0605, 58.01), _lap(113400, 1.0606, 58.01)]
    out = analysis.compare_runs(base, cand)
    assert out["metrics"]["slip_balance"]["verdict"] == "within noise", \
        out["metrics"]["slip_balance"]
    print("  0.0005 of slip balance is not a setup change, however tidy")


def test_corners_are_matched_by_position_not_by_index():
    """The detector finds a different number of corners on different laps,
    so corner 3 is not reliably the same piece of road twice."""
    def corner(pos, bal, spd):
        return {"apex_pos": pos, "slip_balance": bal, "min_speed_kmh": spd}

    base = [_lap(113400, 1.0, 58.0, corners=[corner(0.858, 1.20, 120.0),
                                             corner(0.691, 1.10, 110.0)]),
            _lap(113500, 1.0, 58.0, corners=[corner(0.861, 1.22, 119.5),
                                             corner(0.689, 1.12, 110.4)])]
    # A different lap finds an extra corner, shifting every index.
    cand = [_lap(113400, 0.9, 56.0, corners=[corner(0.140, 0.5, 100.0),
                                             corner(0.857, 0.60, 126.0),
                                             corner(0.690, 1.09, 110.2)]),
            _lap(113500, 0.9, 56.0, corners=[corner(0.142, 0.5, 100.4),
                                             corner(0.859, 0.62, 125.4),
                                             corner(0.692, 1.11, 110.6)])]
    out = analysis.compare_runs(base, cand)
    moved = {c["apex_pos"] for c in out["corners_that_moved"]}
    assert any(abs(p - 0.86) < 0.03 for p in moved), out["corners_that_moved"]
    # 0.691 barely changed and must not be reported.
    assert not any(abs(p - 0.69) < 0.02 for p in moved), \
        out["corners_that_moved"]
    print(f"  flagged {sorted(moved)} and left the unchanged corner alone")


def test_missing_channels_are_reported_not_invented():
    """Suspension data is absent online; that must not read as zero."""
    base = [{"lap_time_ms": 113400, "overall_slip_balance": 1.0},
            {"lap_time_ms": 113600, "overall_slip_balance": 1.02}]
    cand = [{"lap_time_ms": 113300, "overall_slip_balance": 0.95},
            {"lap_time_ms": 113500, "overall_slip_balance": 0.96}]
    out = analysis.compare_runs(base, cand)
    assert out["metrics"]["front_load_transfer_pct"]["verdict"] == \
        "not measured"
    assert out["metrics"]["lap_time_ms"]["verdict"] in ("moved",
                                                        "within noise")
    print("  absent channel reported as not measured")


def test_empty_input_is_refused():
    assert "error" in analysis.compare_runs([], [_lap(113000, 1.0, 58.0)])


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
