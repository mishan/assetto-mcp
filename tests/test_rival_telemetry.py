"""End-to-end tests for opponent telemetry.

Drives real HTTP through the real bridge into a real SQLite file, then out
through the comparison, so the wire format the Lua app has to produce is
exercised rather than assumed.
"""

import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (FakeCollector, age_session, make_session,  # noqa: E402
                     post as _post, run_module)

from ac_race_engineer import analysis, db  # noqa: E402
from ac_race_engineer.bridge import Bridge  # noqa: E402


def _recording(session_id=1):
    return FakeCollector(session_id=session_id, running=True,
                         status=f"recording (session {session_id})")


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
        b = Bridge(path, _recording(1), port=0)
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
        b = Bridge(path, _recording(1), port=0)
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


def test_batch_refused_when_nothing_is_recording():
    """With a stale session present, not merely an empty database.

    The original version of this test set session_id = None against an
    empty database, so it passed because there was nothing to file against
    at all -- it was really testing "no sessions exist". A three-day-old
    session is the case that matters: there is somewhere to put the data,
    and it is the wrong place.
    """
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        stale = make_session(conn, "monza")
        age_session(conn, stale, 3 * 86400)

        col = _recording(1)
        col.session_id = None
        col.running = False
        b = Bridge(path, col, port=0)
        b.start()
        try:
            code, body = _post(b.port, "/rivals", {"cars": _lap_trace()})
            assert code == 200 and body["ok"] is False, body
            assert body["reason"] == "not recording", body
            assert conn.execute(
                "SELECT COUNT(*) FROM rival_samples").fetchone()[0] == 0
            print("  refused, and nothing filed against the stale session")
        finally:
            b.stop()
            conn.close()


def test_batch_accepted_for_another_instances_live_session():
    """The other half of the same rule: a live session elsewhere counts."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        conn = db.connect(path)
        live = make_session(conn, "mugello")

        idle = FakeCollector(running=False)     # this instance isn't recording
        b = Bridge(path, idle, port=0)
        b.start()
        try:
            code, body = _post(b.port, "/rivals", {"cars": _lap_trace()})
            assert code == 200 and body["ok"] is True, body
            stored = conn.execute(
                "SELECT DISTINCT session_id FROM rival_samples").fetchall()
            assert [r[0] for r in stored] == [live], stored
            print(f"  filed against the recording instance's session {live}")
        finally:
            b.stop()
            conn.close()


# --- storage integrity --------------------------------------------------


def test_a_resent_batch_stores_no_duplicates():
    """The Lua app resends a batch whose response it never saw.

    Duplicates skew the spline-bucket means the comparison is built on.
    """
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        samples = [{"car_index": 1, "lap_count": 2, "spline": i / 100,
                    "speed_kmh": 120.0} for i in range(50)]
        drivers = [{"car_index": 1, "driver_name": "Rival"}]
        first = db.store_rival_batch(conn, sid, drivers, samples)
        again = db.store_rival_batch(conn, sid, drivers, samples)
        total = conn.execute(
            "SELECT COUNT(*) FROM rival_samples").fetchone()[0]
        assert (first, again, total) == (50, 0, 50), (first, again, total)
        print("  resend stored 0 duplicates")
        conn.close()


def test_driver_metadata_is_written_once_per_car():
    """It arrives stamped on every sample; a full grid at 10Hz would mean
    well over a thousand redundant upserts a second."""
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        drivers = [{"car_index": 1, "driver_name": "A", "lap_count": 0}
                   for _ in range(500)]
        samples = [{"car_index": 1, "lap_count": 0, "spline": i / 500,
                    "speed_kmh": 100.0} for i in range(500)]
        before = conn.total_changes
        db.store_rival_batch(conn, sid, drivers, samples)
        assert conn.total_changes - before <= 502, conn.total_changes - before
        assert conn.execute(
            "SELECT COUNT(*) FROM rival_drivers").fetchone()[0] == 1
        print("  500 driver entries collapsed to one upsert")
        conn.close()


def test_a_rivals_lap_time_is_tied_to_the_lap_it_belongs_to():
    """last_lap_ms is overwritten every batch, so the moment the lap
    counter advances is the only chance to record which lap it described.

    Without this, "their best lap" cannot be identified at all, and the
    comparison silently defaulted to the lap with the most samples -- the
    longest-observed, which if anything correlates with the slowest.
    """
    with tempfile.TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "t.db")
        sid = make_session(conn)
        db.store_rival_batch(
            conn, sid, [{"car_index": 3, "lap_count": 0}],
            [{"car_index": 3, "lap_count": 0, "spline": 0.5,
              "speed_kmh": 100.0}])
        db.store_rival_batch(
            conn, sid,
            [{"car_index": 3, "lap_count": 1, "last_lap_ms": 113500}],
            [{"car_index": 3, "lap_count": 1, "spline": 0.1,
              "speed_kmh": 100.0}])
        assert db.rival_lap_times(conn, sid, 3) == {0: 113500}
        print("  lap 0 recorded at 113500ms")
        conn.close()


def test_oversized_batch_rejected():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        db.connect(path).close()
        b = Bridge(path, _recording(1), port=0)
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
    sys.exit(1 if run_module(globals()) else 0)
