"""Fuel per lap, derived from the game rather than looked up.

Both inputs previously had to be found by hand: KM_PER_LITER by decrypting
data.acd, track length from memory. That worked once, for one car, at one
circuit, and produced numbers nobody could check. The in-game app now reads
both -- ac.INIConfig.carData() gets at fuel_cons.ini even inside an
encrypted archive, and trackLengthM comes from the AI spline.
"""

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
    assert abs(out["total_litres"] - 52.9) < 0.5, out["total_litres"]
    assert out["stop_required_for_fuel"] is True
    print(f"  {out['litres_per_lap']} L/lap, {out['total_litres']} L total, "
          f"tank {TANK} -> stop forced")


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
