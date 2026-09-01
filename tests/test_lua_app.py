"""In-game Lua app behavior, run against a stubbed CSP API.

Syntax-checking the app caught nothing, because none of the bugs it has
shipped were syntax errors. They were a scheduler whose comment disagreed
with its code, a diagnostic that threw and disabled the thing it inspected,
and an expression whose operator precedence made it a constant. All three
would have been caught by running it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lua_harness  # noqa: E402
from support import run_module  # noqa: E402

# Raises when AC_TESTS_STRICT is set, which is what CI and
# run_tests.py --no-skip do: there, lupa missing is a broken install, and a
# module that skips reports identically to one that passes. Everywhere else
# this is just a bool and the tests skip.
SKIP = lua_harness.require("lupa", "the Lua app tests")

# Without this, pytest collects these tests, calls lua_harness.load(), and
# reports ImportError as an ERROR rather than a skip -- so a machine simply
# lacking lupa looks like a broken build. pytest itself is optional here
# (every module runs standalone for the gaming PC), hence the guarded import.
try:
    import pytest
    pytestmark = pytest.mark.skipif(
        SKIP, reason="pip install lupa to run the Lua app tests")
except ImportError:  # pragma: no cover - standalone runner path
    pass


def _posted_sources(rec):
    """Which tier each POST to /suspension carried."""
    out = []
    for url, body in rec.posts:
        if url.endswith("/suspension"):
            out.append(body["source"])
    return out


def test_the_runtime_is_the_lua_the_game_runs():
    """CSP runs LuaJIT 2.1, which is Lua 5.1 -- not whatever lupa ships newest.

    lupa bundles 5.1 through 5.5 plus LuaJIT in one wheel and its default
    LuaRuntime picks the newest. Testing on 5.5 is wrong in both directions:
    `7 // 2` and `3 & 1` run there and are syntax errors in the game, and
    string.format('%d', 1.5) raises there and works in the game. Without
    this test a wheel that drops LuaJIT would quietly move the whole Lua
    suite onto an interpreter the app never meets.
    """
    info = lua_harness.runtime_info()
    assert info["module"] == lua_harness.LUAJIT_MODULE, info
    assert info["lua_version"] == "Lua 5.1", info
    assert info["jit"] and info["jit"].startswith("LuaJIT 2.1"), info

    # The load-bearing consequence, asserted rather than assumed: 5.1 has no
    # integer division operator, so a 5.x-ism in the app is a syntax error
    # here exactly as it would be in the game.
    lua = lua_harness.new_runtime()
    try:
        lua.eval("7 // 2")
    except Exception:                       # noqa: BLE001 - LuaSyntaxError
        pass
    else:
        raise AssertionError("'7 // 2' parsed, so this is not Lua 5.1")
    print(f"  {info['lua_version']} on {info['jit']}")


def test_the_app_loads_under_a_stubbed_csp():
    lua, api, rec = lua_harness.load()
    assert api is not None, "app did not export its test table"
    st = api.state()
    assert st is not None


def test_both_tiers_get_posted_not_just_the_worker():
    """Regression: strict priority starved the app tier completely.

    The worker fills at 333Hz and drains once a second, so its buffer is
    never empty. Under `if worker then ... elseif app then ...` the app
    branch never ran, and ride height and wheel loads -- which only the app
    tier produces -- silently stopped arriving while the status line
    reported a healthy 333Hz feed.
    """
    lua, api, rec = lua_harness.load()
    api.setRunning(True)

    for _ in range(10):
        api.push("worker", 333)      # worker always has data waiting
        api.push("app", 10)
        api.postSuspension()
        # The stubbed web.post never invokes its callback, so suspBusy
        # would latch. Clear it the way a completed request would.
        api.clearBusy()

    # The tier is in the body, not the URL -- both tiers post to
    # /suspension, so counting posts proves nothing about which one ran.
    sources = _posted_sources(rec)
    st = api.state()
    assert len(sources) >= 5, sources
    assert "worker" in sources, sources
    assert "app" in sources, sources
    # If the app tier were starved its buffer would grow without bound.
    assert st.appBuffered < 100, (
        f"app tier starved: {st.appBuffered} samples backed up")
    print(f"  {len(sources)} posts, sources {sorted(set(sources))}, "
          f"app backlog {st.appBuffered}")


def test_nothing_is_posted_when_the_server_is_not_recording():
    lua, api, rec = lua_harness.load()
    api.setRunning(False)
    api.push("worker", 500)
    api.push("app", 50)
    api.postSuspension()
    assert not [u for u, _ in rec.posts if u.endswith("/suspension")]
    st = api.state()
    assert st.workerBuffered == 0 and st.appBuffered == 0, \
        "buffers should be dropped, not held, while nothing is recording"


def test_multiplayer_suppresses_the_worker_without_an_error():
    """CSP forbids physics scripting online; that is not a malfunction."""
    rec = lua_harness.Recorder()
    rec.online = True
    lua, api, rec = lua_harness.load(rec)
    api.startSuspensionWorker()

    st = api.state()
    assert rec.workers_started == [], \
        "must not attempt a worker in an online session"
    assert st.onlineSuppressed is True, st.onlineSuppressed
    assert "multiplayer" in st.suspNote.lower(), st.suspNote
    assert not rec.warnings, f"should not warn about an expected state: {rec.warnings}"
    print(f"  online -> {st.suspNote!r}, no warning raised")


def test_offline_does_attempt_the_worker():
    rec = lua_harness.Recorder()
    rec.online = False
    lua, api, rec = lua_harness.load(rec)
    api.startSuspensionWorker()
    assert rec.workers_started == ["suspension_worker"], rec.workers_started
    st = api.state()
    assert st.onlineSuppressed is False


def test_an_unknown_online_flag_still_attempts_the_worker():
    """Being unable to tell is not a reason to disable the feature."""
    rec = lua_harness.Recorder()
    rec.online = None            # field absent entirely
    lua, api, rec = lua_harness.load(rec)
    # isOnlineSession() must report nil, not false, and not raise.
    assert api.isOnlineSession() is None
    api.startSuspensionWorker()
    assert rec.workers_started == ["suspension_worker"], rec.workers_started


def test_a_failed_worker_start_keeps_the_reason():
    """pcall's second return is the only description of what went wrong."""
    rec = lua_harness.Recorder()
    rec.worker_start_ok = False
    rec.worker_start_error = "physics scripting unavailable in this session"
    lua, api, rec = lua_harness.load(rec)
    api.startSuspensionWorker()

    st = api.state()
    assert "start failed" in st.suspNote, st.suspNote
    assert "physics scripting unavailable" in st.suspNote, st.suspNote
    assert any("startPhysicsWorker failed" in w for w in rec.warnings), \
        rec.warnings
    print(f"  reason preserved: {st.suspNote!r}")


def test_the_environment_probe_cannot_break_the_worker():
    """A diagnostic that disables what it inspects is worse than none.

    ac.getSim() raises on unknown fields, and an unguarded read of one took
    down the whole probe -- the worker was never even attempted.
    """
    rec = lua_harness.Recorder()
    rec.sim_fields = {}
    rec.online = None     # every sim read raises
    lua, api, rec = lua_harness.load(rec)
    api.startSuspensionWorker()
    assert rec.workers_started == ["suspension_worker"], \
        "probe failure must not prevent the worker starting"
    assert any("environment" in l for l in rec.logs), rec.logs
    print(f"  probe logged {len(rec.logs)} lines and still started the worker")


def test_clamp_and_num_reject_nan_and_out_of_range():
    """These guard every value posted to the bridge, which validates strictly."""
    lua, api, rec = lua_harness.load()
    assert api.clamp(1.5, 0, 1) == 1
    assert api.clamp(-0.2, 0, 1) == 0
    assert api.clamp(0.4, 0, 1) == 0.4
    assert api.num(float("nan"), 7) == 7
    assert api.num(None, 7) == 7
    assert api.num("text", 7) == 7
    assert api.num(3.5, 7) == 3.5


def test_the_overlay_calls_out_laps_driven_but_not_stored():
    """The failure the driver actually hit, seen from the car.

    The overlay rendered whatever /status said, which made it exactly as
    trustworthy as the server. When the server was wrong -- an empty session
    asserting itself as recording -- the driver had a green light through
    seven laps that were never stored, and no way to check from the car.

    car.lapCount belongs to the game, not to us. Laps finishing while none
    are stored is a contradiction the app can see on its own.
    """
    lua, api, rec = lua_harness.load()
    api.setConnected(True)
    api.setRunning(True)

    # Recording starts with the game already at lap 3. That is the baseline,
    # not a shortfall.
    api.setLapCount(3)
    api.setLaps(0)
    assert api.recordingHealth()[0] != "not-storing", api.recordingHealth()

    # One lap since: could be an out-lap, which is skipped by design.
    api.setLapCount(4)
    assert api.recordingHealth()[0] != "not-storing", api.recordingHealth()

    # Seven finished since recording began, still nothing stored.
    api.setLapCount(10)
    state, detail = api.recordingHealth()
    assert state == "not-storing", state
    assert "7 laps driven" in detail and "0 stored" in detail, detail
    print(f"  baseline 3, lapCount 10 -> {state!r}: {detail!r}")


def test_a_recorder_that_started_late_does_not_cry_wolf():
    """The false alarm this warning produced the first time it mattered.

    The driver restarted the MCP server twenty-six laps into a game session.
    The collector opened cleanly and was waiting for lap twenty-seven. The
    overlay compared the game's twenty-six against the recorder's zero and
    put NOT STORING LAPS over a collector that was working perfectly.

    A warning that fires when nothing is wrong is worse than no warning: it
    is the one the driver learns to ignore, and this one exists because
    seven laps at Suzuka were lost while the light was green.
    """
    lua, api, rec = lua_harness.load()
    api.setConnected(True)
    api.setRunning(True)
    api.setLapCount(26)
    api.setLaps(0)

    state, detail = api.recordingHealth()
    assert state == "recording", (state, detail)
    assert api.baseline() == 26, api.baseline()
    print(f"  26 laps already driven, recording just started -> {state!r}")


def test_the_baseline_is_forgotten_when_recording_stops():
    """Otherwise the next run measures itself against the last one's start."""
    lua, api, rec = lua_harness.load()
    api.setConnected(True)
    api.setRunning(True)
    api.setLapCount(5)
    api.setLaps(0)
    api.recordingHealth()
    assert api.baseline() == 5, api.baseline()

    api.setRunning(False)
    api.recordingHealth()
    assert api.baseline() is None, api.baseline()

    # A second run starts counting from wherever the game now is.
    api.setRunning(True)
    api.setLapCount(9)
    api.recordingHealth()
    assert api.baseline() == 9, api.baseline()
    assert api.recordingHealth()[0] == "recording"
    print("  baseline cleared on stop and re-taken on the next run")


def test_storing_laps_reads_as_recording():
    lua, api, rec = lua_harness.load()
    api.setConnected(True)
    api.setRunning(True)
    api.setLapCount(7)
    api.setLaps(6)
    assert api.recordingHealth()[0] == "recording", api.recordingHealth()


def test_a_driver_who_has_not_finished_a_lap_is_not_a_failure():
    """Out-lap, or the first lap in progress: nothing is wrong yet."""
    lua, api, rec = lua_harness.load()
    api.setConnected(True)
    api.setRunning(True)
    api.setLapCount(0)
    api.setLaps(0)
    assert api.recordingHealth()[0] == "recording", api.recordingHealth()


def test_an_offline_bridge_is_reported_as_offline_not_as_data_loss():
    """Two different problems, and conflating them sends you after the
    wrong one: the bridge being down is not the collector being stopped."""
    lua, api, rec = lua_harness.load()
    api.setConnected(False)
    api.setLapCount(7)
    api.setLaps(0)
    assert api.recordingHealth()[0] == "offline", api.recordingHealth()


def test_a_server_restart_does_not_produce_a_false_alarm():
    """The same false alarm as above, reached through the common route.

    Clearing the baseline when status.running goes false does not cover a
    server RESTART, because that is not what a restart looks like from the
    car: the bridge simply stops answering, so recordingHealth returns
    'offline' before it reaches the branch that clears anything. The stale
    baseline then met a fresh recording with status.laps back at zero.

    A restart is the event the recorder heartbeat exists for and the thing
    that happens most, so the alarm was firing precisely when the driver
    had been told to expect one.
    """
    lua, api, rec = lua_harness.load()
    api.setConnected(True)
    api.setRunning(True)
    api.setSession(4)
    api.setLapCount(20)
    api.setLaps(0)
    api.recordingHealth()
    api.setLapCount(26)
    api.setLaps(6)
    assert api.recordingHealth()[0] == "recording"
    assert api.baseline() == 20, api.baseline()

    # The host replaces the server process. The bridge is unreachable for a
    # poll or two.
    api.setConnected(False)
    assert api.recordingHealth()[0] == "offline"
    assert api.baseline() is None, "the baseline outlived the connection"

    # It comes back, autostarts a collector, opens a new session. The driver
    # has not stopped driving and lap 27 will be stored normally.
    api.setConnected(True)
    api.setRunning(True)
    api.setSession(5)
    api.setLaps(0)
    api.setLapCount(27)
    state, detail = api.recordingHealth()
    assert state == "recording", (state, detail)
    assert api.baseline() == 27, api.baseline()
    print("  server restarted mid-session -> 'recording', baseline re-taken")


def test_a_new_session_re_takes_the_baseline():
    """Another instance taking the recorder over is a new recording.

    The connection never drops and status.running never goes false, so the
    session id is the only thing that says the window being counted has
    moved.
    """
    lua, api, rec = lua_harness.load()
    api.setConnected(True)
    api.setRunning(True)
    api.setSession(1)
    api.setLapCount(12)
    api.setLaps(9)
    api.recordingHealth()
    assert api.baseline() == 12

    api.setSession(2)          # takeover: new session, nothing stored in it
    api.setLaps(0)
    api.setLapCount(14)
    state, _ = api.recordingHealth()
    assert state == "recording", state
    assert api.baseline() == 14, api.baseline()
    print("  session changed under a live connection -> baseline re-taken")


def test_a_stored_count_going_backwards_re_takes_the_baseline():
    """The same event with the session id unchanged -- a database that was
    moved, or a count that reset for any reason we did not predict. A
    number that can only go up going down means the window changed."""
    lua, api, rec = lua_harness.load()
    api.setConnected(True)
    api.setRunning(True)
    api.setSession(1)
    api.setLapCount(30)
    api.setLaps(11)
    api.recordingHealth()
    assert api.baseline() == 30

    api.setLaps(0)
    api.setLapCount(33)
    assert api.recordingHealth()[0] == "recording"
    assert api.baseline() == 33, api.baseline()
    print("  stored count fell -> baseline re-taken instead of an alarm")


def test_the_test_helper_resets_every_piece_of_the_baseline():
    """resetBaseline has to clear the whole baseline, not a third of it.

    Asserted on the state directly rather than through behaviour, because
    behaviour cannot currently tell the difference: baselineSession and
    baselineLaps are reassigned unconditionally on the next call, so a
    helper clearing only recordingBaseline produces the same answers today.

    It is still worth fixing, and worth a test. The helper names a thing --
    "the baseline" -- that now has three parts, and one that clears one part
    is a trap for the next person: any new check that reads the stale two
    before they are rewritten would fail in tests only, and look like the
    code under test rather than the harness.
    """
    lua, api, rec = lua_harness.load()
    api.setConnected(True)
    api.setRunning(True)
    api.setSession(3)
    api.setLapCount(40)
    api.setLaps(12)
    api.recordingHealth()
    assert tuple(api.baselineState()) == (40, 3, 12), \
        tuple(api.baselineState())

    api.resetBaseline()
    lap, session, laps = api.baselineState()
    assert lap is None and session is None and laps == 0, (lap, session, laps)

    # And a fresh recording still measures itself from where it starts.
    api.setLapCount(41)
    api.setLaps(0)
    state, detail = api.recordingHealth()
    assert state == "recording", (state, detail)
    assert api.baseline() == 41, api.baseline()
    print("  resetBaseline clears the lap, the session and the stored count")


def test_the_lap_count_helper_survives_a_car_that_is_not_there_yet():
    """ac.getCar(0) can return nil early in load, and recordingHealth already
    reads `car and car.lapCount` for that reason. The test helper indexed it
    unguarded, so a harness that loaded the app before the stub car existed
    would fail inside setLapCount -- on the harness, not on the behaviour
    under test, and with a Lua error rather than an assertion.

    It reports whether it took, so a test cannot set nothing and then assert
    on it.
    """
    lua, api, rec = lua_harness.load()
    api.setConnected(True)
    api.setRunning(True)
    assert api.setLapCount(7) is True
    with_car = api.recordingHealth()[0]

    # Loaded before the car exists. `car` is a file-local, so this is the
    # only way to reach the nil path -- assigning a global of the same name
    # does not touch the app's upvalue.
    empty = lua_harness.Recorder()
    empty.car_available = False
    lua2, api2, _ = lua_harness.load(empty)
    api2.setConnected(True)
    api2.setRunning(True)

    assert api2.setLapCount(9) is False, "helper claimed a nil car took it"
    # And the app itself answers rather than raising, which is what the
    # `car and car.lapCount` guard in recordingHealth is already for.
    assert api2.recordingHealth()[0] is not None
    print(f"  car present -> {with_car!r}; loaded without a car -> helper "
          f"returns False and health still answers")

if __name__ == "__main__":
    if SKIP:
        # Not 0: exiting 0 made `run_tests.py --isolate` report this module
        # as passing when it had run nothing at all, which is how a build
        # with no lupa in it went green. SKIP_EXIT says "ran nothing",
        # and under AC_TESTS_STRICT the require() above has already raised.
        print("skipped - pip install lupa to run the Lua app tests")
        sys.exit(lua_harness.SKIP_EXIT)
    sys.exit(1 if run_module(globals()) else 0)
