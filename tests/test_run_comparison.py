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

from ac_race_engineer import analysis, db  # noqa: E402


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
        os.environ["AC_ENGINEER_DATA"] = d
        os.environ["AC_DOCS_DIR"] = d
        os.environ["AC_ENGINEER_BRIDGE_PORT"] = "0"
        # Importing the server now starts recording, which is right in the
        # game and wrong here: these tests exercise the tool layer over a
        # database they build themselves, and a collector polling for
        # Assetto Corsa in the background contributes a thread, a second
        # SQLite connection and a retry timer to every one of them.
        #
        # Set before the import because the decision is made at import time,
        # and that is the point -- an import with side effects can only be
        # opted out of before it happens.
        os.environ["AC_ENGINEER_NO_AUTOSTART"] = "1"
        import importlib
        _SERVER = importlib.import_module("ac_race_engineer.server")
        atexit.register(_SERVER._bridge.stop)
    return _SERVER


# A flat lap: no lateral load, so detect_corners finds nothing and the only
# channel that moves is the one each test moves deliberately.
_SAMPLE = (180.0, 1.0, 0.0, 0.0, 4, 9000, 0.0, 0.0,
           0.4, 0.4, 0.3, 0.3, 26.0, 26.0, 26.0, 26.0,
           85.0, 85.0, 85.0, 85.0, 0.02, 0.024, 0)


def _store(srv, session_id, lap_number, lap_ms, valid=True, complete=True):
    samples = [(i * 100, i / 60.0, *_SAMPLE) for i in range(60)]
    return db.store_lap(srv._conn, session_id, lap_number, lap_ms, valid,
                        samples, complete=complete)


def _run(srv, a, b, **kw):
    return json.loads(srv.compare_runs(",".join(str(i) for i in a),
                                       ",".join(str(i) for i in b), **kw))


def test_an_invalidated_lap_is_dropped_and_named():
    """One off-track lap turned a real 500ms gain into "within noise".

    Measured on the 3v3 below with the invalid lap left in: change -1620ms,
    resolution 3.4s, verdict "within noise". The flag was sitting in the row
    the whole time and nothing read it.
    """
    srv = _server()
    sid = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    base = [_store(srv, sid, 1, 113400), _store(srv, sid, 2, 113600),
            _store(srv, sid, 3, 113300),
            _store(srv, sid, 4, 111780, valid=False)]
    cand = [_store(srv, sid, 5, 112900), _store(srv, sid, 6, 112700),
            _store(srv, sid, 7, 112800)]

    out = _run(srv, base, cand)
    assert out["metrics"]["lap_time_ms"]["verdict"] == "moved", out["metrics"]
    assert _close(out["metrics"]["lap_time_ms"]["change"], -633.333, 1e-3)
    assert len(out["excluded_laps"]) == 1, out["excluded_laps"]
    dropped = out["excluded_laps"][0]
    assert dropped["lap_id"] == base[-1], dropped
    assert dropped["side"] == "baseline", dropped
    assert "invalidated" in dropped["reason"], dropped
    print("  dropped:", dropped["reason"])

    # Kept when explicitly asked for, and the payload says what that did.
    kept = _run(srv, base, cand, include_invalid=True)
    assert "excluded_laps" not in kept, kept
    assert kept["metrics"]["lap_time_ms"]["verdict"] == "within noise", \
        kept["metrics"]["lap_time_ms"]
    assert "warning" in kept, kept
    print("  include_invalid=True:",
          kept["metrics"]["lap_time_ms"]["change"], "ms, within noise")


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
    srv = _server()
    sid = make_session(srv._conn, track="mugello", car="rss_formula_rss_4")
    base = [_store(srv, sid, 1, 113400, valid=False),
            _store(srv, sid, 2, 113600, valid=False)]
    cand = [_store(srv, sid, 3, 112900), _store(srv, sid, 4, 112700)]
    out = _run(srv, base, cand)
    assert "error" in out and "baseline" in out["error"], out
    assert len(out["excluded_laps"]) == 2, out
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


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
