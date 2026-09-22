"""Turning a lap's samples into the numbers an engineer reasons about.

slip_balance is the one number this tool exists to produce -- positive means
the front is sliding more, and the setup advice that follows depends on its
sign. AC occasionally emits a wheelSlip in the tens of thousands, and a
single such sample moved a balance of 1.4 to 6002, so what gets filtered out
and what survives is load-bearing in both directions.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402

from assetto_mcp import analysis  # noqa: E402

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


# --- the entry phase ----------------------------------------------------


def _entry(n=40, apex=30, brake_until=12, yaw_deg_s=20.0, heading=0.0,
           rear_on_entry=0.5, with_heading=True, dt=40):
    """A corner entry: on the brakes from sample 0, turning in from 8.

    The car rotates at `yaw_deg_s` from turn-in to the apex, so the
    rotation by the apex is 23 steps of yaw_deg_s * dt: 18.4 degrees at the
    defaults. Heading is wrapped to -pi..pi the way AC reports it.
    `rear_on_entry` is the rear slip before sample 20, for a car that is
    loose under braking and settled by the apex.
    """
    out = []
    h = heading
    for i in range(n):
        if 8 <= i <= apex:
            h += math.radians(yaw_deg_s) * dt / 1000.0
        h = (h + math.pi) % (2 * math.pi) - math.pi
        rear = rear_on_entry if i < 20 else 0.5
        s = {"t_ms": i * dt, "norm_pos": i / 100, "speed_kmh": 150.0 - i,
             "gear": 3, "brake": 0.8 if i < brake_until else 0.0,
             "gas": 0.0 if i < apex else 1.0,
             "steer": 0.3 if i >= 8 else 0.0,
             "slip_fl": 1.4, "slip_fr": 1.4, "slip_rl": rear, "slip_rr": rear}
        if with_heading:
            s["heading"] = h
        out.append(s)
    return out


def test_the_entry_is_measured_from_the_brake_point_to_the_apex():
    stats = analysis._corner_stats(_entry(), 30, 38)
    e = stats["entry_phase"]
    assert e["from"] == "brake point", e
    assert e["from_pos"] == stats["brake_point_pos"] == 0.0, (e, stats)
    assert abs(e["rotation_deg"] - 18.4) < 0.2, e
    assert abs(e["yaw_rate_peak_deg_s"] - 20.0) < 0.5, e
    assert abs(e["steer_norm"] - 0.3 * 23 / 31) < 0.01, e
    print(f"  from {e['from']} at {e['from_pos']}: rotated "
          f"{e['rotation_deg']} deg, peak {e['yaw_rate_peak_deg_s']} deg/s")


def test_loose_on_entry_and_pushing_at_the_apex_are_both_visible():
    """The shape the apex figure alone cannot show.

    The rear slides more than the front under braking and the car has
    settled into understeer by the apex. The apex balance says understeer
    and nothing else; the entry balance says where the problem the driver
    is complaining about actually is.
    """
    stats = analysis._corner_stats(_entry(rear_on_entry=2.4), 30, 38)
    assert stats["entry_phase"]["slip_balance"] < 0 < stats["slip_balance"], \
        stats
    print(f"  entry {stats['entry_phase']['slip_balance']}, "
          f"apex {stats['slip_balance']}")


def test_heading_wrapping_past_pi_is_not_a_spin():
    """AC wraps heading at +-pi; differenced raw, that is 6.2 rad a tick.

    Started just short of pi, the car crosses the wrap part-way through the
    entry, and the rotation has to come out the same as anywhere else.
    """
    e = analysis._corner_stats(_entry(heading=math.pi - 0.1), 30, 38)
    e = e["entry_phase"]
    assert abs(e["rotation_deg"] - 18.4) < 0.2, e
    assert e["yaw_rate_peak_deg_s"] < 21, e


def test_a_heading_glitch_is_dropped_not_reported_as_a_snap():
    samples = _entry()
    h = samples[15]["heading"] + math.pi / 2
    samples[15]["heading"] = (h + math.pi) % (2 * math.pi) - math.pi
    e = analysis._corner_stats(samples, 30, 38)["entry_phase"]
    # Both steps touching the bad tick go, and with them 1.6 real degrees.
    assert 16.0 < e["rotation_deg"] < 18.5, e
    assert e["yaw_rate_peak_deg_s"] < 25, e


def test_a_sweeper_taken_without_braking_is_measured_from_turn_in():
    samples = _entry(brake_until=0)
    e = analysis._corner_stats(samples, 30, 38, 0, 8)["entry_phase"]
    assert e["from"] == "turn-in" and e["from_pos"] == 0.08, e
    # And with nothing to start from at all, it says so rather than guess.
    assert analysis._corner_stats(samples, 30, 38)["entry_phase"] is None


def test_a_lap_without_heading_still_reports_the_rest_of_the_entry():
    """Laps from before heading was logged: no rotation, and not zero."""
    e = analysis._corner_stats(_entry(with_heading=False), 30, 38)
    e = e["entry_phase"]
    assert e["rotation_deg"] is None and e["yaw_rate_peak_deg_s"] is None, e
    assert e["slip_balance"] is not None and e["steer_norm"] is not None, e


# --- contacts -----------------------------------------------------------


def _damaged(levels):
    """Samples 40ms apart; `levels` maps an index to the damage from it."""
    out, d = [], 0.0
    for i in range(100):
        d = levels.get(i, d)
        out.append({"t_ms": i * 40, "norm_pos": i / 100, "damage": d})
    return out


def test_a_contact_is_reported_where_the_damage_went_up():
    """One scrape across two ticks is one contact; a repair is not one."""
    out = analysis._contacts(_damaged({20: 0.5, 21: 1.2, 60: 2.0, 80: 0.0}))
    assert out == [{"pos": 0.2, "damage_added": 1.2},
                   {"pos": 0.6, "damage_added": 0.8}], out
    print(f"  {out}")


def test_no_damage_and_no_damage_column_are_different_answers():
    assert analysis._contacts(_damaged({})) == []
    assert analysis._contacts([{"t_ms": 0, "norm_pos": 0.0}]) is None


# --- contacts inferred without the damage counter ---------------------------
#
# The ego car drives along +x at 50 m/s, one sample every 40 ms, and the lap
# completes at wall clock 1000.0 after 4000 ms -- so the sample at t_ms 2000
# was taken at 998.0 with the car at x=100.

def _driving(spikes=None, acc=True, heading=0.0):
    out = []
    for i in range(100):
        s = {"t_ms": i * 40, "norm_pos": i / 100, "speed_kmh": 180.0,
             "gas": 1.0, "brake": 0.0, "heading": heading,
             "pos_x": i * 0.04 * 50, "pos_z": 0.0}
        if acc:
            s["acc_lat"], s["acc_lon"] = 0.0, 0.0
        for k, v in (spikes or {}).get(i, {}).items():
            s[k] = v
        out.append(s)
    return out


_LAP = {"completed_at": 1000.0, "lap_time_ms": 4000}


def _rival(car, created_at, x, t_ms=None, speed=170.0):
    return {"car_index": car, "created_at": created_at, "t_ms": t_ms,
            "pos_x": x, "pos_z": 0.0, "speed_kmh": speed}


def test_a_spike_with_a_car_alongside_is_a_contact():
    """5 g with a car 2 m away at the same instant, on the throttle."""
    out = analysis.infer_contacts(
        _LAP, _driving({50: {"acc_lat": 5.0}, 51: {"acc_lon": -4.0}}),
        [_rival(7, 998.0, 102.0)], {7: "Lily"})
    assert len(out) == 1, out
    c = out[0]
    assert c["verdict"] == "contact" and c["peak_g"] == 5.0, c
    assert c["axis"] == "lat" and c["input"] == "throttle", c
    assert c["nearest"] == {"car_index": 7, "driver_name": "Lily",
                            "distance_m": 2.0, "speed_kmh": 170}, c
    assert c["pos"] == 0.5 and c["t_ms"] == 2000, c
    print(f"  {c}")


def test_a_spike_with_nobody_near_is_a_kerb_when_the_car_carries_on():
    out = analysis.infer_contacts(
        _LAP, _driving({50: {"acc_lon": -3.5}}), [_rival(7, 998.0, 160.0)])
    assert out[0]["verdict"] == "kerb", out
    assert out[0]["speed_lost_kmh"] == 0, out
    assert out[0]["nearest"]["distance_m"] == 60.0, out
    assert out[0]["nearest"]["driver_name"] is None, out


def test_a_spike_with_nobody_near_is_a_wall_when_the_speed_goes_at_once():
    """A 12 g stop from 180 to 40 km/h in the instant of the spike, with
    the nearest car 60 m away: a barrier."""
    spikes = {50: {"acc_lon": -12.0, "speed_kmh": 60.0}}
    for i in range(51, 62):
        spikes[i] = {"speed_kmh": 40.0}
    out = analysis.infer_contacts(
        _LAP, _driving(spikes), [_rival(7, 998.0, 160.0)])
    assert out[0]["verdict"] == "wall", out
    assert out[0]["speed_lost_kmh"] == 140, out


def test_speed_lost_slowly_after_a_spike_is_sand_not_a_wall():
    """A 4 g kerb launch into the gravel: 1.5 g of deceleration for the
    next second takes 50 km/h off, but not in the instant of the spike."""
    spikes = {50: {"acc_lat": 4.0}}
    for i in range(51, 76):
        spikes[i] = {"speed_kmh": 180.0 - 2.0 * (i - 50)}
    out = analysis.infer_contacts(
        _LAP, _driving(spikes), [_rival(7, 998.0, 160.0)])
    assert out[0]["verdict"] == "kerb", out
    assert out[0]["speed_lost_kmh"] == 2, out


def test_a_fast_rotation_with_nobody_near_is_a_snap():
    """Over 3 g for six ticks with the heading turning 150 deg/s and the
    speed still there: the g is the car going round, not something hit.
    The hardest tick is the second one, so the measurement is taken
    around it rather than across the whole group."""
    spikes = {i: {"heading": 0.105 * (i - 49),
                  "acc_lat": 6.0 if i == 51 else 3.5}
              for i in range(50, 56)}
    out = analysis.infer_contacts(
        _LAP, _driving(spikes), [_rival(7, 998.0, 160.0)])
    assert len(out) == 1 and out[0]["verdict"] == "snap", out
    assert out[0]["yaw_rate_deg_s"] == 150, out
    assert out[0]["peak_g"] == 6.0 and out[0]["speed_lost_kmh"] == 0, out
    # The same rotation with a car alongside is still a contact.
    out = analysis.infer_contacts(
        _LAP, _driving(spikes), [_rival(7, 998.0, 103.0)])
    assert out[0]["verdict"] == "contact", out


def test_a_close_follower_is_measured_at_the_same_instant():
    """A car 15 m behind on the same line reaches the ego car's position
    0.3 s later. Matched by clock it is 15 m away throughout; matched by
    position alone it would read as a hit."""
    behind = [_rival(8, t, (t - 998.0) * 50 + 85.0)
              for t in (997.6, 997.8, 998.0, 998.2, 998.4)]
    out = analysis.infer_contacts(
        _LAP, _driving({50: {"acc_lat": 4.0}}), behind)
    assert out[0]["verdict"] == "kerb", out
    assert out[0]["nearest"]["distance_m"] == 15.0, out


def test_rows_of_one_batch_are_placed_by_their_own_clock():
    """Two rows stored together at 998.4: the newer one was taken then, the
    older one 400 ms earlier, when the ego car was 20 m further back."""
    batch = [_rival(9, 998.4, 150.0, t_ms=500400),
             _rival(9, 998.4, 101.0, t_ms=500000)]
    out = analysis.infer_contacts(
        _LAP, _driving({50: {"acc_lat": 4.0}}), batch)
    assert out[0]["verdict"] == "contact", out
    assert out[0]["nearest"]["distance_m"] == 1.0, out


def test_spikes_on_consecutive_ticks_are_one_impact():
    out = analysis.infer_contacts(
        _LAP, _driving({50: {"acc_lat": 3.2}, 52: {"acc_lat": 6.1},
                        53: {"acc_lon": -3.0}, 90: {"acc_lon": -8.0}}))
    assert [c["peak_g"] for c in out] == [6.1, 8.0], out
    assert [c["verdict"] for c in out] == ["no_opponent_data"] * 2, out
    assert [c["speed_lost_kmh"] for c in out] == [0, 0], out


def test_a_car_met_at_the_end_of_a_spin_is_found():
    """Over 3 g from tick 50 to 70, the hardest at 50, and a car 2 m away
    only at the end of it -- 0.8 s after the first spike, outside a window
    measured from the first tick alone."""
    spikes = {i: {"acc_lat": 6.0 if i == 50 else 3.5} for i in range(50, 71)}
    out = analysis.infer_contacts(
        _LAP, _driving(spikes), [_rival(7, 998.8, 142.0)])
    assert len(out) == 1 and out[0]["verdict"] == "contact", out
    assert out[0]["nearest"]["distance_m"] == 2.0, out


def test_a_spike_at_an_angle_counts_on_both_axes_together():
    """2.5 g each way is 3.5 g, under the line on either axis alone."""
    out = analysis.infer_contacts(
        _LAP, _driving({50: {"acc_lat": 2.5, "acc_lon": -2.4}}))
    assert len(out) == 1 and out[0]["peak_g"] == 3.5, out
    assert out[0]["axis"] == "lat", out


def test_with_no_opponent_data_the_physics_still_reads():
    spikes = {50: {"acc_lon": -12.0, "speed_kmh": 60.0}}
    for i in range(51, 62):
        spikes[i] = {"speed_kmh": 40.0}
    out = analysis.infer_contacts(_LAP, _driving(spikes))
    assert out[0]["verdict"] == "no_opponent_data", out
    assert out[0]["if_alone"] == "wall", out
    out = analysis.infer_contacts(_LAP, _driving({50: {"acc_lat": 4.0}}))
    assert out[0]["if_alone"] == "kerb", out
    # Placed against someone, the verdict is the reading and no if_alone.
    out = analysis.infer_contacts(
        _LAP, _driving({50: {"acc_lat": 4.0}}), [_rival(7, 998.0, 160.0)])
    assert "if_alone" not in out[0], out


def test_lap_summary_keeps_only_where_and_how_hard_for_a_kerb():
    out = analysis.compact_contacts(analysis.infer_contacts(
        _LAP, _driving({30: {"acc_lat": 4.0}, 60: {"acc_lat": 5.0}}),
        [_rival(7, 997.2, 60.0), _rival(7, 998.4, 300.0)]))
    assert out[0]["verdict"] == "contact" and "nearest" in out[0], out
    assert out[1] == {"pos": 0.6, "t_ms": 2400, "peak_g": 5.0,
                      "verdict": "kerb"}, out
    assert analysis.compact_contacts(None) is None


def test_no_spike_no_channel_and_no_clock_are_different_answers():
    assert analysis.infer_contacts(_LAP, _driving()) == []
    assert analysis.infer_contacts(_LAP, _driving(acc=False)) is None
    assert analysis.infer_contacts(
        {"lap_time_ms": 4000}, _driving({50: {"acc_lat": 5.0}})) is None


def test_the_feet_are_read_from_the_first_spike_sample():
    out = analysis.infer_contacts(
        _LAP, _driving({50: {"acc_lon": -4.0, "gas": 0.0, "brake": 0.8},
                        90: {"acc_lat": 4.0, "gas": 0.0, "brake": 0.0}}))
    assert [c["input"] for c in out] == ["brake", "coasting"], out
    assert out[0]["brake"] == 0.8 and out[0]["gas"] == 0.0, out


def test_a_glitch_past_the_sanity_ceiling_still_counts_here():
    """peak_lat_g drops a 10 g sample as a glitch; a hit reads the same and
    is the whole point of this detector."""
    out = analysis.infer_contacts(
        _LAP, _driving({50: {"acc_lon": -106.7}}), [_rival(7, 998.0, 107.0)])
    assert out[0]["verdict"] == "contact" and out[0]["peak_g"] == 106.7, out


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
