"""Tyre wear across a stint, and what the driver aids actually did.

Both channels were stored for weeks and read by nothing, which is its own
kind of failure: the driver asked "did the tyres go off" and got an answer
argued from hot pressure and core temperature, while the game's own wear
figures sat in the same rows unread. A proxy that happens to be flat is not
evidence, and neither is a setup value nobody has measured the effect of.
"""

import json
import math
import os
import subprocess
import sys
import tempfile
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


def _attitude_lap(n=800, roll_per_g=1.5, dive_per_g=0.8, peak_g=2.0,
                  static_roll_deg=0.0, static_pitch_deg=0.0,
                  one_direction=False):
    """A lap whose body roll is a known number of degrees per g.

    `static_roll_deg` / `static_pitch_deg` are a constant lean and rake --
    banking, camber, road grade, a car that simply sits crooked. They are
    part of the absolute attitude AC reports and are NOT suspension
    movement, so a report that folds them into the gradient is measuring
    the track, not the car.

    `one_direction` makes every corner a right-hander, the way an oval or
    a one-sided track would.
    """
    out = []
    for i in range(n):
        pos = i / n
        lat = peak_g * math.sin(pos * 4 * math.pi)
        if one_direction:
            lat = abs(lat)
        # A realistic longitudinal profile, because the dive fit is cut at
        # zero g and a lap that jumps straight from hard braking to
        # throttle has no near-zero samples to pin the intercept with --
        # and no spread either, if braking g is a single constant.
        if 0.40 <= pos < 0.50:            # braking, easing as speed falls
            lon = -2.5 + 15.0 * (pos - 0.40)          # -2.5 .. -1.0
        elif 0.50 <= pos < 0.56:          # off the brakes, not yet on power
            lon = -0.4 + 8.0 * (pos - 0.50)           # -0.4 .. +0.08
        elif 0.56 <= pos < 0.80:
            lon = 1.2                                  # on power
        else:
            lon = 0.3                                  # light throttle
        out.append({
            "norm_pos": pos,
            "acc_lat": lat,
            "acc_lon": lon,
            # Stored in radians, the way AC reports them.
            "roll": math.radians(roll_per_g * lat + static_roll_deg),
            "pitch": math.radians(dive_per_g * -lon + static_pitch_deg),
        })
    return out


def _meta(lap_id=1, ms=113000):
    return {"id": lap_id, "lap_time_ms": ms}


def _wear_lap(lap_id, lap_number, start, used, out_lap=False, pitted=False):
    """One entry for `split_stints`, with the lap flags that segment a run."""
    corners = analysis.WEAR_CORNERS
    return {
        "lap": {"id": lap_id, "lap_number": lap_number,
                "lap_time_ms": 113000, "out_lap": out_lap, "pitted": pitted},
        "first": {f"wear_{w}": start for w in corners},
        "last": {f"wear_{w}": start - used for w in corners},
    }


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


# --- stints, which are not sessions -------------------------------------
#
# Sessions keep out-laps and pit laps on purpose and can span a tyre
# change. Running one through stint_wear whole differences the first set's
# starting wear against the last set's ending wear and trends two sets as
# one, which can report a healthy tyre as gone off.


def test_a_tyre_change_splits_the_session_into_two_stints():
    """The failure the segmentation exists for.

    Two sets of five laps each, both wearing 0.4%/lap. Reported whole, the
    subtraction spans the change: 100 at the start of the first set against
    98 at the end of the second, and the answer comes out as one set that
    lost 2% -- which no set did.
    """
    laps = [_wear_lap(1 + i, 1 + i, 100.0 - 0.4 * i, 0.4) for i in range(5)]
    laps += [_wear_lap(10 + i, 10 + i, 100.0 - 0.4 * i, 0.4)
             for i in range(5)]
    split = analysis.split_stints(laps)
    assert len(split["stints"]) == 2, split
    assert "fresh set" in split["stints"][1]["started_because"], split
    for stint in split["stints"]:
        out = analysis.stint_wear(stint["laps"])
        assert abs(out["summary"]["used_total_pct"]["fl"] - 2.0) < 1e-6, out

    # And the whole session run as one stint gets it wrong, which is the
    # point of the split rather than an incidental difference.
    whole = analysis.stint_wear(laps)
    assert whole["summary"]["used_total_pct"]["fl"] > 3.0, whole


def test_a_pit_lap_ends_a_stint_and_is_not_counted_in_either():
    """Its wear delta may straddle the change, so it belongs to neither."""
    laps = [_wear_lap(1, 1, 100.0, 0.4), _wear_lap(2, 2, 99.6, 0.4),
            _wear_lap(3, 3, 99.2, 0.2, pitted=True),
            _wear_lap(4, 4, 100.0, 0.4), _wear_lap(5, 5, 99.6, 0.4)]
    split = analysis.split_stints(laps)
    assert len(split["stints"]) == 2, split
    assert [e["lap"]["id"] for e in split["stints"][0]["laps"]] == [1, 2]
    assert [e["lap"]["id"] for e in split["stints"][1]["laps"]] == [4, 5]
    assert split["boundary_laps"][0]["lap_id"] == 3, split
    assert "pit visit" in split["boundary_laps"][0]["excluded_because"]
    print(f"  pit lap 3 held out, {len(split['stints'])} stints either side")


def test_an_out_lap_begins_a_stint():
    laps = [_wear_lap(1, 1, 100.0, 0.4), _wear_lap(2, 2, 99.6, 0.4),
            _wear_lap(3, 3, 99.2, 0.4, out_lap=True),
            _wear_lap(4, 4, 98.8, 0.4)]
    split = analysis.split_stints(laps)
    assert len(split["stints"]) == 2, split
    assert "out-lap" in split["stints"][1]["started_because"], split
    assert [e["lap"]["id"] for e in split["stints"][1]["laps"]] == [3, 4]


def test_a_clean_session_on_one_set_stays_a_single_stint():
    """Segmentation must not invent boundaries where nothing happened."""
    laps = [_wear_lap(1 + i, 1 + i, 100.0 - 0.4 * i, 0.4) for i in range(8)]
    split = analysis.split_stints(laps)
    assert len(split["stints"]) == 1, split
    assert split["boundary_laps"] == [], split
    assert len(split["stints"][0]["laps"]) == 8


def test_float_noise_in_wear_does_not_split_a_stint():
    """Wear that ticks up a hundredth is noise, not a pit stop."""
    laps = []
    for i in range(6):
        e = _wear_lap(1 + i, 1 + i, 100.0 - 0.4 * i, 0.4)
        for w in analysis.WEAR_CORNERS:
            e["first"][f"wear_{w}"] += 0.01
        laps.append(e)
    assert len(analysis.split_stints(laps)["stints"]) == 1, laps


def test_a_tyre_change_made_mid_lap_is_held_out_rather_than_subtracted():
    """A lap whose own wear goes UP changed tyres part-way through.

    Its delta is negative -- it ends on more rubber than it started with --
    so leaving it in subtracts from the stint total and flattens the trend.
    The pit flags do not always catch this: `pitted` is *inferred* for laps
    migrated from before schema v10, so the wear itself has to be checked.
    """
    laps = [_wear_lap(1, 1, 100.0, 0.4), _wear_lap(2, 2, 99.6, 0.4)]
    # Lap 3 starts on 99.2 and ends on 99.8: a fresh set went on mid-lap.
    mid = _wear_lap(3, 3, 99.2, 0.0)
    for w in analysis.WEAR_CORNERS:
        mid["last"][f"wear_{w}"] = 99.8
    laps.append(mid)
    laps += [_wear_lap(4, 4, 99.8, 0.4), _wear_lap(5, 5, 99.4, 0.4)]

    split = analysis.split_stints(laps)
    assert len(split["stints"]) == 2, split
    assert split["boundary_laps"][0]["lap_id"] == 3, split
    assert "rose during this lap" in split["boundary_laps"][0][
        "excluded_because"], split
    for stint in split["stints"]:
        out = analysis.stint_wear(stint["laps"])
        assert out["summary"]["used_total_pct"]["fl"] > 0, out
    print("  mid-lap change on an unflagged lap -> held out, no negative "
          "wear in either stint")


def test_a_lap_with_no_end_reading_does_not_fragment_the_stint_after_it():
    """A missing reading must not leave a stale baseline behind.

    The between-lap check compares this lap's start against the previous
    lap's end. If that end was never recorded and the baseline is left at
    the lap before it, the next lap looks like a jump back up and the
    stint gets cut a second time -- so one tyre change comes back as two,
    with a one-lap stint wedged between them.
    """
    laps = [_wear_lap(1, 1, 100.0, 2.0), _wear_lap(2, 2, 98.0, 2.0)]
    fresh = _wear_lap(3, 3, 100.0, 0.5)     # the change: 96 -> 100
    fresh["last"] = None                    # ...and its end reading is gone
    laps.append(fresh)
    laps.append(_wear_lap(4, 4, 99.5, 0.5))

    split = analysis.split_stints(laps)
    assert len(split["stints"]) == 2, split
    assert [e["lap"]["id"] for e in split["stints"][0]["laps"]] == [1, 2]
    assert [e["lap"]["id"] for e in split["stints"][1]["laps"]] == [3, 4], (
        "lap 4 belongs with the fresh set, not in a stint of its own")


def test_the_first_stints_reason_does_not_claim_laps_it_never_saw():
    """"First lap of the session" is a claim about data, not a label.

    Handed a slice -- an explicit lap range, or the newest 200 of a long
    session -- the caller knows the first lap may be mid-stint and this
    function does not, so the caller supplies the wording.
    """
    laps = [_wear_lap(1 + i, 50 + i, 100.0 - 0.4 * i, 0.4) for i in range(4)]
    whole = analysis.split_stints(laps)
    assert whole["stints"][0]["started_because"] == "first lap of the session"

    sliced = analysis.split_stints(laps, first_reason="first lap of the range")
    assert sliced["stints"][0]["started_because"] == "first lap of the range"
    # And a real boundary still overrides it rather than inheriting it.
    laps[2]["lap"]["out_lap"] = True
    sliced = analysis.split_stints(laps, first_reason="first lap of the range")
    assert sliced["stints"][1]["started_because"].startswith("out-lap")


def test_an_out_lap_does_not_drag_the_early_wear_rate_down():
    """An out-lap is shorter, and it is always at the start of a stint.

    Averaging its smaller wear in with flying laps depresses the early
    half's rate, which comes back out as a rate RISING later -- which is
    the exact signal this report exists to detect. So the bias would
    manufacture the answer "the tyres went off" on a stint that was flat.
    """
    flying = [_wear_lap(2 + i, 2 + i, 100.0 - 0.4 * i, 0.4) for i in range(8)]
    with_out = [_wear_lap(1, 1, 100.4, 0.1, out_lap=True)] + flying

    steady = analysis.stint_wear(flying)
    biased = analysis.stint_wear(with_out)

    # The rate and the trend ignore the out-lap...
    assert (biased["summary"]["used_per_lap_pct"]
            == steady["summary"]["used_per_lap_pct"]), biased["summary"]
    for w in analysis.WEAR_CORNERS:
        assert abs(biased["trend"][w]["change"]) < 1e-6, biased["trend"]
    # ...but its rubber is still in the total, because it came off.
    assert abs(biased["summary"]["used_total_pct"]["fl"]
               - (steady["summary"]["used_total_pct"]["fl"] + 0.1)) < 1e-6
    assert biased["summary"]["out_laps_excluded_from_rates"] == 1, biased
    assert biased["summary"]["full_laps_measured"] == 8, biased
    s = biased["summary"]
    print(f"  out-lap in the total ({s['used_total_pct']['fl']}%) but out "
          f"of the rate ({s['used_per_lap_pct']['fl']}%/lap), trend flat")


def test_a_stint_that_is_only_an_out_lap_states_no_rate():
    out = analysis.stint_wear([_wear_lap(1, 1, 100.0, 0.1, out_lap=True)])
    assert out["summary"]["used_per_lap_pct"] is None, out["summary"]
    assert "no full lap" in out["summary"]["rate_note"], out["summary"]
    assert out["summary"]["used_total_pct"]["fl"] == 0.1, out["summary"]


# The segmentation only helps if the TOOL does it. The analysis function
# will happily report whatever laps it is handed as one stint, so this
# walks the real MCP tool against a real database and a real pit stop --
# which is where the wrong answer would actually have reached the driver.

_STINT_PROBE = """
import json
from assetto_mcp import db, server

conn = server._conn
sid = db.create_session(conn, car="ks_mazda_mx5_cup", track="mugello",
                        track_config="", tyre_compound="SM",
                        air_temp=24.0, road_temp=31.0)

W = db.SAMPLE_COLUMNS


def row(t_ms, pos, wear):
    d = {c: 0.0 for c in W}
    d.update(t_ms=t_ms, norm_pos=pos, speed_kmh=180.0)
    for c in ("fl", "fr", "rl", "rr"):
        d["wear_" + c] = wear
    return tuple(d[c] for c in W[1:])


# Five laps on one set, a pit stop, five more on a fresh set. Both sets
# wear 0.4%/lap, so both should come back as 2% used -- and the whole
# session run as one stint would report 100 -> 98 across a tyre change.
plan = [(n, 100.0 - 0.4 * (n - 1), False) for n in range(1, 6)]
plan += [(6, 98.0, True)]
plan += [(n, 100.0 - 0.4 * (n - 7), False) for n in range(7, 12)]
for n, start, pitted in plan:
    db.store_lap(conn, sid, n, 113000, True,
                 [row(0, 0.0, start), row(60000, 0.5, start - 0.2),
                  row(113000, 0.99, start - 0.4)], pitted=pitted)

tool = getattr(server.stint_wear, "fn", server.stint_wear)
print(json.dumps({
    "session": json.loads(tool(session_id=sid)),
    "range": json.loads(tool(session_id=sid, from_lap=7, to_lap=11)),
}))
"""


def _run_probe(source: str) -> dict:
    """Run a probe against a throwaway data directory and return its JSON."""
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ)
        env["ASSETTO_MCP_DATA"] = d
        env["ASSETTO_MCP_BRIDGE_PORT"] = "0"  # never the real one
        env["PYTHONPATH"] = os.pathsep.join(
            [str(_HERE.parent), env.get("PYTHONPATH", "")])
        proc = subprocess.run([sys.executable, "-c", source],
                              env=env, capture_output=True, text=True,
                              timeout=120)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)


def test_the_tool_reports_a_session_with_a_pit_stop_as_two_stints():
    """The wrong answer this would have given the driver.

    Sessions keep pit laps on purpose, so a race with a stop in it used to
    go through as one stint: the first set's 100% start against the second
    set's 98% end, reported as one set that lost 2% over eleven laps, with
    both sets thrown into a single early-against-late trend. Each set here
    loses exactly 2% over five laps, and that is what has to come back.
    """
    got = _run_probe(_STINT_PROBE)["session"]
    assert got["stint_count"] == 2, got
    assert [b["lap_number"] for b in got["boundary_laps"]] == [6], got
    for stint in got["stints"]:
        s = stint["summary"]
        assert abs(s["used_total_pct"]["fl"] - 2.0) < 1e-6, stint
        assert s["remaining_at_start_pct"]["fl"] == 100.0, stint
        assert abs(s["remaining_at_end_pct"]["fl"] - 98.0) < 1e-6, stint
    assert got["stints"][0]["lap_numbers"] == [1, 2, 3, 4, 5], got
    assert got["stints"][1]["lap_numbers"] == [7, 8, 9, 10, 11], got
    assert "pit visit" in got["stints"][1]["started_because"], got
    print(f"  11 laps with a stop -> {got['stint_count']} stints, "
          f"2.0% each, pit lap 6 held out")


def test_the_tool_accepts_an_explicit_lap_range():
    """The escape hatch for a boundary the flags did not record."""
    got = _run_probe(_STINT_PROBE)["range"]
    assert got["stint_count"] == 1, got
    assert got["stints"][0]["lap_numbers"] == [7, 8, 9, 10, 11], got
    assert got["lap_range"] == {"from_lap": 7, "to_lap": 11}, got


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


def _two_zone_lap(n=600, spike_slip=4.5, base=0.4):
    """Two straight-line braking zones, one single-sample spike in each.

    The spikes are placed at the END of the first zone and the START of the
    second, which is the arrangement that used to produce a false lockup:
    once the coasting samples between them have been filtered out, the two
    spikes are neighbours in the filtered list even though half a lap of
    telemetry separates them.
    """
    zone_a = range(int(0.20 * n), int(0.26 * n))
    zone_b = range(int(0.60 * n), int(0.66 * n))
    spikes = {zone_a[-1], zone_b[0]}
    out = []
    for i in range(n):
        braking = i in zone_a or i in zone_b
        front = (spike_slip if i in spikes else base) if braking else 0.2
        out.append({
            "norm_pos": i / n,
            "brake": 0.9 if braking else 0.0,
            "steer": 0.02,
            "slip_fl": front, "slip_fr": front * 0.95,
            "slip_rl": 0.3, "slip_rr": 0.28,
        })
    return out


def test_two_spikes_in_different_braking_zones_are_not_one_lockup():
    """A run has to be consecutive in the lap, not in the filtered list.

    Walking the filtered braking samples makes half a lap of coasting
    disappear, so a tick of noise at the end of one braking zone and
    another at the start of the next sit side by side and get reported as a
    two-sample lockup run. Neither wheel ever stopped.
    """
    out = analysis.braking_report(_meta(), _two_zone_lap())
    assert out["front_lockup_run_count"] == 0, out["front_lockup_runs"]
    # The spikes were seen -- they are in the distribution -- just not
    # joined together into a lockup.
    assert out["under_straight_line_braking"]["front"]["max_slip"] == 4.5
    assert (out["under_straight_line_braking"]["front"]
            ["samples_over_lockup_threshold"] == 2), out
    print("  two isolated spikes half a lap apart -> 0 lockup runs, "
          "both still counted in the distribution")


def test_a_run_that_really_is_consecutive_is_still_reported():
    """The guard above must not silence a wheel that actually locked."""
    lap = _two_zone_lap()
    for i in range(130, 136):          # six consecutive samples in zone A
        lap[i]["slip_fl"] = 4.5
        lap[i]["slip_fr"] = 4.3
    out = analysis.braking_report(_meta(), lap)
    assert out["front_lockup_run_count"] == 1, out["front_lockup_runs"]
    assert out["front_lockup_runs"][0]["samples"] >= 6, out


def test_a_lockup_run_is_broken_by_the_driver_lifting_mid_zone():
    """Coming off the brakes ends the run even inside one braking zone."""
    lap = _two_zone_lap()
    for i in range(130, 140):
        lap[i]["slip_fl"] = 4.5
        lap[i]["slip_fr"] = 4.3
    for i in range(134, 136):          # brake released for two samples
        lap[i]["brake"] = 0.1
    out = analysis.braking_report(_meta(), lap)
    assert out["front_lockup_run_count"] == 2, out["front_lockup_runs"]


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


def test_a_car_that_never_rolls_reports_no_roll_gradient():
    """The bug the signed fit exists to kill.

    This car leans a constant 0.8 degrees and its suspension does not move
    at all -- banking, camber, a crooked garage floor, take your pick. The
    old fit took |roll| against |lateral g| through the origin, which turns
    a constant lean into a confident positive gradient: it has to, because
    forcing a line through the origin leaves the offset nowhere to go
    except into the slope.
    """
    out = analysis.attitude_report(
        _meta(), _attitude_lap(roll_per_g=0.0, static_roll_deg=0.8))
    roll = out["roll"]
    assert abs(roll["gradient_deg_per_g"]) < 0.05, roll
    assert abs(roll["static_offset_deg"] - 0.8) < 0.05, roll
    print(f"  constant 0.8 deg lean -> gradient "
          f"{roll['gradient_deg_per_g']} deg/g, offset "
          f"{roll['static_offset_deg']} deg")


def test_a_static_lean_lands_in_the_offset_and_not_in_the_gradient():
    """A banked track must not make the car read as softer than it is."""
    flat = analysis.attitude_report(_meta(), _attitude_lap(roll_per_g=1.2))
    banked = analysis.attitude_report(
        _meta(), _attitude_lap(roll_per_g=1.2, static_roll_deg=1.5))
    assert abs(flat["roll"]["gradient_deg_per_g"] - 1.2) < 0.05, flat
    assert abs(banked["roll"]["gradient_deg_per_g"] - 1.2) < 0.05, banked
    assert abs(banked["roll"]["static_offset_deg"] - 1.5) < 0.05, banked
    assert abs(flat["roll"]["static_offset_deg"]) < 0.05, flat


def test_static_rake_lands_in_the_offset_and_not_in_the_dive_figure():
    """Pitch has the same problem: a car sits nose-down before it brakes."""
    out = analysis.attitude_report(
        _meta(), _attitude_lap(dive_per_g=0.8, static_pitch_deg=0.6))
    assert abs(out["pitch"]["dive_deg_per_g"] - 0.8) < 0.05, out["pitch"]
    assert abs(out["pitch"]["static_offset_deg"] - 0.6) < 0.05, out["pitch"]


def test_a_lap_loaded_in_only_one_direction_says_the_intercept_is_weak():
    """On a one-sided track the intercept is extrapolated, not bracketed.

    The number still gets reported -- it is the best available -- but a
    reader has to be told that the split between static lean and roll rests
    on a fit that never saw the other side of zero.
    """
    both = analysis.attitude_report(_meta(), _attitude_lap())
    assert both["roll"]["loaded_both_directions"] is True, both["roll"]
    assert "gradient_caveats" not in both["roll"], both["roll"]

    oval = analysis.attitude_report(
        _meta(), _attitude_lap(one_direction=True))
    assert oval["roll"]["loaded_both_directions"] is False, oval["roll"]
    assert any("extrapolated" in c
               for c in oval["roll"]["gradient_caveats"]), oval["roll"]


def test_a_lap_whose_roll_is_not_linear_in_g_says_the_fit_was_poor():
    """r2 is what stops a slope through a shapeless cloud being quoted."""
    lap = _attitude_lap(roll_per_g=1.2)
    for i, s in enumerate(lap):
        # Roll that has nothing to do with load: a car crossing kerbs.
        s["roll"] = math.radians(2.5 * math.sin(i * 1.7))
    out = analysis.attitude_report(_meta(), lap)
    assert out["roll"]["fit_r2"] < 0.5, out["roll"]
    assert any("not close to linear" in c
               for c in out["roll"]["gradient_caveats"]), out["roll"]


def test_a_clean_lap_reports_a_fit_that_actually_held():
    out = analysis.attitude_report(_meta(), _attitude_lap(roll_per_g=1.5))
    assert out["roll"]["fit_r2"] > 0.99, out["roll"]
    assert "gradient_caveats" not in out["roll"], out["roll"]


def test_the_signed_slope_is_kept_next_to_the_magnitude():
    """The magnitude alone cannot show a lap that ran the wrong way.

    Which sign means "leaning out of the corner" is AC's convention and
    this project has never checked it, so no direction is claimed -- but
    the sign has to be reported, because what is usable is that it agrees
    across every lap of a car.
    """
    normal = analysis.attitude_report(_meta(), _attitude_lap(roll_per_g=1.5))
    backwards = analysis.attitude_report(
        _meta(), _attitude_lap(roll_per_g=-1.5))
    assert normal["roll"]["gradient_deg_per_g"] == 1.5, normal["roll"]
    assert backwards["roll"]["gradient_deg_per_g"] == 1.5, backwards["roll"]
    # Same magnitude, opposite sign -- and only the signed field shows it.
    assert normal["roll"]["fitted_slope_deg_per_g"] > 0, normal["roll"]
    assert backwards["roll"]["fitted_slope_deg_per_g"] < 0, backwards["roll"]
    print("  two laps at 1.5 deg/g magnitude, opposite signs, told apart")


def test_squat_under_power_is_left_out_of_the_dive_figure():
    """`dive_deg_per_g` has to be dive, not an average of dive and squat.

    Squat is governed by the rear of the car. A lap where the rear squats
    at a different rate from the front's dive must not move the dive
    figure, or a note that says "front spring and damper changes should
    move it" is describing something else.
    """
    lap = _attitude_lap(dive_per_g=0.8)
    for s in lap:
        # A real power zone -- well past the 0.5g that separates driving
        # out of a corner from coasting -- squatting four times as hard as
        # the front dives.
        if 0.60 <= s["norm_pos"] < 0.72:
            s["acc_lon"] = 1.6
            s["pitch"] = math.radians(-3.2 * 1.6)
    out = analysis.attitude_report(_meta(), lap)
    assert abs(out["pitch"]["dive_deg_per_g"] - 0.8) < 0.05, out["pitch"]
    print(f"  rear squatting at 3.2 deg/g -> dive still "
          f"{out['pitch']['dive_deg_per_g']} deg/g")


def test_light_throttle_is_under_power_too():
    """The cut is at zero g, not at the braking threshold.

    A car at +0.3 g is driving out of a corner: the rear is squatting,
    however gently. Cutting at -0.5g would leave the whole 0 to +0.5 band
    in the fit while the payload said power samples were excluded -- true
    of the obvious case and false of the common one.
    """
    lap = _attitude_lap(dive_per_g=0.8)
    # The default lap coasts at +0.3 g. Give that band a squat rate of its
    # own; if it is in the fit, the dive figure moves.
    for s in lap:
        if s["acc_lon"] > 0:
            s["pitch"] = math.radians(-4.0 * s["acc_lon"])
    out = analysis.attitude_report(_meta(), lap)
    assert abs(out["pitch"]["dive_deg_per_g"] - 0.8) < 0.05, out["pitch"]
    assert "at or below zero longitudinal g" in out["pitch"]["dive_note"]


def test_a_lap_that_only_ever_brakes_says_its_intercept_is_extrapolated():
    """Cutting power samples out can leave nothing to anchor the rake.

    Braking-only points are all far from zero, so the split between static
    rake and dive rests on extrapolation. The gradient is still the best
    available and is still reported -- the reader just has to be told.
    """
    both = analysis.attitude_report(_meta(), _attitude_lap())
    assert both["pitch"]["unloaded_samples"] > 0, both["pitch"]
    assert "dive_caveats" not in both["pitch"], both["pitch"]

    # A lap whose only non-power samples are hard braking.
    lap = _attitude_lap()
    for s in lap:
        if -0.5 < s["acc_lon"] <= 0.0:
            s["acc_lon"] = 1.0            # push the coasting band onto power
    out = analysis.attitude_report(_meta(), lap)
    assert out["pitch"]["unloaded_samples"] == 0, out["pitch"]
    assert any("extrapolated" in c
               for c in out["pitch"]["dive_caveats"]), out["pitch"]


def test_a_lap_with_load_but_no_spread_says_so_instead_of_miscounting():
    """The withheld-gradient message has to match the reason.

    A lap held at a constant 1.2g has hundreds of loaded samples and still
    cannot be fitted -- there is no spread in x. Saying "only 700 samples,
    under the 40 needed" would be a false statement produced by reusing
    the wrong branch's text.
    """
    lap = [{"norm_pos": i / 400, "acc_lat": 1.2, "acc_lon": 0.0,
            "roll": math.radians(1.8), "pitch": 0.0} for i in range(400)]
    out = analysis.attitude_report(_meta(), lap)
    assert out["roll"]["gradient_deg_per_g"] is None, out["roll"]
    assert out["roll"]["loaded_samples"] == 400, out["roll"]
    assert "no spread" in out["roll"]["gradient_note"], out["roll"]
    assert "under the" not in out["roll"]["gradient_note"], out["roll"]


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
