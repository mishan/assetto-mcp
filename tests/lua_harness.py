"""Load the in-game Lua app under a stubbed CSP API.

The app runs inside Assetto Corsa against CSP's Lua bindings, which do not
exist anywhere else -- so the only way to test its logic off the gaming PC
is to supply those bindings ourselves. Everything CSP provides that the app
touches is stubbed here, and the stubs record what the app asked for, which
is usually the thing under test: whether a physics worker was started,
whether a batch was posted, and for which tier.

Requires `lupa`. Without it the tests skip, the same way the syntax check
skips without `luaparser` -- the gaming PC has Python because the server
needs it and no reason to have anything else.
"""

from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "lua_app" / "race_engineer"


def available() -> bool:
    try:
        import lupa  # noqa: F401
        return True
    except ImportError:
        return False


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
    import lupa

    rec = rec or Recorder()
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()

    def _json_stringify(tbl):
        return "<json>"

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
        rec.posts.append((str(url), str(body)))

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
        stringify = function(_) return '<json>' end,
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
