"""Fuel per lap, derived from the game rather than looked up.

Both inputs previously had to be found by hand: KM_PER_LITER by decrypting
data.acd, track length from memory. That worked once, for one car, at one
circuit, and produced numbers nobody could check. The in-game app now reads
both -- ac.INIConfig.carData() gets at fuel_cons.ini even inside an
encrypted archive, and trackLengthM comes from the AI spline.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module  # noqa: E402

from ac_race_engineer import analysis, db  # noqa: E402

# The real figures from the RSS Formula 4 at Mugello.
MUGELLO_M = 5245.0
KM_PER_L = 2.18
TANK = 48.0


def test_it_reproduces_the_mugello_numbers_worked_out_by_hand():
    out = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_litres=TANK)
    assert abs(out["litres_per_lap"] - 2.406) < 0.01, out["litres_per_lap"]
    # The hand figure is the distance alone; total_litres is what goes in
    # the car, which is the distance plus the margin.
    assert abs(out["distance_litres"] - 52.9) < 0.5, out["distance_litres"]
    assert abs(out["total_litres"] - 54.4) < 0.5, out["total_litres"]
    assert out["stop_required_for_fuel"] is True
    print(f"  {out['litres_per_lap']} L/lap, {out['distance_litres']} L for "
          f"the distance, {out['total_litres']} L with the margin, "
          f"tank {TANK} -> stop forced")


def test_the_totals_say_whether_the_margin_is_in_them():
    """Two numbers a lap apart cannot share one unqualified name.

    total_litres carried the margin in the stints and not in the total, and
    the payload said which for neither.
    """
    out = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_litres=TANK)
    per_lap = out["litres_per_lap"]
    assert out["margin_laps"] == 0.6
    assert abs(out["total_litres"]
               - (out["distance_litres"] + 0.6 * per_lap)) < 0.06
    assert "margin" in out["totals_include_margin"]
    # laps_per_tank is the number of laps that can be *finished* with the
    # margin intact; the dry figure is the one that runs the tank out.
    assert abs(out["laps_per_tank"] - (out["laps_per_tank_dry"] - 0.6)) < 0.06
    print(f"  {out['distance_litres']} L distance, {out['total_litres']} L "
          f"total, {out['laps_per_tank']} laps a tank "
          f"({out['laps_per_tank_dry']} dry)")


def test_a_forced_stop_is_called_mandatory_not_tactical():
    out = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_litres=TANK)
    assert "mandatory" in out["note"], out["note"]
    # And a short race must not be described the same way.
    short = analysis.fuel_plan(15, KM_PER_L, MUGELLO_M, tank_litres=TANK)
    assert short["stop_required_for_fuel"] is False
    assert "without stopping" in short["note"], short["note"]
    print(f"  22 laps: forced. 15 laps: {short['laps_per_tank']} per tank, "
          f"free choice")


def test_the_stints_add_up_to_the_distance():
    out = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_litres=TANK)
    assert sum(s["laps"] for s in out["stints"]) == 22, out["stints"]
    assert len(out["stints"]) == 2
    # Even split: the longest stint as short as possible, because the limit
    # is tyre life rather than fuel.
    assert abs(out["stints"][0]["laps"] - out["stints"][1]["laps"]) <= 1
    for s in out["stints"]:
        assert s["spare_at_end_litres"] >= 0, s
    print(f"  stints {[s['laps'] for s in out['stints']]}, "
          f"start {out['stints'][0]['start_with_litres']} L, "
          f"add {out['stints'][1]['add_litres']} L")


def test_an_odd_lap_count_splits_without_losing_a_lap():
    out = analysis.fuel_plan(23, KM_PER_L, MUGELLO_M, tank_litres=TANK)
    assert sum(s["laps"] for s in out["stints"]) == 23, out["stints"]


def test_two_stops_give_three_stints():
    out = analysis.fuel_plan(30, KM_PER_L, MUGELLO_M, tank_litres=TANK,
                             stops=2)
    assert len(out["stints"]) == 3
    assert sum(s["laps"] for s in out["stints"]) == 30


def test_suzuka_needs_no_new_numbers():
    """The whole point: a different circuit, nothing looked up."""
    suzuka_m = 5807.0
    out = analysis.fuel_plan(18, KM_PER_L, suzuka_m, tank_litres=TANK)
    assert abs(out["litres_per_lap"] - 2.664) < 0.01, out["litres_per_lap"]
    print(f"  Suzuka {out['litres_per_lap']} L/lap, "
          f"{out['laps_per_tank']} laps a tank")


def test_a_distance_needing_three_stops_is_not_planned_with_one():
    """The plan that cannot be driven, offered as a plan.

    60 laps at Mugello is three stops. Asked for one, this returned two
    stints of 30, a note calling "the stop" mandatory in the singular, and
    a spare of -24.2 L at the end of both -- the deficit hidden by a clamp
    to the tank, and the second stint costed as though the first had
    finished normally.
    """
    out = analysis.fuel_plan(60, KM_PER_L, MUGELLO_M, tank_litres=TANK,
                             stops=1)
    assert out["minimum_stops"] == 3, out["minimum_stops"]
    assert out["stops_requested"] == 1 and out["stops_planned"] == 3, out
    assert len(out["stints"]) == 4, out["stints"]
    assert sum(s["laps"] for s in out["stints"]) == 60
    for s in out["stints"]:
        assert s["spare_at_end_litres"] >= 0, s
        assert "cannot_be_fuelled" not in s, s
        assert s.get("start_with_litres", s.get("add_litres")) <= TANK, s
    assert "3 stops are mandatory" in out["note"], out["note"]
    print(f"  60 laps -> {out['stops_planned']} stops, stints "
          f"{[s['laps'] for s in out['stints']]}, spare "
          f"{[s['spare_at_end_litres'] for s in out['stints']]}")


def test_a_deficit_is_never_hidden_by_a_clamp():
    """40 laps was the subtle version: -0.1 L, in a field called spare.

    Nothing in this function may clamp a stint's fuel to the tank. The
    check is on the arithmetic rather than on the verdict: every stint must
    carry what it burns, and what it carries must fit.
    """
    per_lap = (MUGELLO_M / 1000.0) / KM_PER_L
    for laps in range(2, 80):
        out = analysis.fuel_plan(laps, KM_PER_L, MUGELLO_M, tank_litres=TANK,
                                 stops=1)
        carried = 0.0
        for s in out["stints"]:
            carried += s.get("start_with_litres", s.get("add_litres"))
            assert carried <= TANK + 1e-6, (laps, s)
            carried -= per_lap * s["laps"]
            assert carried >= 0, (laps, s)
            # Every litre is reported to 0.1, so the reconstruction can only
            # ever agree to a couple of roundings.
            assert abs(carried - s["spare_at_end_litres"]) < 0.15, (laps, s)
    print("  2..79 laps: every stint fits the tank and reaches the flag")


def test_the_no_stop_verdict_carries_the_same_margin_as_the_stints():
    """It flipped at exactly total == tank, and the stints ask for more.

    19 laps has a lap in hand and is a no-stopper. 20 laps arrives dry, and
    used to be a no-stopper too until the last 0.1 L of it.
    """
    for laps, forced in ((18, False), (19, False), (20, True)):
        out = analysis.fuel_plan(laps, KM_PER_L, MUGELLO_M, tank_litres=TANK)
        assert out["stop_required_for_fuel"] is forced, (laps, out["note"])
        spare = TANK - out["distance_litres"]
        print(f"  {laps} laps: {out['distance_litres']} L of {TANK}, "
              f"spare {spare:+.2f} L ({spare / out['litres_per_lap']:+.2f} "
              f"lap) -> forced={forced}")
        if not forced:
            # The verdict and the stint arithmetic have to agree: a no-stop
            # race must reach the flag with the margin still in it.
            assert spare >= 0.6 * out["litres_per_lap"] - 1e-6, laps
    # A tank where the two rules disagree by a whole lap. 20 laps fits
    # 49.1 L to the last drop and does not fit it with two thirds of a lap
    # in hand, so this is the case that separates "reaches the flag" from
    # "reaches the flag dry" -- and at Mugello's 48 L no integer lap count
    # falls in that gap, which is why the old rule looked right there.
    edge = analysis.fuel_plan(20, KM_PER_L, MUGELLO_M, tank_litres=49.1)
    assert edge["distance_litres"] <= 49.1, edge["distance_litres"]
    assert edge["stop_required_for_fuel"] is True, edge["note"]
    assert edge["minimum_stops"] == 1
    print(f"  20 laps on a 49.1 L tank: {edge['distance_litres']} L fits dry, "
          f"{edge['total_litres']} L with the margin does not -> stop")

    # And the two answers are one calculation, not two that happen to agree.
    for laps in range(2, 60):
        out = analysis.fuel_plan(laps, KM_PER_L, MUGELLO_M, tank_litres=TANK)
        assert out["stop_required_for_fuel"] is (out["minimum_stops"] > 0), out


def test_the_fuel_rate_multiplier_is_read_rather_than_assumed():
    """A league at 50% or 200% is exactly when a stop is or is not needed."""
    base = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_litres=TANK)
    assert base["fuel_rate_pct"] is None
    assert "100%" in base["fuel_rate_unknown"], base["fuel_rate_unknown"]

    half = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_litres=TANK,
                              fuel_rate=0.5)
    twice = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_litres=TANK,
                               fuel_rate=2.0)
    assert half["fuel_rate_pct"] == 50.0 and twice["fuel_rate_pct"] == 200.0
    assert abs(half["litres_per_lap"] * 2 - base["litres_per_lap"]) < 0.01
    assert abs(twice["litres_per_lap"] / 2 - base["litres_per_lap"]) < 0.01
    # And the thing that actually matters: the verdict moves with it.
    assert half["stop_required_for_fuel"] is False
    assert base["stop_required_for_fuel"] is True
    assert twice["minimum_stops"] > base["minimum_stops"]
    print(f"  22 laps: 50% -> no stop, 100% -> {base['minimum_stops']} stop, "
          f"200% -> {twice['minimum_stops']} stops")


def test_a_zero_percent_session_burns_nothing_and_says_so():
    """0 is a real setting, and the one that divides by zero."""
    out = analysis.fuel_plan(60, KM_PER_L, MUGELLO_M, tank_litres=TANK,
                             fuel_rate=0.0)
    assert out["litres_per_lap"] == 0
    assert out["stop_required_for_fuel"] is False
    assert "0%" in out["note"], out["note"]


def test_an_unknown_tank_says_so_rather_than_dropping_the_verdict():
    """Three keys vanishing left a two-stint plan implying a stop.

    A read-only or missing FUEL range is enough to do it, and the payload
    that came back was indistinguishable from one where stopping had been
    thought about and found unnecessary.
    """
    out = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, stops=1)
    assert "tank_litres" in out and out["tank_litres"] is None, out
    assert out["stop_required_for_fuel"] is None, out
    assert "unknown" in out["note"] and "not been checked" in out["note"]
    assert len(out["stints"]) == 2
    print(" ", out["note"])


def test_the_inputs_are_checked_rather_than_coerced():
    """None of this is reachable through the app; all of it is by a caller.

    km_per_liter=-2.18 came back as litres_per_lap -2.406, a fractional
    stop count raised a TypeError out of range(), and nine stops over three
    laps produced six stints of zero laps.
    """
    ok = dict(race_laps=10, km_per_liter=KM_PER_L, track_length_m=MUGELLO_M,
              tank_litres=TANK)
    for bad, expect in (
            ({"km_per_liter": -KM_PER_L}, "km_per_liter"),
            ({"track_length_m": -MUGELLO_M}, "track_length_m"),
            ({"tank_litres": -1}, "tank_litres"),
            ({"stops": -3}, "stops"),
            ({"stops": 1.7}, "whole number"),
            ({"stops": "1"}, "must be a number"),
            ({"race_laps": 0}, "race_laps"),
            ({"race_laps": None}, "must be a number"),
            ({"race_laps": 3, "stops": 9}, "stint of no laps"),
            ({"fuel_rate": -1}, "fuel_rate"),
            ({"fuel_rate": 50}, "fuel_rate"),
            ({"margin_laps": -1}, "margin_laps")):
        out = analysis.fuel_plan(**{**ok, **bad})
        assert "error" in out, (bad, out)
        assert expect in out["error"], (bad, out["error"])
        assert "stints" not in out, (bad, out)
    print("  twelve bad inputs, twelve refusals naming the argument")


def test_a_tank_too_small_for_one_lap_refuses_to_plan():
    out = analysis.fuel_plan(10, KM_PER_L, MUGELLO_M, tank_litres=3.0)
    assert "stints" not in out, out
    assert "does not cover one lap" in out["error"], out["error"]


def test_the_tank_is_found_even_when_the_game_locks_the_fuel_entry():
    """read_only is right for writing a setup and wrong for tank size.

    Flipping that one flag dropped tank_litres, stop_required_for_fuel and
    the note out of the payload while a two-stint plan stayed behind.
    """
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn, car="carx")
        db.store_setup_snapshot(conn, sid, "carx", spinners=[
            {"name": "FUEL", "min": 1.0, "max": 48.0, "step": 1,
             "read_only": True}])
        assert "FUEL" not in db.setup_ranges(conn, "carx")
        tank, source = db.tank_litres(conn, "carx")
        assert tank == 48.0, tank
        assert "read-only" in source, source

        # And a car nobody has posted a setup for is unknown, not zero.
        assert db.tank_litres(conn, "other") == (None, "not known")
        print(f"  locked FUEL entry still gives {tank} L, from {source}")
        conn.close()


def test_set_fuel_basis_checks_its_own_arguments():
    """The range checks lived in the bridge, so they only guarded HTTP.

    Called directly with a numeric string this raised `'>' not supported
    between instances of 'str' and 'int'`, naming neither the argument nor
    the mistake.
    """
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        assert db.set_fuel_basis(conn, sid, track_length_m="5245")
        assert db.get_session(conn, sid)["track_length_m"] == MUGELLO_M

        number = "must be a number"
        for kwargs, expect in (({"track_length_m": "long"}, number),
                               ({"km_per_liter": object()}, number),
                               ({"km_per_liter": True}, number),
                               ({"track_length_m": 1e9}, "must be between"),
                               ({"fuel_rate": 50}, "must be between")):
            try:
                db.set_fuel_basis(conn, sid, **kwargs)
            except ValueError as e:
                assert expect in str(e), (kwargs, e)
                assert list(kwargs)[0] in str(e), (kwargs, e)
            else:
                raise AssertionError(f"{kwargs} was accepted")

        # 0 is absent for a length and present for the multiplier: a session
        # that burns no fuel is a setting, not a missing value.
        assert db.set_fuel_basis(conn, sid, track_length_m=0) is False
        assert db.set_fuel_basis(conn, sid, fuel_rate=0) is True
        assert db.get_session(conn, sid)["fuel_rate"] == 0
        assert db.get_session(conn, sid)["track_length_m"] == MUGELLO_M
        print("  numeric strings accepted, junk named, a rate of 0 stored")
        conn.close()


# Importing the server opens a database and binds the bridge, so it runs in
# a subprocess against a throwaway data directory and an ephemeral port
# rather than in the suite's own interpreter. Nothing else imports it, which
# is why the tool layer -- where the tank lookup and the argument checks
# live, and where a read-only FUEL entry silently emptied the payload -- had
# no coverage at all.
_SERVER_PROBE = """
import json
from ac_race_engineer import db, server

conn = server._conn
sid = db.create_session(conn, car="rss_formula_rss_4", track="mugello",
                        track_config="mugello_osrw", tyre_compound="S",
                        air_temp=25.0, road_temp=30.0)
db.set_fuel_basis(conn, sid, track_length_m=5245.0, km_per_liter=2.18)
db.store_setup_snapshot(conn, sid, "rss_formula_rss_4", spinners=[
    {"name": "FUEL", "min": 1.0, "max": 48.0, "step": 1, "read_only": True}])

tool = getattr(server.fuel_plan, "fn", server.fuel_plan)
print(json.dumps({
    "locked_tank": json.loads(tool(race_laps=60, stops=1, session_id=sid)),
    "negative_stops": json.loads(tool(race_laps=10, stops=-1,
                                      session_id=sid)),
    "fractional_stops": json.loads(tool(race_laps=10, stops=1.5,
                                        session_id=sid)),
}))
"""


def test_the_tool_layer_survives_a_read_only_fuel_entry():
    """One flag emptied the payload while leaving a two-stint plan behind.

    tank_litres, stop_required_for_fuel and the note all came from a ranges
    lookup that excludes read-only entries, so a car whose fuel load the
    game will not let you change produced a plan that never mentioned
    stopping -- next to stints that only exist because of a stop.
    """
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ)
        env["AC_ENGINEER_DATA"] = d
        env["AC_ENGINEER_BRIDGE_PORT"] = "0"  # never the real one
        env["PYTHONPATH"] = os.pathsep.join(
            [str(Path(__file__).resolve().parent.parent),
             env.get("PYTHONPATH", "")])
        proc = subprocess.run([sys.executable, "-c", _SERVER_PROBE],
                              env=env, capture_output=True, text=True,
                              timeout=120)
        assert proc.returncode == 0, proc.stderr
        got = json.loads(proc.stdout)

        plan = got["locked_tank"]
        assert plan["tank_litres"] == 48.0, plan
        assert "read-only" in plan["tank_litres_source"], plan
        assert plan["stop_required_for_fuel"] is True
        assert plan["stops_planned"] == 3, plan
        # The multiplier cannot be read off Windows, and that is reported
        # rather than assumed away.
        assert plan["fuel_rate_pct"] is None
        assert "fuel_rate_unknown" in plan

        # And the tool's own arguments, which arrive from an MCP client and
        # were taken entirely on trust.
        assert "stops" in got["negative_stops"]["error"]
        assert "whole number" in got["fractional_stops"]["error"]
        print(f"  locked FUEL entry -> {plan['tank_litres']} L, "
              f"{plan['stops_planned']} stops; bad arguments refused")


def test_a_missing_basis_refuses_rather_than_assuming():
    for kml, length in ((None, MUGELLO_M), (KM_PER_L, None), (0, 0)):
        out = analysis.fuel_plan(22, kml, length)
        assert "error" in out, (kml, length, out)
    print("  no basis -> refuses, never a plausible default")


def test_the_fuel_basis_survives_a_partial_update():
    """Track length may arrive without the car's consumption figure."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)

        assert db.set_fuel_basis(conn, sid, track_length_m=MUGELLO_M)
        assert db.set_fuel_basis(conn, sid, km_per_liter=KM_PER_L)
        s = db.get_session(conn, sid)
        assert s["track_length_m"] == MUGELLO_M
        assert s["km_per_liter"] == KM_PER_L

        # A later post with neither value must not wipe what is known.
        assert db.set_fuel_basis(conn, sid) is False
        s = db.get_session(conn, sid)
        assert s["track_length_m"] == MUGELLO_M, "absence erased a value"
        assert s["km_per_liter"] == KM_PER_L, "absence erased a value"
        print("  partial updates merge; an empty one changes nothing")
        conn.close()


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
