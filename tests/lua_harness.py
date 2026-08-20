"""Load the in-game Lua app under a stubbed CSP API.

The app runs inside Assetto Corsa against CSP's Lua bindings, which do not
exist anywhere else -- so the only way to test its logic off the gaming PC
is to supply those bindings ourselves. Everything CSP provides that the app
touches is stubbed here, and the stubs record what the app asked for, which
is usually the thing under test: whether a physics worker was started,
whether a batch was posted, and for which tier.

Requires `lupa`. Without it the tests skip, the same way the syntax check
skips without `luaparser` -- the gaming PC has Python because the server
needs it and no reason to have anything else. In CI that skip would be a
green build over an untested app, so setting AC_TESTS_STRICT=1 turns every
one of those skips into a hard failure; see `require` below.
"""

import importlib
import json
import os
import sys
import unittest
import warnings
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "lua_app" / "race_engineer"

# CSP runs the in-game app on LuaJIT 2.1, which is Lua 5.1 with extensions.
# lupa ships several interpreters in one wheel and bare lupa.LuaRuntime()
# picks the newest -- 5.5 at the time of writing. That is a two-way false
# signal, and both directions have teeth: `7 // 2` and `3 & 1` are ordinary
# arithmetic under 5.5 and syntax errors in the game, while
# string.format('%d', 1.5) works in the game and raises under 5.5. So the
# harness asks for LuaJIT by name.
LUAJIT_MODULE = "lupa.luajit21"

STRICT_ENV = "AC_TESTS_STRICT"
# run_tests.py sets this for its in-process runs. It matters because the
# three runners disagree about exceptions: pytest and run_tests.py read
# unittest.SkipTest as a skip, while the bare standalone runner in
# tests/support.py counts every exception as a failure -- so on the gaming
# PC a skip has to be printed rather than raised.
RUNNER_ENV = "AC_TESTS_RUNNER"
# Exit code a module uses when it ran nothing because an optional
# dependency is absent. 77 is the autotools convention for "skipped"; the
# point is only that it is neither 0 nor 1, so run_tests.py --isolate can
# tell a skipped module from a passing one. Mirrored in run_tests.py.
SKIP_EXIT = 77


# --- optional dependencies ---------------------------------------------


def strict() -> bool:
    """Whether a missing optional test dependency must fail, not skip.

    Two audiences, opposite needs. On the gaming PC lupa and luaparser are
    not installed and never will be, and a skip there is correct. In CI they
    are declared in the [test] extra, so their absence means the install did
    not do what it claims -- and a skipped module reports exactly like a
    passing one. CI sets AC_TESTS_STRICT=1 (run_tests.py --no-skip does the
    same for its children) so the two cases stop looking alike.
    """
    return os.environ.get(STRICT_ENV, "").strip().lower() not in (
        "", "0", "false", "no", "off")


def missing(module: str) -> bool:
    """True if `module` cannot be imported."""
    try:
        importlib.import_module(module)
        return False
    except ImportError:
        return True


def require(module: str, what: str) -> bool:
    """True if `module` is absent and the caller should skip.

    Raises instead when strict() -- so the same line reads as "skip on the
    gaming PC" and "fail the build" in CI, without either being written out
    twice and drifting apart.
    """
    if not missing(module):
        return False
    if strict():
        raise RuntimeError(
            f"{module} is not installed, so {what} would be skipped. "
            f"{STRICT_ENV} is set, which means this is CI or "
            f"run_tests.py --no-skip, where a skip is a failure: "
            f"`pip install -e \".[test]\"` should have provided it.")
    return True


def skip(reason: str) -> None:
    """Skip the running test, in whatever way the current runner understands.

    unittest.SkipTest rather than pytest.skip, because pytest reads it and
    it needs nothing installed -- but tests/support.py's standalone runner
    counts any exception as a failure, and that is the runner on the machine
    where skipping is the *correct* outcome. So there the skip is printed
    instead, and the caller must `return` immediately after calling this.
    """
    understood = ("pytest" in sys.modules
                  or os.environ.get(RUNNER_ENV, "") == "run_tests")
    if understood:
        raise unittest.SkipTest(reason)
    print(f"  skipped: {reason}")


def available() -> bool:
    return not missing("lupa")


# --- which Lua ----------------------------------------------------------


def runtime_module():
    """The lupa binding to use: LuaJIT 2.1, the one CSP runs.

    Falls back to whatever bare lupa offers if the wheel has no LuaJIT, with
    a warning -- test_lua_app has a test that fails outright on that path, so
    a wheel that stops shipping LuaJIT is visible rather than silent.
    """
    try:
        return importlib.import_module(LUAJIT_MODULE)
    except ImportError:
        import lupa
        warnings.warn(
            f"{LUAJIT_MODULE} is not in this lupa build; falling back to "
            f"its default runtime. The game runs LuaJIT 2.1 (Lua 5.1), so "
            f"anything version-sensitive is now being tested against the "
            f"wrong interpreter.", RuntimeWarning, stacklevel=2)
        return lupa


def new_runtime(**kwargs):
    """A Lua runtime, LuaJIT where the wheel provides one."""
    module = runtime_module()
    lua = module.LuaRuntime(**kwargs)
    if module.__name__ == LUAJIT_MODULE:
        # Asking for luajit21 and getting something else would mean the
        # binding is not what its name says, which is worth catching here
        # rather than as a puzzling syntax error three tests later.
        version = lua.eval("_VERSION")
        jit_version = lua.eval("jit and jit.version or nil")
        assert version == "Lua 5.1" and jit_version, (
            f"{LUAJIT_MODULE} produced {version!r} (jit: {jit_version!r}), "
            f"expected Lua 5.1 on LuaJIT")
    return lua


def runtime_info() -> dict:
    """What the tests are actually running on -- for CI to print and assert."""
    lua = new_runtime()
    return {
        "module": runtime_module().__name__,
        "lua_version": lua.eval("_VERSION"),
        "jit": lua.eval("jit and jit.version or nil"),
    }


class Recorder:
    """What the app did, as seen from the CSP side of the boundary."""

    def __init__(self):
        self.posts = []          # (path, decoded body)
        self.gets = []
        self.logs = []
        self.warnings = []
        self.workers_started = []
        self.worker_start_ok = True
        self.worker_start_error = "physics scripting is not available"
        # None means "the field does not exist", which is different from
        # False and is a case the app is required to handle.
        self.online = False
        self.physics_available = True
        self.sim_fields = {"raceSessionType": 1, "carsCount": 1}


def load(rec: Recorder = None, patch_version="0.2.11"):
    """Return (lua_runtime, exported_test_table, recorder)."""
    # Resolved here rather than at module scope: the module has to be
    # importable without lupa so `require` can decide between skipping and
    # failing. It has to be the same binding new_runtime() used, too --
    # lupa.lua_type() does not recognize tables belonging to a sibling
    # interpreter, so the top-level lupa reports LuaJIT's tables as None.
    lupa_module = runtime_module()

    rec = rec or Recorder()
    lua = new_runtime(unpack_returned_tuples=True)
    g = lua.globals()

    def _to_py(value):
        """A Lua value as the nearest Python one, recursively.

        Lua has one table type for both objects and arrays, so the shape has
        to be inferred: keys 1..n and nothing else is a sequence, anything
        else is a mapping. An empty table is genuinely ambiguous and becomes
        an object, which is what CSP's JSON.stringify emits for one; the app
        never posts an empty batch, so nothing depends on the choice.
        """
        if lupa_module.lua_type(value) != "table":
            return value
        items = {k: v for k, v in value.items()}
        if items and all(isinstance(k, int) for k in items) \
                and set(items) == set(range(1, len(items) + 1)):
            return [_to_py(items[i]) for i in range(1, len(items) + 1)]
        return {str(k): _to_py(v) for k, v in items.items()}

    def _json_stringify(tbl):
        """The real thing, not a placeholder.

        A stub that returned a constant made every POST body identical, so
        no test could tell a worker batch from an app one -- which is the
        single most important thing about a suspension post.
        """
        return json.dumps(_to_py(tbl))

    def _json_parse(s):
        return lua.table_from({"ok": True, "stored": 0})

    # --- ac -------------------------------------------------------------
    def get_car(_i=0):
        return lua.table_from({
            "splinePosition": 0.5, "lapCount": 1, "speedKmh": 180.0,
            "brake": 0.0, "gas": 1.0, "gear": 4, "isConnected": True,
            "isInPitlane": False, "carId": "rss_formula_rss_4",
            "bestLapTimeMs": 113000, "previousLapTimeMs": 113500,
        })

    def sim_fields():
        fields = dict(rec.sim_fields)
        if rec.online is not None:
            fields["isOnlineRace"] = rec.online
        return lua.table_from(fields)

    def control_button(_name):
        return lua.table_from({"pressed": lambda *_: False,
                               "configure": lambda *_: None})

    def connect(_layout):
        return lua.table_from({"writeIndex": 0, "running": 0,
                               "samples": lua.table_from({})})

    def start_worker(name):
        rec.workers_started.append(str(name))
        if not rec.worker_start_ok:
            raise RuntimeError(rec.worker_start_error)
        return True

    def web_post(url, body):
        # Bodies reach here as the JSON the app built, so record what the
        # server would see rather than the wire text. /note queues bodies
        # that are already strings and a future endpoint might post
        # something that is not JSON at all, so fall back to the raw text.
        text = str(body)
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = text
        rec.posts.append((str(url), decoded))

    # Python callables reach Lua as userdata, so `type(f) == 'function'` --
    # which is exactly what this app checks before using a CSP binding --
    # is false for them. Every stub therefore has to be a real Lua function
    # closing over the Python callback.
    g._py = lua.table_from({
        "start_worker": start_worker,
        "post": web_post,
        "get": lambda url: rec.gets.append(str(url)),
        "log": lambda s: rec.logs.append(str(s)),
        "warn": lambda s: rec.warnings.append(str(s)),
        "sim_fields": sim_fields,
        "car": get_car,
        "version": lambda: patch_version,
        "stringify": _json_stringify,
    })
    g._physics_available = rec.physics_available

    lua.execute("""
      local py = _py

      -- CSP's sim proxy RAISES on an unknown field rather than returning
      -- nil. Reproducing that is the whole point: an unguarded read of a
      -- field that did not exist once took down the worker probe entirely.
      local function proxy(fields)
        return setmetatable({}, {__index = function(_, k)
          local v = fields[k]
          if v == nil then error("unknown field '" .. tostring(k) .. "'", 2) end
          return v
        end})
      end

      ac = {
        getCar = function(_) return py.car() end,
        getSim = function() return proxy(py.sim_fields()) end,
        getDriverName = function(_) return 'Driver' end,
        ControlButton = function(_)
          return { pressed = function() return false end,
                   configure = function() end }
        end,
        connect = function(_)
          return { writeIndex = 0, running = 0, samples = {} }
        end,
        log = function(s) py.log(s) end,
        warn = function(s) py.warn(s) end,
        getPatchVersion = function() return py.version() end,
        getPatchVersionCode = function() return 2711 end,
        StructItem = setmetatable({}, {__index = function()
          return function() return 0 end end}),
      }

      if _physics_available then
        physics = {
          startPhysicsWorker = function(name, _idx, _cb)
            return py.start_worker(name)
          end,
          allowed = function() return true end,
          getExtendedDamperTravel = function(_c, _w) return 0.05 end,
        }
      end

      web = {
        post = function(url, _h, body, _cb) py.post(url, body) end,
        get = function(url, _cb) py.get(url) end,
      }
      JSON = {
        stringify = function(t) return py.stringify(t) end,
        parse = function(_) return { ok = true, stored = 0 } end,
      }
      vec2 = function() return {} end
      rgbm = function() return {} end
      ui = setmetatable({}, {__index = function()
        return function() return false end end})
      script = {}
    """)

    lua.execute((APP / "race_engineer.lua").read_text(encoding="utf-8"))
    return lua, g.script.__test, rec
