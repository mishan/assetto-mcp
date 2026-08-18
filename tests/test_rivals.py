"""End-to-end tests for opponent telemetry.

Drives real HTTP through the real bridge into a real SQLite file, then out
through the comparison, so the wire format the Lua app has to produce is
exercised rather than assumed.
"""

import json
import math
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ac_race_engineer import analysis, db  # noqa: E402
from ac_race_engineer.bridge import Bridge  # noqa: E402


class FakeCollector:
    """Stands in for the running collector: the bridge only needs an id."""

    def __init__(self, session_id=1):
        self.session_id = session_id
        self.running = True
        self.status = "recording (session 1)"
        self.laps_recorded = 0


def _post(port, path, obj):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _lap_trace(offset_kmh=0.0, with_inputs=True, car_index=1, lap_count=3):
    """A synthetic lap: speed dips at three corners, optional pedal inputs."""
    out = []
    for i in range(200):
        pos = i / 200
        base = 200.0
        for apex in (0.15, 0.5, 0.85):
            base -= 90 * math.exp(-((pos - apex) ** 2) / 0.0008)
        car = {
            "car_index": car_index,
            "lap_count": lap_count,
            "spline": pos,
            "speed_kmh": max(60.0, base + offset_kmh),
            "driver_name": "Rival One",
            "car_model": "rss_formula_rss_4",
            "best_lap_ms": 112500,
            "last_lap_ms": 113000,
        }
        if with_inputs:
            braking = any(apex - 0.06 < pos < apex - 0.01
                          for apex in (0.15, 0.5, 0.85))
            car["brake"] = 0.9 if braking else 0.0
            car["gas"] = 0.0 if braking else 1.0
            car["gear"] = 3 if braking else 5
        out.append(car)
    return out


def _my_samples(offset_kmh=0.0):
    """My lap in the collector's own column names."""
    out = []
    for i in range(200):
        pos = i / 200
        base = 200.0
        for apex in (0.15, 0.5, 0.85):
            base -= 90 * math.exp(-((pos - apex) ** 2) / 0.0008)
        braking = any(apex - 0.05 < pos < apex - 0.01
                      for apex in (0.15, 0.5, 0.85))
        out.append({
            "norm_pos": pos,
            "speed_kmh": max(60.0, base + offset_kmh),
            "brake": 0.9 if braking else 0.0,
            "gas": 0.0 if braking else 1.0,
        })
    return out


def test_bridge_accepts_and_stores_batch():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        db.connect(path).close()
        b = Bridge(path, FakeCollector(1), port=0)
        b.start()
        try:
            code, body = _post(b.port, "/rivals",
                               {"cars": _lap_trace()})
            assert code == 200 and body["ok"], body
            assert body["stored"] == 200, body
            assert body["skipped"] == 0, body

            conn = db.connect(path)
            rivals = db.list_rivals(conn, 1)
            assert len(rivals) == 1, rivals
            assert rivals[0]["driver_name"] == "Rival One"
            assert rivals[0]["best_lap_ms"] == 112500
            samples = db.get_rival_lap_samples(conn, 1, 1, 3)
            assert len(samples) == 200
            print(f"  stored {body['stored']} samples for "
                  f"{rivals[0]['driver_name']}")
            conn.close()
        finally:
            b.stop()


def test_malformed_cars_skipped_not_fatal():
    """One bad car must not cost us the other nineteen."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        db.connect(path).close()
        b = Bridge(path, FakeCollector(1), port=0)
        b.start()
        try:
            cars = _lap_trace()[:5]
            cars.append({"car_index": "not a number", "lap_count": 1,
                         "spline": 0.5, "speed_kmh": 100})
            cars.append({"car_index": 2})              # missing fields
            cars.append("not even a dict")
            code, body = _post(b.port, "/rivals", {"cars": cars})
            assert code == 200, body
            assert body["stored"] == 5, body
            assert body["skipped"] == 3, body
            print(f"  stored {body['stored']}, skipped {body['skipped']}")
        finally:
            b.stop()


def test_batch_rejected_when_not_recording():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        db.connect(path).close()
        col = FakeCollector(1)
        col.session_id = None
        b = Bridge(path, col, port=0)
        b.start()
        try:
            code, body = _post(b.port, "/rivals", {"cars": _lap_trace()})
            assert code == 200 and body["ok"] is False, body
            assert body["reason"] == "not recording", body
            print("  correctly refused while not recording")
        finally:
            b.stop()


def test_oversized_batch_rejected():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        db.connect(path).close()
        b = Bridge(path, FakeCollector(1), port=0)
        b.start()
        try:
            cars = _lap_trace()[:1] * 2500
            code, body = _post(b.port, "/rivals", {"cars": cars})
            assert code == 400, (code, body)
            print("  oversized batch rejected:", body["error"])
        finally:
            b.stop()


def test_comparison_finds_where_rival_gains():
    """Rival is 10 km/h faster everywhere; deltas must say so."""
    rival = [{"spline": c["spline"], "speed_kmh": c["speed_kmh"],
              "gas": c.get("gas"), "brake": c.get("brake")}
             for c in _lap_trace(offset_kmh=10.0)]
    result = analysis.compare_to_rival(_my_samples(0.0), rival)

    assert result["track_coverage_pct"] > 95, result["track_coverage_pct"]
    assert 9.0 < result["mean_delta_kmh"] < 11.0, result["mean_delta_kmh"]
    assert all(s["delta_kmh"] > 0 for s in result["worst_deficits"])
    assert result["rival_input_fields"]["brake"]["varies"] is True
    assert "brake_points" in result
    assert len(result["brake_points"]["theirs"]) == 3, result["brake_points"]
    print(f"  mean delta {result['mean_delta_kmh']} km/h, "
          f"brake points {result['brake_points']['theirs']}")


def test_dead_input_fields_are_reported_not_believed():
    """A constant 0.0 brake column means 'not transmitted', not 'never braked'."""
    rival = [{"spline": c["spline"], "speed_kmh": c["speed_kmh"],
              "gas": 0.0, "brake": 0.0}
             for c in _lap_trace(with_inputs=False)]
    result = analysis.compare_to_rival(_my_samples(), rival)

    assert result["rival_input_fields"]["brake"]["present"] is True
    assert result["rival_input_fields"]["brake"]["varies"] is False
    assert "brake_points" not in result, "must not invent braking points"
    assert "inputs_note" in result
    print("  dead inputs flagged:", result["inputs_note"][:58] + "...")


def test_missing_input_fields_handled():
    """Server sends no pedal data at all -> nulls, not crashes."""
    rival = [{"spline": c["spline"], "speed_kmh": c["speed_kmh"],
              "gas": None, "brake": None}
             for c in _lap_trace(with_inputs=False)]
    result = analysis.compare_to_rival(_my_samples(), rival)
    assert result["rival_input_fields"]["brake"]["present"] is False
    assert result["mean_delta_kmh"] is not None
    print("  absent inputs handled, speed comparison still works")


def test_partial_coverage_is_surfaced():
    """Half a captured lap must not masquerade as a full comparison."""
    rival = [{"spline": c["spline"], "speed_kmh": c["speed_kmh"]}
             for c in _lap_trace() if c["spline"] < 0.5]
    result = analysis.compare_to_rival(_my_samples(), rival)
    assert 45 < result["track_coverage_pct"] < 55, result
    print(f"  coverage reported as {result['track_coverage_pct']}%")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        print(f"\n{name}")
        try:
            fn()
            print("  PASS")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL: {e}")
    print(f"\n{'all passed' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
