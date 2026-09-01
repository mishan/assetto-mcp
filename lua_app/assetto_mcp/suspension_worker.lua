-- Physics worker: samples damper travel at AC's physics rate (333Hz).
--
-- Started by assetto_mcp.lua via physics.startPhysicsWorker(). This runs
-- on the physics thread, so it must stay cheap -- slow code here degrades
-- physics for the whole simulation, not just this app.
--
-- Why bother: damper velocity is a fast signal. Sampling suspension travel
-- on the render thread at 60-144Hz and differentiating it aliases the
-- high-speed band, which is exactly the band bump/rebound valving lives in.
-- A histogram built that way describes body motion, not dampers, and would
-- support confidently wrong advice about which clicks to change.
--
-- The worker writes into a shared ring buffer; the app drains it at render
-- rate and POSTs batches. Nothing crosses the thread boundary except the
-- struct, which is the pattern CSP's own CspDebug app uses.

local BUFFER = 1024        -- ~3 seconds at 333Hz, plenty for a render frame
                           -- to be late without losing samples

-- Layout must match assetto_mcp.lua exactly, field for field.
local shared = ac.connect({
  ac.StructItem.key('assetto_mcp.suspension'),
  -- writeIndex only ever climbs; the app tracks its own read position and
  -- works out what it missed from the difference. The worker cannot know
  -- what the app has read, so it does not try to count drops itself.
  writeIndex = ac.StructItem.int32(),
  running    = ac.StructItem.int32(),
  samples    = ac.StructItem.array(ac.StructItem.struct({
    t_ms      = ac.StructItem.int32(),
    spline    = ac.StructItem.float(),
    brake     = ac.StructItem.float(),
    speed_kmh = ac.StructItem.float(),
    damper    = ac.StructItem.array(ac.StructItem.float(), 4),
  }), BUFFER)
})

local car = ac.getCar(0)
local elapsed = 0
local getDamper = physics.getExtendedDamperTravel

function script.update(dt)
  elapsed = elapsed + dt
  if not car or not getDamper then return end

  shared.running = 1

  local s = shared.samples[shared.writeIndex % BUFFER]

  -- The one physics-rate channel, and the whole reason this worker exists.
  -- Damper travel rather than suspension travel: it is the damper's own
  -- displacement, so differentiating it gives damper velocity without
  -- needing the car's motion ratio. Everything else below is context, so
  -- the app can place the sample on a lap and on the track.
  --
  -- Hoisted to a local: this runs 333 times a second on the physics
  -- thread, where slow code degrades the whole simulation, not just us.
  local d = s.damper
  d[0] = getDamper(0, 0) or 0
  d[1] = getDamper(0, 1) or 0
  d[2] = getDamper(0, 2) or 0
  d[3] = getDamper(0, 3) or 0

  -- ac.getCar() inside a worker still updates at render rate. Fine for
  -- these: spline and brake move slowly next to a damper, and they are only
  -- used to place the sample and to infer the compression sign.
  s.t_ms      = math.floor(elapsed * 1000)
  s.spline    = car.splinePosition or 0
  s.brake     = car.brake or 0
  s.speed_kmh = car.speedKmh or 0

  shared.writeIndex = shared.writeIndex + 1
end
