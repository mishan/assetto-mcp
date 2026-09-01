"""Shared test harness: fake shared memory, a collector driver, HTTP helpers.

Everything here exists so the suite runs on any OS with nothing installed --
no Windows, no Assetto Corsa, no network beyond localhost. The collector is
driven through a fake SimInfo and the bridge is exercised over real HTTP.
"""

import contextlib
import http.client
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ac_race_engineer import db  # noqa: E402
from ac_race_engineer.collector import Collector  # noqa: E402

AC_OFF = 0
AC_LIVE = 2


# --- temp databases, and the handles that outlive them ------------------


def _open_handles(directory: Path) -> list[str]:
    """Files still open inside `directory`, on platforms that will say.

    Linux exposes this through /proc/self/fd. Nothing else here does, and
    that asymmetry is the whole point of this function -- see temp_db.
    """
    fds = Path("/proc/self/fd")
    if not fds.is_dir():
        return []
    out = []
    root = str(directory.resolve())
    for fd in fds.iterdir():
        try:
            target = os.readlink(fd)
        except OSError:          # the fd closed while we walked, or is ours
            continue
        if target.startswith(root + os.sep):
            out.append(target)
    return sorted(set(out))


@contextlib.contextmanager
def temp_db(name: str = "t.db"):
    """A temp directory for a database, with leaked handles reported.

    Yields the path the database should live at. On the way out it checks
    that nothing is still holding a file inside the directory, and names
    what is.

    This exists because the platforms disagree about whether forgetting
    `conn.close()` is an error. Windows refuses to delete an open file, so
    the TemporaryDirectory teardown raises WinError 32 -- from inside
    shutil, pointing at a temp path, with the test that leaked it named
    nowhere obvious. POSIX deletes it happily and says nothing at all.

    So a missing close is invisible to everyone developing on Linux and to
    the Linux CI leg, and red only on the Windows one. That is a slow and
    confusing way to find out, and it is how one reached a review stack:
    test_a_recorded_lap_carries_a_driving_line opened a connection to read
    its samples back and never closed it, passed everywhere it was run, and
    failed on a Windows runner four branches later.

    Reporting the leak here makes it fail on the machine the author is
    actually using, at the test that caused it, with the handle named.

    It also does its own removal rather than letting TemporaryDirectory do
    it, and that is the half that works on Windows. A background thread
    finishing is not a leak -- Collector.stop() joins for ten seconds and
    then RETURNS whether or not the thread went, by design, saying so in
    last_error -- so on a slow runner the directory is deleted out from
    under a thread that is still closing its connection. That is the
    NotADirectoryError this whole helper keeps being asked about, and no
    amount of handle-checking catches it on Windows, where /proc does not
    exist and _open_handles can only answer "no idea".

    So: wait for the thing to be deletable, and only call it a failure once
    it has had longer than stop() itself waits.
    """
    directory = Path(tempfile.mkdtemp(prefix="ac-test-"))
    try:
        yield directory / name
    finally:
        _release(directory)


# Longer than Collector.stop()'s ten-second join, so a thread that took the
# full join and a moment more still finishes inside it. Nothing waits this
# long in the normal case -- the first attempt succeeds.
RELEASE_TIMEOUT = 20.0


def _release(directory: Path) -> None:
    """Remove `directory`, reporting anything still holding it open.

    Two different failures, in the order they can be diagnosed:

    First a leaked handle, which only Linux can name. A short grace before
    reporting, because the bridge is a threading HTTP server: shutdown()
    stops it accepting, but a handler already inside a request keeps its own
    connection until it returns. That is a drain, not a leak, and calling it
    one would make the guard as flaky as the failure it replaces.

    Then the removal itself, retried, which is what Windows needs and what
    POSIX never notices.
    """
    deadline = time.monotonic() + 2.0
    leaked = _open_handles(directory)
    while leaked and time.monotonic() < deadline:
        time.sleep(0.02)
        leaked = _open_handles(directory)
    if leaked:
        shutil.rmtree(directory, ignore_errors=True)
        raise AssertionError(
            "database handles were still open when the test finished, "
            "which fails the Windows CI leg on teardown and nowhere "
            "else:\n  " + "\n  ".join(leaked)
            + "\nClose every connection this test opened.")

    deadline = time.monotonic() + RELEASE_TIMEOUT
    last: OSError | None = None
    while True:
        try:
            shutil.rmtree(directory)
            return
        except FileNotFoundError:
            return
        except OSError as e:                # WinError 32 / 267, mostly
            last = e
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    shutil.rmtree(directory, ignore_errors=True)
    raise AssertionError(
        f"{directory} could not be removed within {RELEASE_TIMEOUT:.0f}s: "
        f"{last}\nSomething is still holding a file open -- on Windows that "
        f"is usually a collector or bridge thread that outlived the test.")


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
        # Attitude and electronics activity, added with schema v8. Distinct
        # non-zero values so a test can tell a stored reading from a default.
        self.heading = 0.5
        self.pitch = 0.01
        self.roll = -0.02
        self.tc = 0.3
        self.abs = 0.4
        # Wear counts down from 100 in AC. Distinct per corner so a test can
        # catch a collector that stores one wheel's value four times.
        self.tyreWear = [99.5, 99.4, 99.2, 99.1]
        self.carDamage = [0.0] * 5


class _Graph:
    def __init__(self):
        self.status = AC_LIVE
        self.completedLaps = 0
        self.iLastTime = 0
        self.normalizedCarPosition = 0.0
        self.isInPitLane = 0
        self.tyreCompound = "F200 (S)"
        # World position: the only source of lateral placement, and so the
        # only source of a driving line. Advanced by tick() alongside
        # normalizedCarPosition so a stored lap traces a path rather than
        # repeating one point.
        self.carCoordinates = [100.0, 5.0, -200.0]


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


class TimedOut(AssertionError):
    """The collector never reached the state a step was waiting for."""


def wait_for(predicate, what, timeout=15.0, interval=0.002):
    """Block until `predicate()` is true, or fail saying what we wanted.

    Every wait in this harness goes through here rather than sleeping for a
    plausible-looking interval. A fixed sleep encodes an assumption about
    how fast the machine is, and the CI runners are not that machine: the
    sleep-based version of this file passed thousands of times locally and
    failed on GitHub's Windows and Ubuntu runners within a day.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise TimedOut(f"timed out after {timeout}s waiting for {what}")


def run_collector(script, db_path, sim=None):
    """Drive a Collector through `script`, a list of fn(sim, col) steps.

    The collector is fully stopped before returning, so callers can delete
    the database afterwards. Leaving its thread alive with the connection
    open is how a temp-directory cleanup turned into a NotADirectoryError
    from a thread nobody was watching.

    Pass `sim` to supply a modified stub -- an older shared-memory layout
    with fields removed, say.
    """
    if sim is None:
        sim = FakeSim()
    col = Collector(db_path, lambda: sim)
    col.start()
    try:
        wait_for(lambda: col.sessions_started > 0, "the first session")
        for i, step in enumerate(script):
            try:
                step(sim, col)
            except TimedOut as e:
                raise TimedOut(f"script step {i}: {e}") from None
    finally:
        col.stop()
        assert not col.running, col.last_error
    return col


def tick(sim, col, n=6):
    """Advance physics packets and wait for each to be sampled."""
    for _ in range(n):
        before = col.samples_taken
        sim.graphics.normalizedCarPosition = (
            sim.graphics.normalizedCarPosition + 0.1) % 1.0
        # Move in the world too, so a recorded lap is a path. A stationary
        # coordinate would let a collector that stored the same sample every
        # tick pass a test about storing position. Guarded on both presence
        # and length, because tests stand in for two different broken
        # layouts: one with the field removed, and one where it is there but
        # too short. The harness has to survive the same inputs the
        # collector does, or a test of the collector fails in here instead.
        coords = getattr(sim.graphics, "carCoordinates", None)
        if coords is not None and len(coords) >= 3:
            coords[0] += 12.0
            coords[2] -= 5.0
        sim.physics.packetId += 1
        wait_for(lambda: col.samples_taken > before, "a sample to be taken")


def complete_lap(sim, col, lap_time_ms, stored=True):
    """Cross the line, and wait for the lap to land in the database.

    stored=False for an out-lap, which has no meaningful time and is
    deliberately skipped.
    """
    before = col.laps_recorded
    sim.graphics.iLastTime = lap_time_ms
    sim.graphics.completedLaps += 1
    if stored:
        wait_for(lambda: col.laps_recorded > before, "the lap to be stored")


def enter_pits(sim, col):
    sim.graphics.isInPitLane = 1
    wait_for(lambda: col.current_lap_pitted, "the pit visit to be noticed")


def leave_pits(sim, col):
    sim.graphics.isInPitLane = 0


def go_off(sim, col):
    """Leave the session to the menus (AC_OFF)."""
    sim.graphics.status = AC_OFF
    wait_for(lambda: "waiting" in col.status, "the collector to go idle")


def go_live(sim, col):
    """Re-enter a session, lap counters reset as AC does on a fresh run."""
    before = col.sessions_started
    sim.graphics.completedLaps = 0
    sim.graphics.iLastTime = 0
    sim.graphics.status = AC_LIVE
    wait_for(lambda: col.sessions_started > before, "a new session")


def restart_from_menu(sim, col):
    """Restart in-game: the lap counter goes backwards, status never
    leaves AC_LIVE, which is why watching for AC_OFF alone missed it."""
    before = col.sessions_started
    sim.graphics.iLastTime = 0
    sim.graphics.completedLaps = 0
    wait_for(lambda: col.sessions_started > before, "a new session")


# --- database -----------------------------------------------------------


def make_session(conn, track="mugello", car="ks_mazda_mx5_cup"):
    return db.create_session(conn, car=car, track=track, track_config="",
                             tyre_compound="SM", air_temp=24.0,
                             road_temp=31.0)


def age_session(conn, session_id, seconds):
    """Backdate a whole session so staleness logic can be exercised.

    The heartbeat moves with it. Ageing started_at alone would describe a
    session that began an hour ago and is still being recorded right now,
    which is a real state -- see heartbeat_session -- but not the one any
    caller of this function means.
    """
    then = time.time() - seconds
    conn.execute("UPDATE sessions SET started_at = ?, last_seen_at = ?"
                 " WHERE id = ?", (then, then, session_id))
    conn.commit()


def heartbeat_session(conn, session_id, seconds_ago=0.0):
    """Set only the heartbeat: a session that started long ago and is live.

    This is the case the lap-based staleness rules could not express. A
    driver on the out-lap of a Nordschleife session has produced no laps
    and will not for ten minutes, and is unambiguously recording.
    """
    conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?",
                 (time.time() - seconds_ago, session_id))
    conn.commit()


def clear_heartbeat(conn, session_id):
    """A session recorded before v10: no heartbeat exists or ever will."""
    conn.execute("UPDATE sessions SET last_seen_at = NULL WHERE id = ?",
                 (session_id,))
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
