"""Shared test harness: fake shared memory, a collector driver, HTTP helpers.

Everything here exists so the suite runs on any OS with nothing installed --
no Windows, no Assetto Corsa, no network beyond localhost. The collector is
driven through a fake SimInfo and the bridge is exercised over real HTTP.
"""

import http.client
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ac_race_engineer import db  # noqa: E402
from ac_race_engineer.collector import Collector  # noqa: E402

AC_OFF = 0
AC_LIVE = 2


# --- fake shared memory -------------------------------------------------


class _Phys:
    def __init__(self):
        self.packetId = 0
        self.gas = 1.0
        self.brake = 0.0
        self.steerAngle = 0.1
        self.speedKmh = 120.0
        self.gear = 4
        self.rpms = 9000
        self.accG = [0.5, 0.0, 0.2]
        self.wheelSlip = [0.4, 0.4, 0.3, 0.3]
        self.wheelsPressure = [26.0] * 4
        self.tyreCoreTemperature = [85.0] * 4
        self.rideHeight = [0.02, 0.024]
        self.numberOfTyresOut = 0
        self.airTemp = 25.0
        self.roadTemp = 36.0


class _Graph:
    def __init__(self):
        self.status = AC_LIVE
        self.completedLaps = 0
        self.iLastTime = 0
        self.normalizedCarPosition = 0.0
        self.isInPitLane = 0
        self.tyreCompound = "F200 (S)"


class _Static:
    carModel = "rss_formula_rss_4"
    track = "mugello"
    trackConfiguration = "mugello_osrw"


class FakeSim:
    def __init__(self):
        self.physics = _Phys()
        self.graphics = _Graph()
        self.static = _Static()

    def close(self):
        pass


class FakeCollector:
    """Stands in for Collector where only its reported state matters.

    running and session_id are separate on purpose: the real collector
    leaves session_id set after stop(), and several tests exist because
    trusting it unconditionally files data against a dead session.
    """

    def __init__(self, session_id=None, running=False, status="idle"):
        self.session_id = session_id
        self.running = running
        self.status = status
        self.laps_recorded = 0
        self.last_error = None


# --- driving the collector ----------------------------------------------


def run_collector(script, db_path):
    """Drive a Collector through `script`, a list of fn(sim) steps."""
    sim = FakeSim()
    col = Collector(db_path, lambda: sim)
    col.start()
    for step in script:
        step(sim)
        time.sleep(0.06)
    time.sleep(0.15)
    col.stop()
    return col


def tick(sim, n=6):
    """Advance physics packets so samples accumulate."""
    for _ in range(n):
        sim.physics.packetId += 1
        sim.graphics.normalizedCarPosition = (
            sim.graphics.normalizedCarPosition + 0.1) % 1.0
        time.sleep(0.01)


def complete_lap(sim, lap_time_ms):
    sim.graphics.completedLaps += 1
    sim.graphics.iLastTime = lap_time_ms


# --- database -----------------------------------------------------------


def make_session(conn, track="mugello", car="ks_mazda_mx5_cup"):
    return db.create_session(conn, car=car, track=track, track_config="",
                             tyre_compound="SM", air_temp=24.0,
                             road_temp=31.0)


def age_session(conn, session_id, seconds):
    """Backdate a session so staleness logic can be exercised."""
    conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?",
                 (time.time() - seconds, session_id))
    conn.commit()


# --- HTTP ---------------------------------------------------------------


def post(port, path, obj=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = raw if raw is not None else json.dumps(obj).encode()
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(payload)
    except ValueError:
        return resp.status, payload


def get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    payload = json.loads(resp.read())
    conn.close()
    return payload


# --- standalone runner --------------------------------------------------


def run_module(namespace) -> int:
    """Run a module's test_* functions without pytest.

    The gaming PC has Python but not necessarily pytest, and being able to
    run a single file there is the difference between diagnosing something
    on the spot and not.
    """
    failures = 0
    for name, fn in sorted(namespace.items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        print(f"\n{name}")
        try:
            fn()
            print("  PASS")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR: {type(e).__name__}: {e}")
    print(f"\n{'all passed' if not failures else f'{failures} FAILED'}")
    return failures
