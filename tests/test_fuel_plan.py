"""Fuel per lap, derived from the game rather than looked up.

The in-game app reads both inputs: ac.INIConfig.carData() gets at
fuel_cons.ini even inside an encrypted data.acd, and trackLengthM comes from
the AI spline.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import make_session, run_module  # noqa: E402

from assetto_mcp import analysis, db  # noqa: E402

# The real figures from the RSS Formula 4 at Mugello.
MUGELLO_M = 5245.0
KM_PER_L = 2.18
TANK = 48.0


def test_it_matches_the_mugello_numbers_worked_out_by_hand():
    out = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_liters=TANK)
    assert abs(out["liters_per_lap"] - 2.406) < 0.01, out["liters_per_lap"]
    # The hand figure is the distance alone; total_liters is what goes in
    # the car, which is the distance plus the margin.
    assert abs(out["distance_liters"] - 52.9) < 0.5, out["distance_liters"]
    assert abs(out["total_liters"] - 54.4) < 0.5, out["total_liters"]
    assert out["stop_required_for_fuel"] is True
    print(f"  {out['liters_per_lap']} L/lap, {out['distance_liters']} L for "
          f"the distance, {out['total_liters']} L with the margin, "
          f"tank {TANK} -> stop forced")


def test_the_totals_say_whether_the_margin_is_in_them():
    """Two numbers a lap apart cannot share one unqualified name."""
    out = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_liters=TANK)
    per_lap = out["liters_per_lap"]
    assert out["margin_laps"] == 0.6
    assert abs(out["total_liters"]
               - (out["distance_liters"] + 0.6 * per_lap)) < 0.06
    assert "margin" in out["totals_include_margin"]
    # laps_per_tank is the number of laps that can be *finished* with the
    # margin intact; the dry figure is the one that runs the tank out.
    assert abs(out["laps_per_tank"] - (out["laps_per_tank_dry"] - 0.6)) < 0.06
    print(f"  {out['distance_liters']} L distance, {out['total_liters']} L "
          f"total, {out['laps_per_tank']} laps a tank "
          f"({out['laps_per_tank_dry']} dry)")


def test_a_forced_stop_is_called_mandatory_not_tactical():
    out = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_liters=TANK)
    assert "mandatory" in out["note"], out["note"]
    # And a short race must not be described the same way.
    short = analysis.fuel_plan(15, KM_PER_L, MUGELLO_M, tank_liters=TANK)
    assert short["stop_required_for_fuel"] is False
    assert "without stopping" in short["note"], short["note"]
    print(f"  22 laps: forced. 15 laps: {short['laps_per_tank']} per tank, "
          f"free choice")


def test_the_stints_add_up_to_the_distance():
    out = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_liters=TANK)
    assert sum(s["laps"] for s in out["stints"]) == 22, out["stints"]
    assert len(out["stints"]) == 2
    # Even split: the longest stint as short as possible, because the limit
    # is tyre life rather than fuel.
    assert abs(out["stints"][0]["laps"] - out["stints"][1]["laps"]) <= 1
    for s in out["stints"]:
        assert s["spare_at_end_liters"] >= 0, s
    print(f"  stints {[s['laps'] for s in out['stints']]}, "
          f"start {out['stints'][0]['start_with_liters']} L, "
          f"add {out['stints'][1]['add_liters']} L")


def test_an_odd_lap_count_splits_without_losing_a_lap():
    out = analysis.fuel_plan(23, KM_PER_L, MUGELLO_M, tank_liters=TANK)
    assert sum(s["laps"] for s in out["stints"]) == 23, out["stints"]


def test_two_stops_give_three_stints():
    out = analysis.fuel_plan(30, KM_PER_L, MUGELLO_M, tank_liters=TANK,
                             stops=2)
    assert len(out["stints"]) == 3
    assert sum(s["laps"] for s in out["stints"]) == 30


def test_suzuka_needs_no_new_numbers():
    """A different circuit needs only its length, nothing looked up."""
    suzuka_m = 5807.0
    out = analysis.fuel_plan(18, KM_PER_L, suzuka_m, tank_liters=TANK)
    assert abs(out["liters_per_lap"] - 2.664) < 0.01, out["liters_per_lap"]
    print(f"  Suzuka {out['liters_per_lap']} L/lap, "
          f"{out['laps_per_tank']} laps a tank")


def test_a_distance_needing_three_stops_is_not_planned_with_one():
    """60 laps at Mugello is three stops, whatever was asked for.

    Two stints of 30 cannot be driven: each would end 24 L short.
    """
    out = analysis.fuel_plan(60, KM_PER_L, MUGELLO_M, tank_liters=TANK,
                             stops=1)
    assert out["minimum_stops"] == 3, out["minimum_stops"]
    assert out["stops_requested"] == 1 and out["stops_planned"] == 3, out
    assert len(out["stints"]) == 4, out["stints"]
    assert sum(s["laps"] for s in out["stints"]) == 60
    for s in out["stints"]:
        assert s["spare_at_end_liters"] >= 0, s
        assert "cannot_be_fuelled" not in s, s
        assert s.get("start_with_liters", s.get("add_liters")) <= TANK, s
    assert "3 stops are mandatory" in out["note"], out["note"]
    print(f"  60 laps -> {out['stops_planned']} stops, stints "
          f"{[s['laps'] for s in out['stints']]}, spare "
          f"{[s['spare_at_end_liters'] for s in out['stints']]}")


def test_a_deficit_is_never_hidden_by_a_clamp():
    """Nothing may clamp a stint's fuel to the tank.

    The check is on the arithmetic rather than on the verdict: every stint
    must carry what it burns, and what it carries must fit. At 40 laps a
    clamp would hide a deficit of 0.1 L.
    """
    per_lap = (MUGELLO_M / 1000.0) / KM_PER_L
    for laps in range(2, 80):
        out = analysis.fuel_plan(laps, KM_PER_L, MUGELLO_M, tank_liters=TANK,
                                 stops=1)
        carried = 0.0
        for s in out["stints"]:
            carried += s.get("start_with_liters", s.get("add_liters"))
            assert carried <= TANK + 1e-6, (laps, s)
            carried -= per_lap * s["laps"]
            assert carried >= 0, (laps, s)
            # Every liter is reported to 0.1, so the reconstruction can only
            # ever agree to a couple of roundings.
            assert abs(carried - s["spare_at_end_liters"]) < 0.15, (laps, s)
    print("  2..79 laps: every stint fits the tank and reaches the flag")


def test_the_no_stop_verdict_carries_the_same_margin_as_the_stints():
    """The verdict must not flip at total == tank; the stints ask for more.

    19 laps has a lap in hand and is a no-stopper. 20 laps arrives dry, so
    it needs a stop.
    """
    for laps, forced in ((18, False), (19, False), (20, True)):
        out = analysis.fuel_plan(laps, KM_PER_L, MUGELLO_M, tank_liters=TANK)
        assert out["stop_required_for_fuel"] is forced, (laps, out["note"])
        spare = TANK - out["distance_liters"]
        print(f"  {laps} laps: {out['distance_liters']} L of {TANK}, "
              f"spare {spare:+.2f} L ({spare / out['liters_per_lap']:+.2f} "
              f"lap) -> forced={forced}")
        if not forced:
            # The verdict and the stint arithmetic have to agree: a no-stop
            # race must reach the flag with the margin still in it.
            assert spare >= 0.6 * out["liters_per_lap"] - 1e-6, laps
    # A tank that separates "reaches the flag" from "reaches the flag dry":
    # 20 laps fits 49.1 L to the last drop but not with the margin in hand.
    # At Mugello's 48 L no integer lap count falls in that gap.
    edge = analysis.fuel_plan(20, KM_PER_L, MUGELLO_M, tank_liters=49.1)
    assert edge["distance_liters"] <= 49.1, edge["distance_liters"]
    assert edge["stop_required_for_fuel"] is True, edge["note"]
    assert edge["minimum_stops"] == 1
    print(f"  20 laps on a 49.1 L tank: {edge['distance_liters']} L fits dry, "
          f"{edge['total_liters']} L with the margin does not -> stop")

    # And the two answers are one calculation, not two that happen to agree.
    for laps in range(2, 60):
        out = analysis.fuel_plan(laps, KM_PER_L, MUGELLO_M, tank_liters=TANK)
        assert out["stop_required_for_fuel"] is (out["minimum_stops"] > 0), out


def test_the_fuel_rate_multiplier_is_read_rather_than_assumed():
    """A league at 50% or 200% is exactly when a stop is or is not needed."""
    base = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_liters=TANK)
    assert base["fuel_rate_pct"] is None
    assert "100%" in base["fuel_rate_unknown"], base["fuel_rate_unknown"]

    half = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_liters=TANK,
                              fuel_rate=0.5)
    twice = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, tank_liters=TANK,
                               fuel_rate=2.0)
    assert half["fuel_rate_pct"] == 50.0 and twice["fuel_rate_pct"] == 200.0
    assert abs(half["liters_per_lap"] * 2 - base["liters_per_lap"]) < 0.01
    assert abs(twice["liters_per_lap"] / 2 - base["liters_per_lap"]) < 0.01
    # And the thing that actually matters: the verdict moves with it.
    assert half["stop_required_for_fuel"] is False
    assert base["stop_required_for_fuel"] is True
    assert twice["minimum_stops"] > base["minimum_stops"]
    print(f"  22 laps: 50% -> no stop, 100% -> {base['minimum_stops']} stop, "
          f"200% -> {twice['minimum_stops']} stops")


def test_a_zero_percent_session_burns_nothing_and_says_so():
    """0 is a real setting, and the one that divides by zero."""
    out = analysis.fuel_plan(60, KM_PER_L, MUGELLO_M, tank_liters=TANK,
                             fuel_rate=0.0)
    assert out["liters_per_lap"] == 0
    assert out["stop_required_for_fuel"] is False
    assert "0%" in out["note"], out["note"]


def test_an_unknown_tank_says_so_rather_than_dropping_the_verdict():
    """Without a tank size the verdict is unknown, and says so.

    A read-only or missing FUEL range is enough to lose it. Dropping the
    keys would leave a two-stint plan indistinguishable from one where
    stopping had been checked and found unnecessary.
    """
    out = analysis.fuel_plan(22, KM_PER_L, MUGELLO_M, stops=1)
    assert "tank_liters" in out and out["tank_liters"] is None, out
    assert out["stop_required_for_fuel"] is None, out
    assert "unknown" in out["note"] and "not been checked" in out["note"]
    assert len(out["stints"]) == 2
    print(" ", out["note"])


def test_the_inputs_are_checked_rather_than_coerced():
    """None of this is reachable through the app; all of it is by a caller.

    Unchecked, a negative km_per_liter gives a negative liters_per_lap, a
    fractional stop count raises TypeError out of range(), and nine stops
    over three laps give six stints of zero laps.
    """
    ok = dict(race_laps=10, km_per_liter=KM_PER_L, track_length_m=MUGELLO_M,
              tank_liters=TANK)
    for bad, expect in (
            ({"km_per_liter": -KM_PER_L}, "km_per_liter"),
            ({"track_length_m": -MUGELLO_M}, "track_length_m"),
            ({"tank_liters": -1}, "tank_liters"),
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
    out = analysis.fuel_plan(10, KM_PER_L, MUGELLO_M, tank_liters=3.0)
    assert "stints" not in out, out
    assert "does not cover one lap" in out["error"], out["error"]


def test_the_tank_is_found_even_when_the_game_locks_the_fuel_entry():
    """read_only matters for writing a setup, not for tank size."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn, car="carx")
        db.store_setup_snapshot(conn, sid, "carx", spinners=[
            {"name": "FUEL", "min": 1.0, "max": 48.0, "step": 1,
             "read_only": True}])
        assert "FUEL" not in db.setup_ranges(conn, "carx")
        tank, source = db.tank_liters(conn, "carx")
        assert tank == 48.0, tank
        assert "read-only" in source, source

        # And a car nobody has posted a setup for is unknown, not zero.
        assert db.tank_liters(conn, "other") == (None, "not known")
        print(f"  locked FUEL entry still gives {tank} L, from {source}")
        conn.close()


def test_set_fuel_basis_checks_its_own_arguments():
    """Checks in the bridge alone would only guard HTTP.

    A direct call with junk must get an error naming the argument, not a
    TypeError from a comparison.
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
# rather than in the suite's own interpreter. The tank lookup and the
# argument checks live in the tool layer.
_SERVER_PROBE = """
import json
from assetto_mcp import db, server

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


def _run_probe(source: str) -> dict:
    """Run a probe against a throwaway data directory and return its JSON."""
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ)
        env["ASSETTO_MCP_DATA"] = d
        env["ASSETTO_MCP_BRIDGE_PORT"] = "0"  # never the real one
        env["PYTHONPATH"] = os.pathsep.join(
            [str(Path(__file__).resolve().parent.parent),
             env.get("PYTHONPATH", "")])
        proc = subprocess.run([sys.executable, "-c", source],
                              env=env, capture_output=True, text=True,
                              timeout=120)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)


def test_the_tool_layer_survives_a_read_only_fuel_entry():
    """A car whose fuel load the game locks still gets a stop verdict.

    The ranges lookup excludes read-only entries, so the tank has to come
    from elsewhere, or the plan's stints would imply a stop it never
    mentions.
    """
    got = _run_probe(_SERVER_PROBE)

    plan = got["locked_tank"]
    assert plan["tank_liters"] == 48.0, plan
    assert "read-only" in plan["tank_liters_source"], plan
    assert plan["stop_required_for_fuel"] is True
    assert plan["stops_planned"] == 3, plan
    # The multiplier cannot be read off Windows, and that is reported
    # rather than assumed away.
    assert plan["fuel_rate_pct"] is None
    assert "fuel_rate_unknown" in plan

    # And the tool's own arguments, which arrive from an MCP client.
    assert "stops" in got["negative_stops"]["error"]
    assert "whole number" in got["fractional_stops"]["error"]
    print(f"  locked FUEL entry -> {plan['tank_liters']} L, "
          f"{plan['stops_planned']} stops; bad arguments refused")


# The tank is resolved from two unrelated places -- the car's FUEL setup
# range, and the static page's maxFuel -- and only the second is what
# sessions.max_fuel_liters means. This walks both twice, because the second
# call is the one that would read a range-derived tank back out of the
# session and label it "the game's maxFuel".
_PROVENANCE_PROBE = """
import json
from assetto_mcp import db, server

conn = server._conn
facts = {}
# Stands in for shared memory: empty is AC shut, which is every machine this
# suite runs on anyway.
server._live_fuel_facts = lambda: (
    dict(facts), None if facts else "AC is not running")


def new_session(car):
    return db.create_session(conn, car=car, track="mugello", track_config="",
                             tyre_compound="S", air_temp=25.0, road_temp=30.0)


def basis(sid):
    values, source = server._fuel_basis(sid, db.get_session(conn, sid))
    return {"tank": values["tank"], "source": source["tank_source"],
            "stored": db.get_session(conn, sid)["max_fuel_liters"]}


out = {}

# A tank that came from the car's FUEL setup range, twice.
ranged = new_session("range_car")
db.store_setup_snapshot(conn, ranged, "range_car", spinners=[
    {"name": "FUEL", "min": 1.0, "max": 48.0, "step": 1, "read_only": True}])
out["range_first"] = basis(ranged)
out["range_second"] = basis(ranged)
# And with the ranges gone.
conn.execute("DELETE FROM setup_ranges WHERE car = 'range_car'")
conn.commit()
out["range_forgotten"] = basis(ranged)

# A tank that genuinely came off the static page, twice, with AC closing in
# between so the second call can only be answered from the session row.
facts.update({"max_fuel_liters": 55.0, "fuel_rate": 1.0})
live = new_session("live_car")
out["live_first"] = basis(live)
facts.clear()
out["live_second"] = basis(live)

print(json.dumps(out))
"""


def test_only_a_tank_from_maxfuel_is_stored_as_maxfuel():
    """The session column holds what the static page said, or nothing.

    db.tank_liters answers from the car's FUEL setup range, a property of
    the car; the AC reader's session() answers from static.maxFuel, a property of
    the session. Only the second belongs in sessions.max_fuel_liters, or a
    setup-range number reads back as "the game's maxFuel".
    """
    got = _run_probe(_PROVENANCE_PROBE)

    for call in ("range_first", "range_second"):
        r = got[call]
        assert r["tank"] == 48.0, (call, r)
        assert "setup range" in r["source"], (call, r)
        assert "maxFuel" not in r["source"], (call, r)
        assert r["stored"] is None, (
            f"{call}: a setup-range tank reached sessions.max_fuel_liters")

    # Nothing was cached, so nothing is invented once the ranges are gone.
    # Unknown is the honest answer, and the ranges are what re-derives it.
    gone = got["range_forgotten"]
    assert gone["tank"] is None, gone
    assert gone["source"] == "not known", gone

    for call in ("live_first", "live_second"):
        r = got[call]
        assert r["tank"] == 55.0, (call, r)
        assert r["source"] == "the game's maxFuel", (call, r)
        assert r["stored"] == 55.0, (
            f"{call}: a real maxFuel reading was not remembered")

    print("  setup-range tank stays out of the column and keeps its name; "
          "maxFuel is stored and still reads as maxFuel")


def test_a_basis_the_plan_would_refuse_is_refused_at_the_write():
    """An absurd value is refused where it is supplied.

    Stored, it would only fail when somebody asked for a plan, possibly a
    session later and nowhere near whoever supplied it.
    """
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        for field, plausible, absurd in (
                ("track_length_m", MUGELLO_M, 0.5),
                ("km_per_liter", KM_PER_L, 0.0005),
                ("max_fuel_liters", TANK, 0.001)):
            assert db.set_fuel_basis(conn, sid, **{field: plausible}) is True
            assert db.get_session(conn, sid)[field] == plausible

            try:
                db.set_fuel_basis(conn, sid, **{field: absurd})
            except ValueError as e:
                assert field in str(e), (field, e)
                assert "must be between" in str(e), (field, e)
            else:
                raise AssertionError(f"{field}={absurd} was stored")
            assert db.get_session(conn, sid)[field] == plausible, (
                f"{field} was overwritten by a value that was refused")

            # 0 still means "not supplied", which is deliberate: the in-game
            # app posts a field it has no value for as 0.
            assert db.set_fuel_basis(conn, sid, **{field: 0}) is False
            assert db.get_session(conn, sid)[field] == plausible, (
                f"an absent {field} erased a known one")
        print("  three floors, three refusals naming the field, 0 still "
              "meaning absent")
        conn.close()


def test_the_write_floors_and_the_plan_agree():
    """No band where the database stores a basis fuel_plan will not use.

    The two checks are the same constants, and this is what says so: just
    below each floor both refuse and name the field, and at the floor both
    accept it. A floor only in one of them puts the refusal a session away
    from the caller who caused it.
    """
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        ok = dict(race_laps=10, km_per_liter=KM_PER_L,
                  track_length_m=MUGELLO_M, tank_liters=TANK)
        for field, arg, floor in (
                ("track_length_m", "track_length_m",
                 analysis.MIN_TRACK_LENGTH_M),
                ("km_per_liter", "km_per_liter", analysis.MIN_KM_PER_LITER),
                ("max_fuel_liters", "tank_liters", analysis.MIN_TANK_LITERS)):
            below = floor * 0.99
            try:
                db.set_fuel_basis(conn, sid, **{field: below})
            except ValueError as e:
                assert field in str(e), (field, e)
            else:
                raise AssertionError(f"{field}={below} was stored")
            refused = analysis.fuel_plan(**{**ok, arg: below})
            assert arg in refused.get("error", ""), (field, refused)

            assert db.set_fuel_basis(conn, sid, **{field: floor}) is True
            accepted = analysis.fuel_plan(**{**ok, arg: floor})
            assert arg not in accepted.get("error", ""), (field, accepted)
        print(f"  floors agree: {analysis.MIN_TRACK_LENGTH_M:g} m, "
              f"{analysis.MIN_KM_PER_LITER:g} km/L, "
              f"{analysis.MIN_TANK_LITERS:g} L")
        conn.close()


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


_LIVE_FUEL_PROBE = """
import json
from assetto_mcp import db, server

conn = server._conn
server._live_fuel_facts = lambda: ({}, "AC is not running")
sid = db.create_session(conn, car="nsx", track="mugello", track_config="",
                        tyre_compound="S", air_temp=25.0, road_temp=30.0)
conn.execute("UPDATE sessions SET km_per_liter = 2.5, track_length_m = 5000,"
             " fuel_rate = 1.0 WHERE id = ?", (sid,))
conn.commit()
server._active_session = lambda explicit=None: sid
snap = {"sim": "ac", "car": "nsx", "track": "mugello", "track_config": "",
        "fuel_l": 9.0, "laps_remaining": 3, "lap_done": 0.5}
print(json.dumps({
    "race": server._fuel_to_finish(snap),
    "other_car": server._fuel_to_finish({**snap, "car": "f4"}),
    "other_track": server._fuel_to_finish({**snap, "track": "monza"}),
    "other_game": server._fuel_to_finish({**snap, "sim": "ams2"}),
    "timed": server._fuel_to_finish({**snap, "laps_remaining": None}),
}))
"""


def test_live_snapshot_fuel_uses_the_driven_sessions_consumption():
    got = _run_probe(_LIVE_FUEL_PROBE)
    race = got["race"]
    assert race["liters_per_lap"] == 2.0, race
    assert race["laps_to_go"] == 2.5 and race["margin_laps"] == 2.0, race
    assert got["other_car"] is None and got["other_track"] is None, got
    # The same names in another game are another car and track.
    assert got["other_game"] is None, got
    assert got["timed"] is None, got
    print(f"  {race['verdict']}, {race['margin_laps']} laps spare")


_NO_BASIS_PROBE = """
import json
from assetto_mcp import db, server

conn = server._conn
server._live_fuel_facts = lambda: ({}, "AC is not running")
sid = db.create_session(conn, car="bare", track="mugello", track_config="",
                        tyre_compound="S", air_temp=25.0, road_temp=30.0)
print(server.fuel_plan(20, session_id=sid))
"""


def test_a_plan_with_no_burn_and_no_tank_gives_both_fixes():
    out = _run_probe(_NO_BASIS_PROBE)
    assert "liters_per_lap" in out["do_this"], out
    assert "Tank capacity" in out["do_this"], out


def test_fuel_to_finish_counts_only_the_part_of_this_lap_still_to_drive():
    cases = [
        # fuel, laps remaining, lap done, l/lap -> laps to go, margin, verdict
        (10.0, 3, 0.5, 4.0, 2.5, 0.0, "tight"),
        (9.0, 3, 0.5, 4.0, 2.5, -0.25, "short by"),
        (20.0, 1, 0.9, 4.0, 0.1, 4.9, "enough"),
        (5.0, 0, 0.3, 4.0, 0.0, 1.25, "enough"),
    ]
    for fuel, left, done, rate, to_go, margin, verdict in cases:
        out = analysis.fuel_to_finish(fuel, left, done, rate)
        assert out["laps_to_go"] == to_go and out["margin_laps"] == margin, \
            (fuel, left, done, out)
        assert out["verdict"].startswith(verdict), out
    assert "off" in analysis.fuel_to_finish(10.0, 3, 0.5, 0.0)["verdict"]
    for bad in ((None, 3, 0.5, 4.0), (10.0, 3, 0.5, -1.0),
                (10.0, None, 0.5, 4.0)):
        assert analysis.fuel_to_finish(*bad) is None, bad
    print("  margins from the part of the lap still to drive")


def test_each_lap_burn_is_measured_from_line_to_line():
    """The first lap starts wherever recording did, so it has no burn."""
    from support import FakeSim, complete_lap, run_collector, temp_db, tick

    def lap(fuel_at_the_line, ms):
        def step(sim, col):
            tick(sim, col)
            sim.physics.fuel = fuel_at_the_line
            complete_lap(sim, col, ms)
        return step

    sim = FakeSim()
    sim.physics.fuel = 40.0
    with temp_db() as path:
        run_collector([lap(38.2, 114900), lap(35.7, 114000),
                       lap(33.3, 114500)], path, sim)
        conn = db.connect(path)
        burns = [r["fuel_used_l"] for r in db.list_laps(conn, limit=None)]
        conn.close()
    assert burns[::-1] == [None, 2.5, 2.4], burns
    print(f"  burns {burns[::-1]}")


_MEASURED_FUEL_PROBE = """
import json
from assetto_mcp import db, server

conn = server._conn
server._live_fuel_facts = lambda: ({}, "AC is not running")
sid = db.create_session(conn, car="nsx", track="mugello", track_config="",
                        tyre_compound="S", air_temp=25.0, road_temp=30.0)
conn.execute("UPDATE sessions SET km_per_liter = 2.5, track_length_m = 5000,"
             " fuel_rate = 1.0 WHERE id = ?", (sid,))
conn.commit()
server._active_session = lambda explicit=None: sid
tool = getattr(server.fuel_plan, "fn", server.fuel_plan)
snap = {"sim": "ac", "car": "nsx", "track": "mugello", "track_config": "",
        "fuel_l": 9.0, "laps_remaining": 3, "lap_done": 0.5}


def bases(**kw):
    plan = json.loads(tool(race_laps=10, session_id=sid, **kw))
    return [plan["liters_per_lap"], plan["liters_per_lap_source"].split(":")[0],
            server._fuel_to_finish(snap)["basis"]]


out = {}
# An out-lap's burn and a lap nothing measured are not a lap's burn.
for n, (used, out_lap) in enumerate(((2.6, False), (2.4, False),
                                     (9.0, True), (None, False)), 1):
    db.store_lap(conn, sid, n, 114000, True, [], out_lap=out_lap,
                 fuel_used_l=used)
out["two_measured"] = bases()
db.store_lap(conn, sid, 5, 114000, True, [], fuel_used_l=2.5)
out["three_measured"] = bases()
out["given"] = bases(liters_per_lap=3.1)
print(json.dumps(out))
"""


def test_the_burn_is_given_then_measured_then_nominal():
    got = _run_probe(_MEASURED_FUEL_PROBE)
    assert got == {"two_measured": [2.0, "nominal", "nominal"],
                   "three_measured": [2.5, "measured", "measured"],
                   "given": [3.1, "given", "measured"]}, got
    print(f"  {got}")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
