"""Judging a setup change against the driver's own repeatability.

Lap time is the noisiest channel on the car. Measured spread across four
laps of an unchanged setup ran 0.3-0.6s, so a change worth less than about
half a second cannot be seen in a short run however well it is driven --
while front load transfer moved 2.2 points for a rear anti-roll bar change
against under 0.3 of noise. Same laps, wildly different resolving power.

These tests are about not overclaiming, and there are two ways to overclaim
here. One is calling a difference smaller than the noise a change. The other
is asking forty questions of the same six laps and reporting whichever came
back loudest: at 95% each that flagged something in 83% of runs where
nothing had been touched, which is not a subtle bias, it is the usual
answer. So the statistics themselves are pinned here, against values from
scipy computed when these tests were written and hardcoded as literals --
scipy is not a dependency of this project and must not become one, on the
gaming PC least of all.

There is a third way, which is to correct so hard that nothing can ever be
found. Correcting across all 38 tests of a fifteen-corner payload held the
false-positive rate at 5% and dropped the chance of catching a real
2.2-point load transfer change in two laps a side from 93% to 7%. So the
family is the eight metrics, which is fixed, and the corners are
exploratory: measured, ranked, reported uncorrected, and asserted by
nothing. Both halves are pinned below -- the family-wise rate by
simulation, the detection rates against scipy, and the fact that a lead is
never called a finding.

The mutations these were written to kill, every one of which the previous
suite passed: the critical-value table set to 2.0 at df 1-2, t(df=4) set to
1.05, resolution multiplied by three, resolution replaced by 999999, the
standard error missing its factor of sqrt(2), and df computed as n1+n2.
Since the split, two more: putting the corner tests back in the family, and
taking the correction out altogether.
"""

import atexit
import json
import os
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module  # noqa: E402

from assetto_mcp import analysis, db  # noqa: E402


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


def _corner(pos, bal=None, spd=None):
    return {"apex_pos": pos, "slip_balance": bal, "min_speed_kmh": spd}


def _close(a, b, tol=5e-4):
    return abs(a - b) <= tol


# --- the statistics themselves ------------------------------------------


def test_the_t_distribution_is_computed_not_looked_up():
    """Reference values from scipy.stats.t, hardcoded at authoring time.

    The old implementation was a fourteen-entry table plus a lookup that
    rounded an untabulated df UP to the next key, so df=21 was judged at
    2.0860 instead of 2.0796 and everything past df=30 at 1.96 -- 3.9% low
    at df=31, and low always means readier to call a change real.

    The table also could not express any level but 95%, which is exactly
    what the family correction needs, so it is gone entirely.
    """
    for t, df, expected in [(1.0, 1, 0.5), (2.0, 1, 0.295167235301),
                            (2.776445, 4, 0.0500000053821),
                            (3.0, 4, 0.0399419680717),
                            (1.0, 4, 0.3739009663),
                            (6.0, 2, 0.0266714732154),
                            (2.079614, 21, 0.0499999843947),
                            (2.039513, 31, 0.0500000471238)]:
        got = analysis._t_p_value(t, df)
        assert abs(got - expected) < 1e-9, (t, df, got, expected)

    # The df the table never held are exactly the ones it got wrong.
    for df, expected in [(1, 12.7062047362), (2, 4.3026527297),
                         (3, 3.1824463053), (4, 2.7764451052),
                         (5, 2.5705818356), (10, 2.2281388520),
                         (21, 2.0796138447), (31, 2.0395134464),
                         (60, 2.0002978220)]:
        got = analysis._t_crit(df, 0.05)
        assert abs(got - expected) < 1e-6, (df, got, expected)

    # And the corrected levels, which no table of 95% values can reach.
    # 0.05/8 is the family: eight metrics, whatever the circuit.
    assert _close(analysis._t_crit(2, 0.05 / 8), 12.5897405612)
    assert _close(analysis._t_crit(4, 0.05 / 8), 5.2610575751)
    print("  p-values and critical values match scipy to 1e-9")


def test_the_documented_detection_rates_are_what_the_code_does():
    """The figures in the docstrings, recomputed from the code.

    Both docstrings tell a model what a "within noise" answer is worth at
    two, three, five and eight laps a side, and a model will act on those
    numbers without any way to check them. They were true of a family of
    38 once and became wrong the moment the family changed, silently, in
    prose. So they are pinned.

    References from scipy.stats.nct at the critical value for 0.05/8, and
    for the two the noncentral t implementation returns nan for, from a
    30-digit mpmath integration of the same quantity.
    """
    def rate(delta, sd, n):
        df, se = 2 * n - 2, sd * (2.0 / n) ** 0.5
        return analysis._t_power(delta / se, df,
                                 analysis._t_crit(df, 0.05 / 8))

    # A 500ms lap gain against this driver's own 0.25s lap-time spread.
    for n, expected in ((2, 0.0307100), (3, 0.1077780),
                        (5, 0.3877880), (8, 0.7656790)):
        assert _close(rate(500.0, 250.0, n), expected, 1e-6), (n, rate)
    # A rear anti-roll bar: 2.2 points of front load transfer against 0.3.
    for n, expected in ((2, 0.2891760), (3, 0.9683558)):
        assert _close(rate(2.2, 0.3, n), expected, 1e-6), (n, rate)
    print(f"  lap time 500ms: {100 * rate(500.0, 250.0, 3):.0f}% at 3 laps a "
          f"side, {100 * rate(500.0, 250.0, 5):.0f}% at 5; load transfer "
          f"2.2pt: {100 * rate(2.2, 0.3, 2):.0f}% at 2, "
          f"{100 * rate(2.2, 0.3, 3):.0f}% at 3")


def test_resolution_is_the_critical_value_times_the_standard_error():
    """Every ingredient of the number pinned at once.

    113400/113600/113300 against 112900/112700/112800: df=4, pooled SD
    129.0995, se 105.4093, difference -633.333, t -6.0083. Judged as a
    family of one, so no correction stands between the arithmetic and the
    reported figure.
    """
    e = analysis._measure([113400, 113600, 113300],
                          [112900, 112700, 112800], 1.0)
    analysis._holm([e])
    analysis._report(e)

    assert e["baseline_n"] == 3 and e["candidate_n"] == 3, e
    assert _close(e["change"], -633.333, 1e-3), e
    # An se missing its sqrt(2) gives 74.54 and a resolution of 207.0; df
    # taken as n1+n2 gives t=2.4469 and 210.9. Both miss by far more than
    # this tolerance, as does any multiple of the right answer.
    assert _close(e["resolution"], 292.663, 1e-3), e
    assert _close(e["p_value"], 0.00386, 1e-5), e
    assert e["verdict"] == "moved", e
    # A real change this much bigger than the resolution is nearly certain
    # to be caught. A change the size of the resolution is not -- see below.
    assert _close(e["power"], 0.99, 5e-3), e
    print(f"  resolution {e['resolution']}ms, p {e['p_value']}, "
          f"power {e['power']}")


def test_resolution_is_a_fifty_percent_threshold_not_a_promise():
    """It is printed to three decimals and detects half the time.

    A resolution reported without that reads as "changes this big will be
    seen", and the docstring did say two laps a side was enough for a large
    effect. It is the point where the coin is fair, nothing more.
    """
    e = analysis._measure([113400, 113600, 113300],
                          [112900, 112700, 112800], 1.0)
    analysis._holm([e])
    analysis._report(e)
    se = 105.40925533894598
    at_threshold = analysis._t_power(e["resolution"] / se, 4,
                                     analysis._t_crit(4, 0.05))
    # Just over half, not exactly half: the spread is estimated from four
    # degrees of freedom rather than known, and the runs that happen to look
    # tidy clear the bar more often than the untidy ones miss it.
    assert 0.5 < at_threshold < 0.62, at_threshold
    print(f"  a real change of exactly {e['resolution']}ms is seen "
          f"{100 * at_threshold:.0f}% of the time")


def test_every_reported_number_matches_the_reference_for_a_known_run():
    """The rear ARB run, end to end, against independently computed values.

    Eight metrics are measured, five constant on both sides, so the family
    is 8 and the two that moved are held to 0.05/8. Adding a metric to
    RUN_METRICS moves these numbers, which is the point: what counts as
    significant depends on how many questions were asked of the same laps.
    """
    base = [_lap(113400, 1.06, 58.1), _lap(113600, 1.07, 57.9)]
    cand = [_lap(113300, 0.95, 55.7), _lap(113500, 0.96, 55.9)]
    out = analysis.compare_runs(base, cand)
    assert out["multiple_comparisons"]["tests_in_family"] == 8, \
        out["multiple_comparisons"]

    load = out["metrics"]["front_load_transfer_pct"]
    assert _close(load["p_value"], 0.00411, 1e-5), load
    assert _close(load["p_value_adjusted"], 0.0329, 1e-4), load
    assert _close(load["resolution"], 1.780, 1e-3), load
    assert _close(load["power"], 0.78, 5e-3), load
    assert load["verdict"] == "moved", load

    slip = out["metrics"]["slip_balance"]
    assert _close(slip["resolution"], 0.089, 1e-3), slip
    assert slip["verdict"] == "moved", slip

    lt = out["metrics"]["lap_time_ms"]
    assert _close(lt["p_value"], 0.553, 1e-4), lt      # 3 significant figures
    assert lt["p_value_adjusted"] == 1.0, lt
    assert _close(lt["resolution"], 832.915, 1e-2), lt
    assert _close(lt["power"], 0.04, 5e-3), lt
    assert lt["verdict"] == "within noise", lt
    print(f"  load transfer resolves to {load['resolution']}%, lap time to "
          f"{lt['resolution']}ms, from the same two laps")


def test_the_family_wise_false_positive_rate_is_held_at_five_percent():
    """Nothing changed, fifteen corners, three laps a side, 300 times.

    This is the rate for the claim the payload actually makes: a metric
    that "moved". Uncorrected, one metric or another cleared 95% in a
    quarter of null runs, and with the corners judged the same way it was
    83%, stated as fact in the summary line a model quotes.

    Fifteen corners rather than six because that is where the old design
    hurt, and because the number below must not move when the circuit
    does: the corners are not in the family, so a null run at Mugello and a
    null run at an oval have to come out the same.

    Seeded, so a regression is a failure rather than a bad afternoon. The
    same seed over 2500 trials gives 5.08%; 300 is what fits in a test.
    """
    rng = random.Random(20260819)
    positions = [0.02 + i * 0.064 for i in range(15)]

    def noise_lap():
        return {
            "lap_time_ms": rng.gauss(113400, 450),
            "overall_slip_balance": rng.gauss(1.05, 0.05),
            "top_speed_kmh": rng.gauss(215, 1.2),
            "peak_lat_g": rng.gauss(2.9, 0.06),
            "time_coasting_pct": rng.gauss(6.5, 0.5),
            "tyres": {"fl": {"core_temp_avg": rng.gauss(100, 1.5),
                             "pressure_end": rng.gauss(28.5, 0.2)}},
            "suspension": {"front_load_transfer_pct": rng.gauss(58, 0.3)},
            "corners": [_corner(p + rng.gauss(0, 0.0008),
                                rng.gauss(1.1, 0.08), rng.gauss(110, 1.5))
                        for p in positions],
        }

    trials, flagged, uncorrected, led = 300, 0, 0, 0
    for _ in range(trials):
        out = analysis.compare_runs([noise_lap() for _ in range(3)],
                                    [noise_lap() for _ in range(3)])
        if any(m.get("verdict") == "moved" for m in out["metrics"].values()):
            flagged += 1
        # The same runs judged the old way: any one metric under 0.05.
        if any((m.get("p_value") if m.get("p_value") is not None else 1.0)
               < 0.05 for m in out["metrics"].values()):
            uncorrected += 1
        if any(t.get("lead") == "worth a look"
               for c in out["corner_leads"] for t in c.values()
               if isinstance(t, dict)):
            led += 1

    rate, old = 100 * flagged / trials, 100 * uncorrected / trials
    assert rate < 12, f"family-wise false positives {rate:.1f}%"
    # If this collapses, the fixture has stopped exercising anything and the
    # assertion above is passing for the wrong reason. It also fails if the
    # correction is removed, which is the point.
    assert old > 15, f"uncorrected rate {old:.1f}% -- check the fixture"
    # The exploratory layer is uncorrected on purpose and this is what that
    # costs. It is not a defect as long as the payload says so, which
    # test_a_corner_lead_is_never_reported_as_a_finding checks that it does.
    assert 100 * led / trials > 40, f"leads on {100 * led / trials:.1f}% of " \
        f"null runs -- the corner fixture has stopped moving"
    print(f"  {rate:.1f}% of null runs named a metric as moved; {old:.1f}% "
          f"would have, judged one metric at a time; "
          f"{100 * led / trials:.0f}% carried an exploratory corner lead")


def test_the_correction_is_stated_in_the_payload():
    """A reader has to be able to see that a correction happened."""
    out = analysis.compare_runs(
        [_lap(113400, 1.06, 58.1), _lap(113600, 1.07, 57.9)],
        [_lap(113300, 0.95, 55.7), _lap(113500, 0.96, 55.9)])
    mc = out["multiple_comparisons"]
    assert mc["method"] == "holm-bonferroni", mc
    assert mc["tests_in_family"] == 8, mc
    assert _close(mc["strictest_threshold"], 0.00625, 1e-5), mc
    assert "5%" in mc["note"], mc
    for m in out["metrics"].values():
        if "p_value" in m:
            assert "p_value_adjusted" in m, m
    print(f"  {mc['tests_in_family']} tests, strictest threshold "
          f"{mc['strictest_threshold']}")


def test_the_corner_count_does_not_change_what_the_metrics_need():
    """The whole reason for the split, in one comparison.

    Two laps a side, a rear anti-roll bar worth 2.2 points of front load
    transfer against under 0.3 of noise -- the case the tool exists for.
    With the corner tests in the family that same run is 38 questions at
    Mugello and 8 at an oval, so the answer depended on where it was
    driven: p 0.0041 against a threshold of 0.05/38, "within noise".

    Fifteen corners of pure noise are added here and must change nothing
    about the verdict, the family size, or the threshold.
    """
    def corners(seed):
        rng = random.Random(seed)
        return [_corner(0.02 + i * 0.064 + rng.gauss(0, 0.0008),
                        rng.gauss(1.1, 0.08), rng.gauss(110, 1.5))
                for i in range(15)]

    base = [_lap(113400, 1.06, 58.1, corners=corners(1)),
            _lap(113600, 1.07, 57.9, corners=corners(2))]
    cand = [_lap(113300, 0.95, 55.7, corners=corners(3)),
            _lap(113500, 0.96, 55.9, corners=corners(4))]
    out = analysis.compare_runs(base, cand)

    mc = out["multiple_comparisons"]
    assert mc["tests_in_family"] == 8, mc
    assert _close(mc["strictest_threshold"], 0.00625, 1e-5), mc
    assert mc["exploratory_tests_not_in_family"] >= 20, mc
    load = out["metrics"]["front_load_transfer_pct"]
    assert load["verdict"] == "moved", load
    # Identical to the corner-free run: the corners cost the metrics nothing.
    assert _close(load["p_value_adjusted"], 0.0329, 1e-4), load
    assert _close(load["resolution"], 1.780, 1e-3), load
    print(f"  {out['corners_compared']} corners on the track, "
          f"{mc['tests_in_family']} tests in the family, load transfer "
          f"{load['verdict']}")


def test_clearing_alone_but_not_the_correction_is_its_own_answer():
    """No evidence and not enough laps are different next steps.

    Reported as a bare "within noise" they read the same, and the engineer
    who would have run three more laps stops instead. p here is 0.0213:
    under 0.05 on its own, over 0.05/8 as a member of the family.
    """
    base = [_lap(113400, 1.05, 58.1), _lap(113400, 1.05, 58.0),
            _lap(113400, 1.05, 57.9)]
    cand = [_lap(113400, 1.05, 57.7), _lap(113400, 1.05, 57.6),
            _lap(113400, 1.05, 57.8)]
    out = analysis.compare_runs(base, cand)
    load = out["metrics"]["front_load_transfer_pct"]

    assert _close(load["p_value"], 0.0213, 1e-4), load
    assert load["p_value_adjusted"] > 0.05, load
    assert load["verdict"] == analysis.SUGGESTIVE, load
    assert load["verdict"] != "moved" and "noise" not in load["verdict"], load
    # And the summary carries it without asserting it.
    assert "nothing moved beyond noise" in out["summary"], out["summary"]
    assert "Suggestive" in out["summary"], out["summary"]
    # The resolution still comes from the corrected level: what it would
    # take to confirm, not what it took to be suggestive.
    assert load["resolution"] > abs(load["change"]), load
    print(f"  {load['change']}% of load transfer: {load['verdict']} "
          f"(p {load['p_value']}, adjusted {load['p_value_adjusted']})")


# --- what the payload says ----------------------------------------------


def test_a_real_change_on_a_quiet_channel_is_seen_in_two_laps():
    """The rear ARB case: load transfer 58.0 -> 55.8, noise under 0.3."""
    base = [_lap(113400, 1.06, 58.1), _lap(113600, 1.07, 57.9)]
    cand = [_lap(113300, 0.95, 55.7), _lap(113500, 0.96, 55.9)]
    out = analysis.compare_runs(base, cand)

    lt = out["metrics"]["front_load_transfer_pct"]
    assert lt["verdict"] == "moved", lt
    assert lt["change"] < -2, lt
    print(f"  load transfer {lt['baseline']} -> {lt['candidate']} "
          f"(resolution {lt['resolution']}) -> {lt['verdict']}")


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
    # Two runs that each repeated exactly are an infinite t and a zero
    # p-value, and 0.01 of load transfer is still not a setup change.
    assert out["metrics"]["front_load_transfer_pct"]["verdict"] == \
        "within noise", out["metrics"]["front_load_transfer_pct"]
    print("  0.0005 of slip balance is not a setup change, however tidy")


def test_a_corner_lead_is_never_reported_as_a_finding():
    """A corner answers where, not whether, and the payload has to say so.

    The summary is the line a model quotes. It used to be computed from the
    metrics alone and so said "nothing moved beyond noise" directly above a
    populated corner list; the fix for that was to count the corners in it,
    which made the summary assert something no correction stood behind.
    Now it carries them and marks them as leads.
    """
    def run(speed):
        return [_lap(113400, 1.05, 58.0,
                     corners=[_corner(0.30 + 0.001 * i, 1.2, speed + 0.4 * i),
                              _corner(0.60, 1.0, 90.0)])
                for i in range(3)]

    out = analysis.compare_runs(run(120.0), run(138.0))
    lead = out["corner_leads"][0]["min_speed_kmh"]
    assert lead["lead"] == "worth a look", lead
    # Not a verdict, not the word the metrics use, and its p is named for
    # what it is: nothing corrected it.
    assert "verdict" not in lead and "moved" not in json.dumps(lead), lead
    assert "p_value_uncorrected" in lead, lead
    assert "p_value_adjusted" not in lead, lead

    assert "nothing moved beyond noise" in out["summary"], out["summary"]
    assert "exploratory leads" in out["summary"], out["summary"]
    assert "not findings" in out["summary"], out["summary"]
    assert "EXPLORATORY" in out["corner_leads_note"], out["corner_leads_note"]
    assert "NOT corrected" in out["corner_leads_note"], out
    print(" ", out["summary"])


def test_corner_leads_are_ranked_by_effect_size_and_capped():
    """Fifteen corners on two channels is thirty tests to print.

    Ranked rather than filtered by significance, because a list showing
    only what cleared 95% is a significance filter under another name and
    reads as a list of findings. The engineer wants the biggest first.
    """
    rng = random.Random(4)
    moved = {3: 12.0, 9: 6.0, 12: 2.0}          # km/h, on three of fifteen

    def run(shift):
        return [_lap(113400, 1.05, 58.0,
                     corners=[_corner(0.02 + k * 0.064 + rng.gauss(0, 0.0005),
                                      1.1, 110.0 + rng.gauss(0, 0.4)
                                      + (moved.get(k, 0.0) if shift else 0.0))
                              for k in range(15)])
                for _ in range(3)]

    out = analysis.compare_runs(run(False), run(True))
    assert out["corners_compared"] == 15, out["corners_compared"]
    assert len(out["corner_leads"]) == analysis.CORNER_LEADS_SHOWN, \
        len(out["corner_leads"])
    sizes = [c["min_speed_kmh"]["effect_size"] for c in out["corner_leads"]]
    assert sizes == sorted(sizes, reverse=True), sizes
    top = [round(c["min_speed_kmh"]["change"])
           for c in out["corner_leads"][:3]]
    assert top == [12, 6, 2], top
    print(f"  15 corners compared, {len(out['corner_leads'])} listed, "
          f"largest first: {top} km/h")


def test_corners_are_matched_by_position_not_by_index():
    """The detector finds a different number of corners on different laps,
    so corner 3 is not reliably the same piece of road twice."""
    base = [_lap(113400, 1.0, 58.0, corners=[_corner(0.858, 1.20, 120.0),
                                             _corner(0.691, 1.10, 110.0)]),
            _lap(113500, 1.0, 58.0, corners=[_corner(0.861, 1.22, 119.5),
                                             _corner(0.689, 1.12, 110.4)])]
    # A different lap finds an extra corner, shifting every index.
    cand = [_lap(113400, 0.9, 56.0, corners=[_corner(0.140, 0.5, 100.0),
                                             _corner(0.857, 0.60, 126.0),
                                             _corner(0.690, 1.09, 110.2)]),
            _lap(113500, 0.9, 56.0, corners=[_corner(0.142, 0.5, 100.4),
                                             _corner(0.859, 0.62, 125.4),
                                             _corner(0.692, 1.11, 110.6)])]
    out = analysis.compare_runs(base, cand)
    flagged = {c["apex_pos"] for c in out["corner_leads"]
               if any(t.get("lead") == "worth a look"
                      for t in c.values() if isinstance(t, dict))}
    assert any(abs(p - 0.86) < 0.03 for p in flagged), out["corner_leads"]
    # 0.691 barely changed and must not be flagged.
    assert not any(abs(p - 0.69) < 0.02 for p in flagged), out["corner_leads"]
    print(f"  flagged {sorted(flagged)} and left the unchanged corner alone")


def test_corner_leads_are_named_by_turn_number():
    """A lead has to be quotable, and an apex position is not.

    "0.858" is a number a driver cannot act on. The same corner is T3 in
    every payload of this comparison -- the leads, the turns table, and the
    summary sentence a model reads out.
    """
    base = [_lap(113400, 1.0, 58.0, corners=[_corner(0.140, 0.5, 100.0),
                                             _corner(0.691, 1.10, 110.0),
                                             _corner(0.858, 1.20, 120.0)]),
            _lap(113500, 1.0, 58.0, corners=[_corner(0.142, 0.5, 100.4),
                                             _corner(0.689, 1.12, 110.4),
                                             _corner(0.861, 1.22, 119.5)])]
    cand = [_lap(113400, 0.9, 56.0, corners=[_corner(0.140, 0.5, 100.2),
                                             _corner(0.690, 1.09, 110.2),
                                             _corner(0.857, 0.60, 132.0)]),
            _lap(113500, 0.9, 56.0, corners=[_corner(0.142, 0.5, 100.1),
                                             _corner(0.692, 1.11, 110.6),
                                             _corner(0.859, 0.62, 131.4)])]
    out = analysis.compare_runs(base, cand)

    named = [t["turn"] for t in out["turns"]]
    assert named == ["T1", "T2", "T3"], out["turns"]
    at = {t["turn"]: t["apex_pos"] for t in out["turns"]}
    assert _close(at["T3"], 0.8588, 1e-3), out["turns"]

    for lead in out["corner_leads"]:
        assert _close(lead["apex_pos"], at[lead["turn"]], 0.01), lead
    moved = next(c for c in out["corner_leads"]
                 if _close(c["apex_pos"], 0.8588, 1e-3))
    assert moved["turn"] == "T3", moved
    assert "T3" in out["summary"], out["summary"]
    print(f"  the corner that moved is T3 in the leads and in: "
          f"...{out['summary'].split('Separately')[-1][:60]}...")


def test_a_corner_only_one_run_drove_is_still_numbered():
    """Two laps a side, and the candidate found a corner the baseline did not.

    Pooled, that corner is on exactly half the laps. Under a majority rule
    it took no number -- so the one corner in the payload that most needs a
    name, the one under corners_in_one_run_only, was the one left with only
    an apex position. Numbering asks for repeatability instead: two laps
    drove it, which is driving rather than an event.
    """
    base = [_lap(113400, 1.0, 58.0, corners=[_corner(0.691, 1.10, 110.0)]),
            _lap(113500, 1.0, 58.0, corners=[_corner(0.689, 1.12, 110.4)])]
    cand = [_lap(113400, 0.9, 56.0, corners=[_corner(0.140, 0.5, 100.0),
                                             _corner(0.690, 1.09, 110.2)]),
            _lap(113500, 0.9, 56.0, corners=[_corner(0.142, 0.5, 100.4),
                                             _corner(0.692, 1.11, 110.6)])]
    out = analysis.compare_runs(base, cand)

    assert [t["turn"] for t in out["turns"]] == ["T1", "T2"], out["turns"]
    only = out["corners_in_one_run_only"]
    assert len(only) == 1, only
    assert only[0]["side"] == "candidate" and only[0]["turn"] == "T1", only
    print(f"  candidate-only corner named {only[0]['turn']} at "
          f"{only[0]['apex_pos']}")


def test_apexes_a_meter_apart_are_the_same_corner():
    """0.0299, 0.0300 and 0.0301 are one corner, not two.

    Bucketing with round(pos / 0.02) * 0.02 put 0.0299 in one bucket and the
    other two in the next, so a three-lap run reported baseline_n 2 -- and on
    a realistic fifteen-corner run every single corner came back n=2 from
    three laps, with df and the noise estimate wrong for all of them.

    Swept along the track rather than tested at one position, because any
    fixed grid of buckets works perfectly for a corner in the middle of one
    and splits a corner sitting on an edge. Which corners are on an edge is
    a property of the circuit, not of the code, so a single position proves
    nothing.
    """
    def run(base):
        return [_lap(113400, 1.0, 58.0,
                     corners=[_corner(base + 0.0001 * i, 1.2, 120.0 + i)])
                for i in range(-1, 2)]

    for base in (0.020, 0.025, 0.030, 0.035, 0.041, 0.045, 0.0501, 0.055):
        laps = run(base)
        out = analysis.compare_runs(laps, run(base))
        assert out["corners_compared"] == 1, (base, out)
        assert not out["corners_in_one_run_only"], (base, out)
        paired, _, _ = analysis._compare_corners(laps, run(base), 0.01)
        assert paired[0]["tests"][0]["baseline_n"] == 3, (base, paired)
        # The apex reported is a real one, not a bucket center 52m from any.
        assert abs(paired[0]["apex_pos"] - base) < 0.0005, (base, paired[0])
    print("  three laps, one corner, n=3, wherever on track it sits")


def test_two_corners_in_one_bucket_are_not_pooled():
    """0.02 of a lap is 105m at Mugello: a hairpin and the kink after it.

    Pooled they gave n=6 from three laps -- df=10 where the data supports 4,
    on samples that are not independent -- and the "corner" reported was the
    mean of two different pieces of road.

    The two here are 0.008 apart, closer than the matching tolerance, so
    proximity alone cannot separate them. What does is that a car passes an
    apex once a lap: two observations from the same lap in one group are
    proof that two corners have been pooled.
    """
    def run(speeds):
        return [_lap(113400, 1.0, 58.0,
                     corners=[_corner(0.030 + 0.0004 * i, 1.2, speeds[0] + i),
                              _corner(0.038 + 0.0004 * i, 1.1, speeds[1] + i)])
                for i in range(3)]

    out = analysis.compare_runs(run((90.0, 150.0)), run((104.0, 164.0)))
    assert out["corners_compared"] == 2, out
    assert len(out["corner_leads"]) == 2, out["corner_leads"]
    for c in out["corner_leads"]:
        assert c["min_speed_kmh"]["baseline_n"] == 3, c
    apexes = sorted(c["apex_pos"] for c in out["corner_leads"])
    assert apexes[1] - apexes[0] > 0.007, apexes
    # Pooled, the two would average to 120 and 134 and read as one corner.
    changes = sorted(round(c["min_speed_kmh"]["change"], 1)
                     for c in out["corner_leads"])
    assert changes == [14.0, 14.0], changes
    print(f"  two corners kept apart at {apexes}, n=3 each")


def test_a_corner_with_no_slip_balance_is_still_judged_on_min_speed():
    """detect_corners drops slip that looks like a glitch, and does so often.

    Intersecting the slip-balance buckets alone then excluded the corner
    from the comparison entirely: a corner gaining 15 km/h on every
    candidate lap, with slip_balance null, produced an empty list.
    """
    def run(speed):
        return [_lap(113400, 1.0, 58.0,
                     corners=[_corner(0.30 + 0.001 * i, None,
                                      speed + 0.4 * i)])
                for i in range(3)]

    out = analysis.compare_runs(run(120.0), run(135.0))
    assert out["corners_compared"] == 1, out
    leads = out["corner_leads"]
    assert leads and "min_speed_kmh" in leads[0], leads
    assert "slip_balance" not in leads[0], leads
    assert leads[0]["min_speed_kmh"]["lead"] == "worth a look", leads
    assert _close(leads[0]["min_speed_kmh"]["change"], 15.0, 1e-6), leads
    print("  corner flagged on min speed alone: +15 km/h")


def test_a_corner_found_in_only_one_run_is_reported():
    """It used to be dropped without a word, which reads as agreement."""
    base = [_lap(113400, 1.0, 58.0, corners=[_corner(0.30, 1.2, 120.0)]),
            _lap(113400, 1.0, 58.0, corners=[_corner(0.301, 1.2, 120.0)])]
    cand = [_lap(113400, 1.0, 58.0, corners=[_corner(0.30, 1.2, 120.0),
                                             _corner(0.72, 1.0, 95.0)]),
            _lap(113400, 1.0, 58.0, corners=[_corner(0.301, 1.2, 120.0),
                                             _corner(0.721, 1.0, 95.0)])]
    out = analysis.compare_runs(base, cand)
    lonely = out["corners_in_one_run_only"]
    assert len(lonely) == 1, lonely
    assert lonely[0]["side"] == "candidate", lonely
    assert abs(lonely[0]["apex_pos"] - 0.7205) < 0.001, lonely
    print("  unmatched corner reported:", lonely)


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


# --- which laps are allowed into the comparison at all ------------------
#
# These reach through the MCP tool because that is where the lap rows are:
# analysis.compare_runs is handed summaries and cannot know a lap was
# abandoned. The server module is imported lazily, against a scratch data
# directory and a bridge on port 0, so that importing this file starts
# nothing on its own.

_SERVER = None


def _server():
    global _SERVER
    if _SERVER is None:
        d = tempfile.mkdtemp(prefix="ac-compare-runs-")
        os.environ["ASSETTO_MCP_DATA"] = d
        os.environ["AC_DOCS_DIR"] = d
        os.environ["ASSETTO_MCP_BRIDGE_PORT"] = "0"
        # Importing the server now starts recording, which is right in the
        # game and wrong here: these tests exercise the tool layer over a
        # database they build themselves, and a collector polling for
        # Assetto Corsa in the background contributes a thread, a second
        # SQLite connection and a retry timer to every one of them.
        #
        # Set before the import because the decision is made at import time,
        # and that is the point -- an import with side effects can only be
        # opted out of before it happens.
        os.environ["ASSETTO_MCP_NO_AUTOSTART"] = "1"
        import importlib
        _SERVER = importlib.import_module("assetto_mcp.server")
        atexit.register(_SERVER._bridge.stop)
    return _SERVER


# A flat lap: no lateral load, so detect_corners finds nothing and the only
# channel that moves is the one each test moves deliberately.
_SAMPLE = (180.0, 1.0, 0.0, 0.0, 4, 9000, 0.0, 0.0,
           0.4, 0.4, 0.3, 0.3, 26.0, 26.0, 26.0, 26.0,
           85.0, 85.0, 85.0, 85.0, 0.02, 0.024, 0)

# Where tyres_out sits inside _SAMPLE, which begins after lap_id, t_ms and
# norm_pos. Derived rather than written as a literal so a migration that
# inserts a column does not silently make these tests drive off a different
# part of the car.
_TYRES_OUT_IN_SAMPLE = db.SAMPLE_COLUMNS.index("tyres_out") - 3


def _store(srv, session_id, lap_number, lap_ms, complete=True,
           wide=False, pitted=False, out_lap=False):
    """Store a lap. `wide` puts four wheels off long enough to count.

    Track limits are scored from the samples now rather than asserted, so a
    test that wants an off-track lap has to actually drive one off.
    """
    samples = [(i * 100, i / 60.0, *_SAMPLE) for i in range(60)]
    if wide:
        off = list(_SAMPLE)
        off[_TYRES_OUT_IN_SAMPLE] = 4
        for i in range(20, 30):
            samples[i] = (i * 100, i / 60.0, *off)
    return db.store_lap(srv._conn, session_id, lap_number, lap_ms, True,
                        samples, complete=complete, pitted=pitted,
                        out_lap=out_lap)


def _run(srv, a, b, **kw):
    return json.loads(srv.compare_runs(",".join(str(i) for i in a),
                                       ",".join(str(i) for i in b), **kw))


def test_a_lap_that_ran_wide_is_compared_and_said_to_have_run_wide():
    """It is still a lap. The corner speeds and brake points happened.

    This used to drop it, on the grounds that one off-track lap turned a
    real 500ms gain into "within noise". True, and the wrong fix: the lap
    was thrown out of every channel to protect one of them, and the driver
    was told nothing. Now it is counted and reported, so a result that
    rests on it can be read as such -- and clean_laps_only is there for
    when the question really is about a clean lap time.
    """
    srv = _server()
    sid = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    base = [_store(srv, sid, 1, 113400), _store(srv, sid, 2, 113600),
            _store(srv, sid, 3, 113300),
            _store(srv, sid, 4, 111780, wide=True)]
    cand = [_store(srv, sid, 5, 112900), _store(srv, sid, 6, 112700),
            _store(srv, sid, 7, 112800)]

    out = _run(srv, base, cand)
    assert "excluded_laps" not in out, out.get("excluded_laps")
    assert out["metrics"]["lap_time_ms"]["baseline_n"] == 4, out["metrics"]
    wide = out["ran_wide"]
    assert len(wide) == 1, wide
    assert wide[0]["lap_id"] == base[-1] and wide[0]["side"] == "baseline"
    assert wide[0]["max_tyres_out"] == 4, wide
    assert wide[0]["excursions"] == 1, wide
    assert "clean_laps_only" in out["ran_wide_note"]
    print("  compared and reported:", wide[0])

    # And excluded on request, by name, with the reason.
    clean = _run(srv, base, cand, clean_laps_only=True)
    assert clean["metrics"]["lap_time_ms"]["baseline_n"] == 3, clean["metrics"]
    assert len(clean["excluded_laps"]) == 1, clean["excluded_laps"]
    assert "ran wide" in clean["excluded_laps"][0]["reason"]
    assert clean["metrics"]["lap_time_ms"]["verdict"] == "moved", \
        clean["metrics"]["lap_time_ms"]
    print("  clean_laps_only:", clean["excluded_laps"][0]["reason"])


def test_the_old_include_invalid_argument_still_works_and_says_so():
    """Renaming a tool argument breaks every caller that passes it.

    `include_invalid=false` meant "drop the laps that ran wide", which is
    now `clean_laps_only=true`. Accepting the old name and ignoring it
    would be worse than removing it: the call would succeed and quietly
    include laps the caller had asked to leave out.
    """
    srv = _server()
    sid = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    base = [_store(srv, sid, 1, 113400), _store(srv, sid, 2, 113600),
            _store(srv, sid, 3, 113300),
            _store(srv, sid, 4, 111780, wide=True)]
    cand = [_store(srv, sid, 5, 112900), _store(srv, sid, 6, 112700),
            _store(srv, sid, 7, 112800)]

    old = _run(srv, base, cand, include_invalid=False)
    assert old["metrics"]["lap_time_ms"]["baseline_n"] == 3, old["metrics"]
    assert len(old["excluded_laps"]) == 1, old["excluded_laps"]
    assert "deprecated" in old, old.keys()
    assert "clean_laps_only" in old["deprecated"], old["deprecated"]

    # include_invalid=True is the current default, and still says it is old.
    kept = _run(srv, base, cand, include_invalid=True)
    assert kept["metrics"]["lap_time_ms"]["baseline_n"] == 4, kept["metrics"]
    assert "deprecated" in kept, kept.keys()

    # Asking for both at once, meaning opposite things, is refused rather
    # than resolved by whichever the code happens to read second.
    clash = _run(srv, base, cand, include_invalid=True, clean_laps_only=True)
    assert "error" in clash and "contradict" in clash["error"], clash

    # And a caller that never knew the old name sees nothing about it.
    plain = _run(srv, base, cand)
    assert "deprecated" not in plain, plain.keys()
    print("  include_invalid honoured, flagged, and refused when contradicted")


def test_a_lap_abandoned_before_the_line_is_dropped_and_named():
    """Its stored time is wall-clock elapsed, not a lap time.

    Laps ending in a crash or a reset to the pits are deliberately stored
    now, so this is live rather than hypothetical: one in the baseline moved
    lap time by +17630ms and the verdict stayed "within noise".
    """
    srv = _server()
    sid = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    base = [_store(srv, sid, 1, 113400), _store(srv, sid, 2, 113600),
            _store(srv, sid, 3, 113300),
            _store(srv, sid, 4, 166300, complete=False)]
    cand = [_store(srv, sid, 5, 112900), _store(srv, sid, 6, 112700),
            _store(srv, sid, 7, 112800)]

    out = _run(srv, base, cand)
    assert out["metrics"]["lap_time_ms"]["verdict"] == "moved", out["metrics"]
    dropped = out["excluded_laps"][0]
    assert dropped["lap_id"] == base[-1], dropped
    assert "abandoned" in dropped["reason"], dropped
    print("  dropped:", dropped["reason"])


def test_two_different_tracks_or_cars_are_refused_by_name():
    """A Suzuka MX-5 run against a Mugello F4 run came back "moved", -37.3s.

    Stated as confidently as a real result, and the payload named neither
    track nor car, so there was nothing in it to notice the mistake by.
    """
    srv = _server()
    a = make_session(srv._conn, track="suzuka", car="ks_mazda_mx5_cup")
    b = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    base = [_store(srv, a, 1, 150400), _store(srv, a, 2, 150600)]
    cand = [_store(srv, b, 1, 113400), _store(srv, b, 2, 113600)]

    out = _run(srv, base, cand)
    assert "error" in out, out
    assert "suzuka" in out["error"] and "mx5" in out["error"], out["error"]
    assert "mugello" in out["error"] and "rss_formula" in out["error"], \
        out["error"]
    print(" ", out["error"])


def test_the_track_and_car_are_stated_in_the_answer():
    """Nothing in the old payload said what had been compared."""
    srv = _server()
    sid = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    base = [_store(srv, sid, 1, 113400), _store(srv, sid, 2, 113600)]
    cand = [_store(srv, sid, 3, 112900), _store(srv, sid, 4, 112700)]
    out = _run(srv, base, cand)
    assert out["track"] == "mugello", out
    assert out["car"] == "rss_formula_rss_4", out
    print(f"  {out['track']} in {out['car']}")


def test_a_side_left_with_no_usable_laps_refuses():
    # Unusable, not merely off track: a lap that ran wide is compared now,
    # so a side has to be emptied by laps whose *times* are not lap times.
    srv = _server()
    sid = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    base = [_store(srv, sid, 1, 113400, pitted=True),
            _store(srv, sid, 2, 0, out_lap=True)]
    cand = [_store(srv, sid, 3, 112900), _store(srv, sid, 4, 112700)]
    out = _run(srv, base, cand)
    assert "error" in out and "baseline" in out["error"], out
    assert len(out["excluded_laps"]) == 2, out
    reasons = sorted(d["reason"] for d in out["excluded_laps"])
    assert any("pit" in r for r in reasons), reasons
    assert any("out-lap" in r for r in reasons), reasons
    print(" ", out["error"])


def test_lap_ids_survive_the_way_a_list_actually_arrives():
    """A trailing separator was refused: "not a lap id: '\\n'".

    These ids arrive as text written by a model, which wraps lines and
    leaves the odd trailing comma. Empty parts were skipped -- but a part
    holding only whitespace is not empty, it is truthy, so it reached int()
    and raised, and a perfectly readable list came back as an error naming a
    newline as a lap id. Stripping spaces up front did not cover it: the
    newline was left in place, and "1 2" was quietly read as lap 12 rather
    than refused.
    """
    srv = _server()
    sid = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    b0, b1 = _store(srv, sid, 1, 113400), _store(srv, sid, 2, 113600)
    c0, c1 = _store(srv, sid, 3, 112900), _store(srv, sid, 4, 112700)
    cand = f"{c0},{c1}"

    clean = json.loads(srv.compare_runs(f"{b0},{b1}", cand))
    assert "error" not in clean, clean
    for spelling in (f"{b0},\n{b1}",       # wrapped between the ids
                     f"{b0},{b1},",        # trailing separator
                     f"{b0},{b1},\n",      # trailing separator, then wrapped
                     f"  {b0} , {b1}\n"):  # indented, and wrapped at the end
        got = json.loads(srv.compare_runs(spelling, cand))
        assert got == clean, (spelling, got.get("error"))
        print(f"  {spelling!r:22s} read as {b0},{b1}")

    # Whitespace alone is not a lap id list, and is answered as an empty
    # list rather than by blaming whichever character it tripped over.
    out = json.loads(srv.compare_runs(" \n ", cand))
    assert out.get("error") == "need lap ids on both sides", out
    print(" ", out["error"])

    # Genuinely unreadable input still refuses, and still quotes the part it
    # could not read so the caller can see which one to fix. "12 13" is in
    # there because removing the spaces used to turn it into lap 1213.
    for bad in (f"{b0};{b1}", "abc", f"{b0} {b1}"):
        out = json.loads(srv.compare_runs(bad, cand))
        assert out.get("error") == f"not a lap id: {bad!r}", (bad, out)
        print(" ", out["error"])


def test_a_run_with_no_cornering_does_not_claim_a_shared_reference():
    """corner_detection has to agree with itself.

    When no lap on either side carries enough lateral load, there is no
    shared bar: lat_g_reference returns None and every lap falls back to its
    own peak. The note was appended unconditionally and said a single shared
    reference had been used across both sides -- flatly contradicting the
    `basis` field beside it, for the one reader who looks at both, which is
    someone debugging a corner list that surprised them.
    """
    srv = _server()
    sid = make_session(srv._conn, track="mugello",
                       car="rss_formula_rss_4")
    # A flat lap: _SAMPLE carries no lateral g at all, so nothing corners.
    base = [_store(srv, sid, n, 113000 + n * 40) for n in (1, 2, 3)]
    cand = [_store(srv, sid, n, 113100 + n * 40) for n in (4, 5, 6)]

    out = _run(srv, base, cand)
    cd = out["corner_detection"]
    assert cd["basis"] == "this lap's own cornering load", cd
    assert "laps_in_reference" not in cd, cd
    assert "no lap on either side" in cd["note"], cd
    assert "one lateral-g reference" not in cd["note"], cd
    print(f"  {cd['basis']!r}; note agrees with it")


# --- lap-time consistency -----------------------------------------------


def _times(*secs, base=113.0):
    """Lap summaries that differ only in lap time, `secs` off `base`."""
    return [_lap(round((base + v) * 1000), 1.0, 58.0) for v in secs]


def test_a_run_that_tightens_up_is_confirmed():
    """The question the means cannot ask.

    Eight laps a side at the same average pace, and the candidate's spread
    has collapsed. Lap time's mean reads "within noise", which is true and
    beside the point; the spread test is what says the change did
    something.
    """
    base = _times(1.0, -0.8, 0.9, -1.1, 0.7, -0.6, 1.2, -0.9)
    cand = _times(0.05, -0.04, 0.02, -0.03, 0.06, -0.05, 0.01, 0.0)
    out = analysis.compare_runs(base, cand)
    c = out["metrics"]["lap_time_consistency"]

    assert c["direction"] == "tighter", c
    assert c["verdict"] == "moved", c
    # The most lopsided of all C(16, 8) = 12870 relabellings, and its mirror.
    assert c["p_value"] == analysis._sig(2 / 12870), c
    assert out["metrics"]["lap_time_ms"]["verdict"] == "within noise"
    assert out["multiple_comparisons"]["tests_in_family"] == 9, \
        out["multiple_comparisons"]
    assert "lap-time consistency (tighter)" in out["summary"], out["summary"]
    print(f"  sd {c['baseline_sd']} -> {c['candidate_sd']} ms, "
          f"p {c['p_value']}, {c['verdict']}")


def test_one_spin_in_a_run_is_not_a_change_in_consistency():
    """The F test's failure, pinned.

    One lap in six lost five seconds and the rest are as repeatable as the
    other run. A variance-ratio F test calls that a change in spread; under
    no change at all it does so in over 40% of runs where a spin happens
    now and then. Relabelling the laps knows that a spin landing in one run
    rather than the other is a coin toss.
    """
    base = _times(0.2, -0.3, 0.1, 5.0, -0.2, 0.3)
    cand = _times(0.25, -0.2, 0.15, -0.35, 0.3, -0.1)
    c = analysis.compare_runs(base, cand)["metrics"]["lap_time_consistency"]
    assert c["verdict"] != "moved", c
    assert c["p_value"] > 0.05, c
    print(f"  one 5s spin: p {c['p_value']}, {c['verdict']}")


def test_removing_two_spins_from_six_laps_is_not_yet_evidence():
    """Sebring v9, and why no valid test would have confirmed it.

    Six laps inside a second after a run where two of six spun. If one lap
    in three spins by chance, six laps without one happens 9% of the time,
    so these laps cannot separate "the setup fixed it" from "not this
    time". The payload has to say "within noise" here. It is the honest
    answer, and the backlog entry that asked for an F test was asking for a
    test that gets this case right by being wrong four times in ten.
    """
    base = _times(0.1, -0.2, 0.3, 5.1, -0.1, 6.4)
    cand = _times(0.0, 0.4, -0.3, 0.6, -0.2, 0.2)
    c = analysis.compare_runs(base, cand)["metrics"]["lap_time_consistency"]
    assert c["direction"] == "tighter", c
    assert c["verdict"] == "within noise", c
    assert c["p_value"] > 0.2, c
    print(f"  the Sebring shape: p {c['p_value']}")


def test_too_few_laps_to_test_says_how_many_it_would_take():
    """Three laps a side cannot confirm a change in spread of any size.

    Twenty relabellings, two of them the most lopsided: nothing can come in
    under p = 0.1. Counting that test in the family would have raised every
    other metric's bar for a test that could never reject, so it is left
    out, and the family stays at eight.
    """
    base = _times(1.0, -1.0, 0.8)
    cand = _times(0.02, -0.01, 0.0)
    out = analysis.compare_runs(base, cand)
    c = out["metrics"]["lap_time_consistency"]
    assert c["verdict"] == "too few laps to test", c
    assert c["smallest_possible_p"] == 0.1, c
    assert c["laps_needed_a_side"] == 6, c
    assert "p_value" not in c, c
    assert out["multiple_comparisons"]["tests_in_family"] == 8, \
        out["multiple_comparisons"]
    print(f"  3 a side: {c['verdict']}, needs {c['laps_needed_a_side']}")


def test_a_run_with_spins_does_not_invent_consistency_changes():
    """Nothing changed, spins happen, six laps a side, 150 times.

    The lap-time model the F test fails on: a 0.3s spread and a 15% chance
    of a 3-8s spin on any lap, the same on both sides. Seeded; the design
    simulated over 600 such runs came in at 0.3%.
    """
    rng = random.Random(20260910)

    def run():
        return _times(*[rng.gauss(0, 0.3)
                        + (rng.uniform(3, 8) if rng.random() < 0.15 else 0)
                        for _ in range(6)])

    trials = 150
    moved = sum(1 for _ in range(trials)
                if analysis.compare_runs(run(), run())["metrics"]
                ["lap_time_consistency"].get("verdict") == "moved")
    assert moved <= trials * 0.05, f"{moved} of {trials} null runs moved"
    print(f"  {moved} of {trials} null runs with spins called it moved")


def test_the_smallest_p_is_what_the_code_can_actually_return():
    """Exact below the enumeration cap, the drawn estimate's floor above it.

    Ten laps a side is 184756 relabellings, so they are drawn, and a drawn
    p is (hits + 1) / (draws + 1): never under 1/20001, even though one
    relabelling in 184756 is rarer. Reporting the rarer figure would
    promise a p the estimate cannot produce.
    """
    assert analysis._smallest_permutation_p(6, 6) == 2 / 924
    floor = 1 / (analysis.PERMUTATION_DRAWS + 1)
    assert analysis._smallest_permutation_p(10, 10) == floor
    assert 2 / 184756 < floor


def test_a_threshold_no_lap_count_can_meet_is_said_not_searched_for():
    """A family big enough to push 0.05/m under the drawn floor.

    No number of laps gets a drawn p below 1/20001, so the search for how
    many laps it would take has no answer -- and used to loop forever
    looking for one.
    """
    c = analysis._measure_consistency([1.0, -1.0, 0.8], [0.0, 0.1, -0.1],
                                      family_size=5000)
    assert c["verdict"] == "too few laps to test", c
    assert c["laps_needed_a_side"] is None, c


def test_the_leads_note_counts_the_channels_and_tests_it_had():
    """Five channels a corner now, and the null-lead rate rises with them.

    The note said "up to two channels each" and quoted 77.6% -- the rate
    measured for thirty tests -- after the entry channels had taken a
    corner to five.
    """
    base = [_lap(113400, 1.0, 58.0, corners=[_entered(0.4, 1.10, b)])
            for b in (-0.20, -0.21, -0.19)]
    cand = [_lap(113400, 1.0, 58.0, corners=[_entered(0.4, 1.12, b)])
            for b in (0.10, 0.11, 0.09)]
    note = analysis.compare_runs(base, cand)["corner_leads_note"]
    assert f"up to {len(analysis.CORNER_CHANNELS)} channels" in note, note
    assert "two channels" not in note and "77.6" not in note, note
    print(f"  ...{note[note.index('compared on'):][:90]}...")


# --- entry phase, through the comparison --------------------------------


def _entered(pos, apex_bal, entry_bal):
    return {"apex_pos": pos, "slip_balance": apex_bal, "min_speed_kmh": 110.0,
            "entry_phase": {"from": "brake point", "slip_balance": entry_bal,
                            "steer_norm": 0.3, "yaw_rate_peak_deg_s": 30.0}}


def test_a_change_on_entry_shows_even_when_the_apex_did_not_move():
    """claude_sebring_v6: a diff change aimed at entry, and nowhere to see it.

    The apex balance is identical across the two runs. Only the entry
    phase moved -- the car stopped being loose under braking -- and before
    the entry channels existed this comparison reported no lead at all.
    """
    base = [_lap(113400, 1.0, 58.0, corners=[_entered(0.4, 1.10, b)])
            for b in (-0.20, -0.21, -0.19)]
    cand = [_lap(113400, 1.0, 58.0, corners=[_entered(0.4, 1.10, b)])
            for b in (0.10, 0.11, 0.09)]
    lead = analysis.compare_runs(base, cand)["corner_leads"][0]
    assert lead["entry_slip_balance"]["lead"] == "worth a look", lead
    assert lead["slip_balance"]["lead"] == "quiet", lead
    print(f"  entry balance {lead['entry_slip_balance']['baseline']} -> "
          f"{lead['entry_slip_balance']['candidate']}; apex unchanged")


# --- turn numbering, through the tools ----------------------------------
#
# analysis.corner_map is tested on corner dicts in test_delta_and_corners.
# What is tested here is the wiring: that the numbering a session gets is
# the same numbering every lap of it is labelled against, which is a
# property of the server and not of the analysis.

_CORNERS = ((0.15, 2.4, 1), (0.45, 2.2, 1), (0.80, 2.6, -1))


def _cornering(n=600, corners=_CORNERS, skip=()):
    """Samples for a lap that corners at these positions.

    `skip` drops a corner by its 1-based place, standing in for the light
    corner a lap's detector loses under the bar. Triangular rather than
    smooth: the shape does not matter here, only that the lateral load is
    over the threshold for more than CORNER_MIN_SAMPLES ticks.
    """
    half = 0.04
    rows = []
    for i in range(n):
        pos = i / n
        lat = 0.0
        for k, (apex, g, sign) in enumerate(corners, 1):
            d = abs(pos - apex)
            if k not in skip and d < half:
                lat = sign * g * (1.0 - d / half)
        turning = abs(lat) > 0.4
        rows.append((i * 100, pos, 220.0 * (1.0 - min(0.6, abs(lat) / 4.0)),
                     0.0 if turning else 1.0, 0.8 if turning else 0.0,
                     0.5 if turning else 0.0, 3 if turning else 6, 9000,
                     lat, -1.0 if turning else 0.2,
                     1.4, 1.4, 0.5, 0.5, 26.0, 26.0, 26.0, 26.0,
                     85.0, 85.0, 85.0, 85.0, 0.02, 0.024, 0))
    return rows


def _tool(srv, name):
    """The function behind an MCP tool, whatever the decorator returns."""
    fn = getattr(srv, name)
    return getattr(fn, "fn", fn)


def test_the_tools_number_the_turns_and_label_every_laps_corners():
    """One numbering per session, and every lap read against it.

    The lap here misses its middle corner, the way a lap near the detection
    bar does. Its own corner list closes the gap -- the third corner becomes
    its second -- so this is exactly the lap that used to make "corner 2"
    mean two different pieces of road inside one session.
    """
    srv = _server()
    sid = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    for n in range(1, 4):
        db.store_lap(srv._conn, sid, n, 113000 + n, True, _cornering())
    thin = db.store_lap(srv._conn, sid, 4, 113400, True,
                        _cornering(skip=(2,)))

    turns = json.loads(_tool(srv, "track_corners")(session_id=sid))
    assert [t["turn"] for t in turns["turns"]] == ["T1", "T2", "T3"], turns
    apexes = [t["apex_pos"] for t in turns["turns"]]
    want = (0.15, 0.45, 0.80)
    assert all(abs(a - b) < 0.02 for a, b in zip(apexes, want)), apexes
    assert turns["built_from_laps"], turns
    assert turns["turns"][2]["turn_sign"] == -1, turns["turns"][2]

    lap = json.loads(_tool(srv, "lap_summary")(lap_id=thin))
    assert [c["turn"] for c in lap["corners"]] == ["T1", "T3"], lap["corners"]
    assert [c["corner"] for c in lap["corners"]] == [1, 2], lap["corners"]
    # Same numbering in both payloads, not two maps that happen to agree.
    assert abs(lap["corners"][1]["apex_pos"] - apexes[2]) < 0.02, \
        lap["corners"]
    print(f"  turns {[t['turn'] for t in turns['turns']]} at {apexes}; "
          f"the lap missing T2 reports T1, T3")


def test_a_lap_is_detected_against_the_bar_its_turn_numbers_came_from():
    """Corners and their numbers have to come from the same threshold.

    lap_summary used to hold a single lap to its own peak lateral g, which
    is a different bar from the one the session's numbering was built
    against -- so a corner could exist on the lap and not in the map, or
    the reverse, and the labelling would be matching apexes across two
    different corner lists. The payload says which bar it used.
    """
    srv = _server()
    sid = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    ids = [db.store_lap(srv._conn, sid, n, 113000 + n, True, _cornering())
           for n in range(1, 4)]

    lap = json.loads(_tool(srv, "lap_summary")(lap_id=ids[0]))
    detection = lap["corner_detection"]
    assert detection["basis"].startswith("shared across"), detection
    assert "turn numbers" in detection["basis"], detection
    assert detection["laps_in_reference"] == 3, detection
    print(f"  basis: {detection['basis']}")


def test_the_numbering_pages_back_past_laps_it_cannot_use():
    """Newest usable laps, however far back they are.

    The search reads a page of laps at a time instead of the whole session,
    so a session whose newest laps are all out-laps is the case that proves
    it keeps paging: stopping at the first page would find nothing to number
    and report a session that plainly cornered as one that never did.
    """
    srv = _server()
    sid = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    usable = [db.store_lap(srv._conn, sid, n, 113000 + n, True, _cornering())
              for n in range(1, 4)]
    newer = srv.CORNER_MAP_PAGE + 5
    for n in range(4, 4 + newer):
        db.store_lap(srv._conn, sid, n, 113000, True,
                     [(i * 100, i / 60.0, *_SAMPLE) for i in range(60)],
                     out_lap=True)

    out = json.loads(_tool(srv, "track_corners")(session_id=sid))
    assert sorted(out["built_from_laps"]) == usable, out["built_from_laps"]
    assert [t["turn"] for t in out["turns"]] == ["T1", "T2", "T3"], out
    print(f"  {newer} out-laps on top; built from laps {usable}")


def test_a_session_with_nothing_to_number_says_which_kind_of_nothing():
    """"No turns" has two causes and they need different answers.

    A session with no usable laps is answered by driving one. A session
    whose laps carry no cornering load at all is answered by looking at the
    laps -- and reporting the first for the second sent a reader hunting a
    bug in corner detection on a session that had never stored a flying lap.
    """
    srv = _server()
    empty = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    out = json.loads(_tool(srv, "track_corners")(session_id=empty))
    assert out["turns"] == [] and "no laps" in out["error"], out

    flat = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    db.store_lap(srv._conn, flat, 1, 113000, True,
                 [(i * 100, i / 60.0, *_SAMPLE) for i in range(60)])
    out = json.loads(_tool(srv, "track_corners")(session_id=flat))
    assert out["turns"] == [], out
    assert "cornering load" in out["error"], out
    print(f"  {out['error']}")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
