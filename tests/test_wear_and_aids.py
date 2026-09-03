"""Tyre wear across a stint, and what the driver aids actually did.

Both channels were stored for weeks and read by nothing, which is its own
kind of failure: the driver asked "did the tyres go off" and got an answer
argued from hot pressure and core temperature, while the game's own wear
figures sat in the same rows unread. A proxy that happens to be flat is not
evidence, and neither is a setup value nobody has measured the effect of.
"""

import math
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from support import run_module  # noqa: E402

from assetto_mcp import analysis  # noqa: E402


def _stint(per_lap, start=100.0, laps=8, lap_ms=113000):
    """A stint whose tyres lose `per_lap(n)` percent on lap n, per corner.

    Wear counts DOWN from `start`, the way AC reports it, so a test that
    passed by reading the raw field would be reading it backwards.
    """
    entries = []
    remaining = {w: start for w in analysis.WEAR_CORNERS}
    for n in range(1, laps + 1):
        first = {f"wear_{w}": remaining[w] for w in analysis.WEAR_CORNERS}
        for w in analysis.WEAR_CORNERS:
            remaining[w] -= per_lap(n, w)
        last = {f"wear_{w}": remaining[w] for w in analysis.WEAR_CORNERS}
        entries.append({
            "lap": {"id": 500 + n, "lap_number": n, "lap_time_ms": lap_ms},
            "first": first, "last": last,
        })
    return entries


def _brake_lap(n=600, straight_slip=0.4, corner_slip=2.6, lock_at=None,
               lock_slip=4.5, rear_slip=0.3, aid_constant=0.06):
    """A lap with one straight-line braking zone and one trail-braked corner.

    The corner brakes just as hard but with the wheel turned, and carries
    slip well above the lockup threshold -- which is exactly the false
    positive the steering filter exists to prevent.
    """
    out = []
    for i in range(n):
        pos = i / n
        straight_braking = 0.40 <= pos < 0.46
        trail_braking = 0.70 <= pos < 0.76
        braking = straight_braking or trail_braking
        front = straight_slip if straight_braking else (
            corner_slip if trail_braking else 0.2)
        if lock_at is not None and abs(pos - lock_at) < 0.012:
            front = lock_slip
        out.append({
            "norm_pos": pos,
            "brake": 0.9 if braking else 0.0,
            "steer": 0.55 if trail_braking else 0.02,
            "slip_fl": front, "slip_fr": front * 0.95,
            "slip_rl": rear_slip, "slip_rr": rear_slip * 0.95,
            # Held constant on purpose: this is what the real fields do.
            "abs_active": aid_constant, "tc_active": aid_constant + 0.02,
        })
    return out


def _attitude_lap(n=800, roll_per_g=1.5, dive_per_g=0.8, peak_g=2.0):
    """A lap whose body roll is a known number of degrees per g."""
    out = []
    for i in range(n):
        pos = i / n
        lat = peak_g * math.sin(pos * 4 * math.pi)
        lon = -2.0 if 0.4 <= pos < 0.5 else 0.3
        out.append({
            "norm_pos": pos,
            "acc_lat": lat,
            "acc_lon": lon,
            # Stored in radians, the way AC reports them.
            "roll": math.radians(roll_per_g * lat),
            "pitch": math.radians(dive_per_g * -lon),
        })
    return out


def _meta(lap_id=1, ms=113000):
    return {"id": lap_id, "lap_time_ms": ms}


# --- tyre wear ----------------------------------------------------------


def test_wear_is_reported_counting_up_even_though_ac_counts_down():
    """`used` has to grow as the tyre gets worse.

    AC's field decreases from 100. Passing it straight through would give a
    driver a number that falls as their tyres deteriorate, which is the
    kind of thing that gets read wrong once and trusted forever.
    """
    out = analysis.stint_wear(_stint(lambda n, w: 0.5, laps=6))
    s = out["summary"]
    assert s["remaining_at_start_pct"]["fl"] == 100.0, s
    assert abs(s["remaining_at_end_pct"]["fl"] - 97.0) < 1e-6, s
    assert abs(s["used_total_pct"]["fl"] - 3.0) < 1e-6, s
    assert abs(s["used_per_lap_pct"]["fl"] - 0.5) < 1e-6, s
    print(f"  6 laps at 0.5%/lap -> {s['used_total_pct']['fl']}% used, "
          f"{s['remaining_at_end_pct']['fl']}% left")


def test_a_steady_tyre_and_a_degrading_one_are_told_apart():
    """The distinction the whole report exists for.

    Every tyre accumulates wear; that on its own costs nothing. "They went
    off" means the RATE rose, and a total alone cannot show that.
    """
    steady = analysis.stint_wear(_stint(lambda n, w: 0.5, laps=8))
    for w in analysis.WEAR_CORNERS:
        assert abs(steady["trend"][w]["change"]) < 1e-6, steady["trend"]

    # Same total wear, but concentrated in the second half.
    degrading = analysis.stint_wear(
        _stint(lambda n, w: 0.2 if n <= 4 else 0.8, laps=8))
    for w in analysis.WEAR_CORNERS:
        t = degrading["trend"][w]
        assert t["change"] > 0.5, t
    assert (abs(degrading["summary"]["used_total_pct"]["fl"]
                - steady["summary"]["used_total_pct"]["fl"]) < 1e-6), (
        "the two stints should have identical totals, or this test is "
        "measuring the total rather than the trend")
    print(f"  steady change {steady['trend']['fl']['change']}, "
          f"degrading change {degrading['trend']['fl']['change']}, "
          f"same total")


def test_the_worst_corner_is_named():
    out = analysis.stint_wear(_stint(
        lambda n, w: 1.0 if w == "rr" else 0.3, laps=6))
    assert out["summary"]["worst_corner"] == "rr", out["summary"]


def test_a_lap_without_wear_is_not_counted_as_a_lap_without_degradation():
    """Missing data and zero wear must not look the same.

    A pre-v9 lap has no reading at all. Treating that as 0% used would
    report a stint as flawless because half of it was unrecorded.
    """
    entries = _stint(lambda n, w: 0.5, laps=4)
    entries[1]["first"] = None
    entries[2]["last"] = {"wear_fl": None, "wear_fr": None,
                          "wear_rl": None, "wear_rr": None}
    out = analysis.stint_wear(entries)
    assert out["laps_with_wear"] == 2, out
    assert out["laps_without_wear"] == 2, out
    assert out["laps"][1]["has_wear"] is False
    assert "used_this_lap_pct" not in out["laps"][1]
    assert out["summary"]["laps_measured"] == 2, out["summary"]
    print(f"  {out['laps_with_wear']} measured, "
          f"{out['laps_without_wear']} without wear, reported separately")


def test_a_stint_with_no_wear_at_all_says_so_rather_than_reporting_zero():
    entries = _stint(lambda n, w: 0.5, laps=3)
    for e in entries:
        e["first"] = e["last"] = None
    out = analysis.stint_wear(entries)
    assert "summary" not in out, out
    assert "cannot be backfilled" in out["error"], out
    assert out["laps_with_wear"] == 0


def test_the_trend_is_withheld_when_there_are_too_few_laps():
    """Three laps cannot support a first-half-against-second-half claim."""
    out = analysis.stint_wear(_stint(lambda n, w: 0.5, laps=3))
    assert "trend" not in out, out
    assert "summary" in out, out


# --- braking ------------------------------------------------------------
#
# This began as an ABS activity report and the first real lap killed it:
# shared memory's `abs` and `tc` were constant to three decimal places
# across a whole lap, straights included. So these test what can actually
# be measured -- what the tyres did under braking -- and that the aid
# fields are described as the constants they are.


def test_trail_braking_is_not_mistaken_for_a_locked_wheel():
    """The filter the whole report depends on.

    Slip under combined braking and cornering is high by construction. The
    synthetic corner here brakes just as hard as the straight and carries
    2.6 slip -- comfortably over the lockup threshold -- with the wheel
    turned. Counting it would make every trail-braked entry a lockup, and
    the report would flag a spotless lap.
    """
    out = analysis.braking_report(_meta(), _brake_lap())
    assert out["front_lockup_run_count"] == 0, out["front_lockup_runs"]
    # And the corner's samples were excluded, not merely outvoted.
    assert out["straight_line_braking_samples"] < out["hard_braking_samples"]
    print(f"  {out['hard_braking_samples']} braking samples, "
          f"{out['straight_line_braking_samples']} straight-line, "
          f"{out['front_lockup_run_count']} lockups")


def test_a_front_wheel_running_away_in_a_straight_line_is_found():
    out = analysis.braking_report(_meta(), _brake_lap(lock_at=0.43))
    assert out["front_lockup_run_count"] >= 1, out
    run = out["front_lockup_runs"][0]
    assert 0.41 <= run["from_pos"] <= 0.45, run
    assert out["under_straight_line_braking"]["front"]["max_slip"] == 4.5
    print(f"  lockup found at {run['from_pos']}-{run['to_pos']}, "
          f"{run['samples']} samples")


def test_the_axle_nearer_its_limit_is_named():
    """Which end is closer to locking is what brake bias moves."""
    fronty = analysis.braking_report(
        _meta(), _brake_lap(straight_slip=1.2, rear_slip=0.3))
    assert fronty["axle_closer_to_locking"] == "front", fronty

    reary = analysis.braking_report(
        _meta(), _brake_lap(straight_slip=0.3, rear_slip=1.2))
    assert reary["axle_closer_to_locking"] == "rear", reary


def test_a_lap_with_no_straight_line_braking_refuses_to_judge():
    """Trail braking alone cannot say anything about a locking wheel."""
    lap = [s for s in _brake_lap() if not (0.40 <= s["norm_pos"] < 0.46)]
    out = analysis.braking_report(_meta(), lap)
    assert "under_straight_line_braking" not in out, out
    assert "straight-line braking" in out["error"], out


def test_a_constant_aid_field_is_reported_as_constant_not_as_activity():
    """The mistake this module was rebuilt around.

    A field holding one value down every straight cannot be measuring
    intervention, and the payload must not let a reader believe it is.
    """
    out = analysis.braking_report(_meta(), _brake_lap(aid_constant=0.06))
    fields = out["aid_fields"]
    assert fields["abs"]["varies"] is False, fields
    assert fields["abs"]["min"] == fields["abs"]["max"] == 0.06, fields
    assert "not a measure of intervention" in out["aid_fields_note"]
    assert "physics worker" in out["aid_fields_note"]
    print(f"  abs held {fields['abs']['min']} all lap -> reported as "
          f"constant")


# --- body attitude ------------------------------------------------------


def test_the_roll_gradient_recovers_a_known_stiffness():
    """Degrees of roll per g -- the direct measure of roll stiffness.

    Every anti-roll bar argument so far has been reasoned from load
    transfer or from the driver's description. This is the number that
    either moves when a bar moves, or shows that it did not.
    """
    out = analysis.attitude_report(_meta(), _attitude_lap(roll_per_g=1.5))
    roll = out["roll"]
    assert abs(roll["gradient_deg_per_g"] - 1.5) < 0.05, roll
    # Converted from radians, not passed through.
    assert 2.5 < roll["max_abs_deg"] < 3.5, roll
    print(f"  {roll['gradient_deg_per_g']} deg/g recovered from a 1.5 "
          f"deg/g car, peak roll {roll['max_abs_deg']} deg")


def test_a_stiffer_car_reports_a_smaller_gradient():
    soft = analysis.attitude_report(_meta(), _attitude_lap(roll_per_g=2.4))
    stiff = analysis.attitude_report(_meta(), _attitude_lap(roll_per_g=0.9))
    assert (soft["roll"]["gradient_deg_per_g"]
            > stiff["roll"]["gradient_deg_per_g"] + 1.0), (soft, stiff)


def test_dive_under_braking_is_measured_separately_from_roll():
    out = analysis.attitude_report(_meta(), _attitude_lap(dive_per_g=0.8))
    assert abs(out["pitch"]["dive_deg_per_g"] - 0.8) < 0.05, out["pitch"]


def test_a_gradient_is_withheld_when_the_car_was_never_loaded():
    """A slope through a handful of near-zero points is noise, not a number."""
    gentle = _attitude_lap(peak_g=0.2)
    out = analysis.attitude_report(_meta(), gentle)
    assert out["roll"]["gradient_deg_per_g"] is None, out["roll"]
    assert "under the" in out["roll"]["gradient_note"], out["roll"]


def test_a_lap_without_attitude_says_so_instead_of_reporting_a_flat_car():
    lap = [{"norm_pos": i / 100, "acc_lat": 1.5, "acc_lon": -1.0}
           for i in range(100)]
    out = analysis.attitude_report(_meta(), lap)
    assert out["has_attitude"] is False, out
    assert "never captured" in out["error"], out
    assert "roll" not in out


def test_an_empty_lap_is_refused_rather_than_divided_by():
    assert "error" in analysis.braking_report(_meta(), [])
    assert "error" in analysis.attitude_report(_meta(), [])
    assert "error" in analysis.stint_wear([])


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
