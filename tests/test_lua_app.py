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


if __name__ == "__main__":
    if SKIP:
        # Not 0: exiting 0 made `run_tests.py --isolate` report this module
        # as passing when it had run nothing at all, which is how a build
        # with no lupa in it went green. SKIP_EXIT says "ran nothing",
        # and under AC_TESTS_STRICT the require() above has already raised.
        print("skipped - pip install lupa to run the Lua app tests")
        sys.exit(lua_harness.SKIP_EXIT)
    sys.exit(1 if run_module(globals()) else 0)
