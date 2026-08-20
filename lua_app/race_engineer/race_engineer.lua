-- Race Engineer: in-game companion for the ac-race-engineer MCP server.
--
-- Talks to the localhost HTTP bridge (default port 9666):
--   * polls /status every 2s (recording state + messages from Claude)
--   * POSTs /note when a complaint tag is pressed (button or wheel binding)
--   * POSTs /rivals once a second (batched opponent telemetry)
--   * POSTs /suspension once a second (travel, loads, ride height)
--   * POSTs /setup when the setup changes (ranges and values from the game)
--
-- CSP caps Lua apps at 2 concurrent web requests. There are five things
-- that want to talk, so they take turns: sendSlots() below is the single
-- gate, and the timers are deliberately out of phase so they don't collide
-- every second frame. A note press wins any tie -- it is the one request
-- the driver is waiting on.

local BASE = 'http://127.0.0.1:9666'
local MAX_CONCURRENT_REQUESTS = 2
local POLL_INTERVAL = 2.0

-- Opponent capture. AC's shared memory is ego-only, so the server can only
-- learn about other cars from here. Sampling every frame would be wasteful
-- and would swamp the SQLite writer; 10Hz resolves a braking point to about
-- 5m at Mugello's speeds, which is finer than any decision we make from it.
local RIVAL_SAMPLE_INTERVAL = 0.1
local RIVAL_POST_INTERVAL = 1.0
-- Offset so the rival POST and the status poll don't land on the same frame.
-- Both timers start at zero and are decremented by the same dt, so without
-- this they fire together every two seconds by construction -- which is the
-- concurrency cap on its own, before the driver presses anything.
local RIVAL_POST_PHASE = 0.5
-- Two seconds of a full 64-car grid at 10Hz, so a single delayed POST does
-- not start shedding samples. Must stay under the bridge's MAX_RIVAL_BATCH.
local RIVAL_BUFFER_MAX = 1400

-- Suspension capture. Stock shared memory exposes none of this, so like
-- rival data it can only come from in here.
--
-- Two tiers, and which one we get is decided at runtime:
--
--   worker  a CSP physics worker sampling at 333Hz. Damper velocity is a
--           fast signal, and this is the only app-reachable way to see it
--           without aliasing. CSP gates physics scripting, so this may not
--           be available.
--   app     this script, at render rate. Fine for ride height and wheel
--           loads, which move slowly. Not fine for damper valving, and the
--           server labels it accordingly rather than quietly pretending.
local SUSP_SAMPLE_INTERVAL = 1 / 60      -- app tier; no point beating frames
local SUSP_POST_INTERVAL = 1.0
-- Distinct from both the poll (period 2.0, fires on even seconds) and the
-- rival post (period 1.0, phase 0.5). 1.5 would have looked staggered and
-- collided with rivals on every single second: 1.5 mod 1.0 == 0.5.
local SUSP_POST_PHASE = 0.25
-- Setup values only change in the pits. Slow poll, and the fingerprint
-- check below means an unchanged setup never hits the network.
local SETUP_POLL_INTERVAL = 3.0
local SUSP_BUFFER_MAX = 1800             -- under the bridge's cap, ~5s at 333Hz
local WORKER_RING = 1024                 -- must match suspension_worker.lua

local car = ac.getCar(0)

local status = {
  connected = false,
  running = false,
  text = 'connecting...',
  laps = 0,
  sessionId = nil,   -- server's session; a change resets the setup feed
}
local rivalBuffer = {}
local rivalMeta = {}       -- [car_index] = per-car fields, not per-sample
local rivalSampleTimer = 0
local rivalPostTimer = RIVAL_POST_PHASE
local rivalBusy = false
local rivalDropped = 0
local rivalSent = 0
local message = nil        -- { id, text } from Claude, shown until dismissed
local ackedId = 0          -- last message id we dismissed (poll may race ack)
local pollTimer = 0
local pollBusy = false
local sendBusy = false
local noteQueue = {}
local toast, toastTimer = nil, 0

-- Two buffers, not one. The tiers are different channels on different
-- clocks: the worker reads damper travel from the physics API at 333Hz,
-- while the render-rate sampler reads suspension travel, wheel loads and
-- ride height from ac.getCar(). Posting them under a single label let a
-- histogram be built across the seam between two zero points, which
-- manufactures a high-speed tail out of nothing -- and reads exactly like
-- valving that packs down over kerbs.
local suspAppBuffer = {}
local suspWorkerBuffer = {}
local suspSampleTimer = 0
local suspPostTimer = SUSP_POST_PHASE
local suspBusy = false
local suspDropped = 0
local suspSent = 0
local suspNote = 'starting'  -- shown in the app window
-- Set when the worker was deliberately not attempted because we are online.
-- Kept separate from suspNote so the window can render it as information
-- rather than as a degraded state: nothing is wrong, this is the rule.
local onlineSuppressed = false
-- Setup snapshot state. setupSent holds the fingerprint the server has
-- accepted, so an unchanged setup costs nothing and a rejected post retries.
local setupBusy = false
local setupSent = nil
local setupNote = ''
local setupTimer = 0
local worker = nil           -- shared struct, once connected
local workerReadIndex = 0
local workerProducing = false -- only true once samples have actually arrived

-- Complaint tags. Each gets a clickable button and a bindable control
-- (Settings window) so they can go on wheel buttons.
local TAGS = {
  { tag = 'understeer', label = 'Understeer' },
  { tag = 'oversteer',  label = 'Oversteer'  },
  { tag = 'braking',    label = 'Braking'    },
  { tag = 'traction',   label = 'Traction'   },
}
for _, t in ipairs(TAGS) do
  t.btn = ac.ControlButton('__EXT_RACE_ENGINEER_' .. t.tag:upper())
end

-- The bridge rejects out-of-range values (they would never correlate with a
-- corner downstream), so clamp here rather than lose a note to a 400. Spline
-- can read slightly outside 0..1 around the start/finish line, and these
-- fields are nil before the session is fully loaded.
local function num(v, fallback)
  if type(v) ~= 'number' or v ~= v then return fallback end  -- v ~= v -> NaN
  return v
end

local function clamp(v, lo, hi)
  if v < lo then return lo end
  if v > hi then return hi end
  return v
end

-- What a bridge reply actually means, decided in one place.
--
-- Three of these handlers were written separately and three of them got the
-- same thing wrong: a 200 carrying {ok=false} means the server had nowhere
-- to file the data -- no session recording -- and treating it as delivered
-- shows a healthy status line over data that went nowhere. HTTP said yes;
-- the body said no.
--
-- Returns one of:
--   'offline'  no reply at all: the bridge is down or the request failed
--   'rejected' an HTTP error: the request was wrong, log it
--   'refused'  200, but the server declined to store it (ok=false)
--   'ok'       stored; second return is the parsed body
local function classifyReply(err, response)
  if err or not response or not response.status then return 'offline' end
  if response.status ~= 200 then return 'rejected' end
  local parsed_ok, parsed = pcall(JSON.parse, response.body or '')
  if not parsed_ok or type(parsed) ~= 'table' then
    -- A 200 we cannot read is not evidence of anything.
    return 'offline'
  end
  if parsed.ok == false then return 'refused', parsed end
  return 'ok', parsed
end

-- How many of CSP's concurrent web requests are still free. Notes ignore
-- this and always send: the driver is waiting on that one, and it is a few
-- hundred bytes.
local function sendSlots()
  local used = 0
  if pollBusy then used = used + 1 end
  if sendBusy then used = used + 1 end
  if rivalBusy then used = used + 1 end
  if suspBusy then used = used + 1 end
  if setupBusy then used = used + 1 end
  return MAX_CONCURRENT_REQUESTS - used
end

local function enqueueNote(tag)
  if #noteQueue >= 200 then
    toast, toastTimer = 'note LOST (bridge down, queue full)', 3
    return
  end
  noteQueue[#noteQueue + 1] = JSON.stringify{
    tag = tag,
    spline = clamp(num(car.splinePosition, 0), 0, 1),
    lap_count = clamp(math.floor(num(car.lapCount, 0)), 0, 100000),
    speed_kmh = clamp(num(car.speedKmh, 0), 0, 1000),
  }
  toast, toastTimer = tag:upper() .. ' noted', 1.5
end

-- Declared before pumpNotes so both the enqueue and the retry path share it.

-- A note the bridge never received must go back on the queue. The bridge
-- now refuses to share its port, so an ordinary server restart leaves it
-- unbindable for as long as Windows holds the old socket -- minutes, during
-- which every complaint tag the driver pressed used to be dropped on the
-- floor behind a two-second toast.
local NOTE_QUEUE_MAX = 200

local function pumpNotes()
  if sendBusy or #noteQueue == 0 then return end
  sendBusy = true
  local body = table.remove(noteQueue, 1)
  web.post(BASE .. '/note', { ['Content-Type'] = 'application/json' }, body,
    function(err, response)
      sendBusy = false
      if err or not response then
        -- Transport failure: the server never saw it. Put it back at the
        -- front so notes keep their order, and let it retry.
        if #noteQueue < NOTE_QUEUE_MAX then
          table.insert(noteQueue, 1, body)
          toast, toastTimer = 'bridge down - note queued', 2
        else
          toast, toastTimer = 'note LOST (queue full)', 3
        end
      elseif response.status ~= 200 then
        -- Don't let a rejected note look like a recorded one: the "noted"
        -- toast already fired at enqueue time.
        toast, toastTimer = 'note REJECTED (' .. tostring(response.status) .. ')', 3
        ac.warn('race_engineer: /note rejected: ' .. tostring(response.body))
      else
        -- Stored, but with no session to attach it to. Say so: an orphaned
        -- note won't show up against this run's telemetry.
        local ok, parsed = pcall(JSON.parse, response.body or '')
        if ok and type(parsed) == 'table' and parsed.orphaned then
          toast, toastTimer = 'noted, but NOT RECORDING', 3
        end
      end
    end)
end

-- ---------------------------------------------------------------- suspension

-- Try for the 333Hz physics worker, and be explicit when we don't get it.
--
-- Everything here is defensive on purpose: physics scripting is gated by
-- CSP and by the track, the two getters are undocumented, and older CSP
-- builds may not have startPhysicsWorker at all. A missing capability must
-- degrade to render-rate sampling, never take the app down -- losing the
-- complaint tags because a damper channel was unavailable would be a poor
-- trade.
-- Everything we know about why a worker might be refused, gathered once and
-- logged together. Each of these has been a plausible cause at some point
-- and none of them is visible from the server side, so guessing from a bare
-- "cannot start" costs a round trip to the driver every time.
-- Every line of this is a guess about an undocumented API, so every line
-- gets its own pcall. ac.getSim() hands back a proxy that RAISES on an
-- unknown field rather than returning nil, so one wrong field name here
-- takes down the whole probe -- which is precisely what happened when this
-- was written as a straight sequence of reads: the diagnostic threw, the
-- caller's pcall swallowed it, and the worker was never even attempted.
-- A diagnostic that can break the thing it inspects is worse than none.
local function ask(label, fn)
  local ok, v = pcall(fn)
  ac.log('  ' .. label .. ': ' ..
    (ok and (type(v) .. ' ' .. tostring(v)) or ('unavailable (' ..
      tostring(v) .. ')')))
end

local function logWorkerEnvironment()
  ac.log('race_engineer: physics worker environment')
  ask('csp version', function() return ac.getPatchVersion() end)
  ask('csp version code', function() return ac.getPatchVersionCode() end)
  ask('physics table', function() return type(physics) end)
  ask('startPhysicsWorker', function()
    return type(physics.startPhysicsWorker) end)
  ask('getExtendedDamperTravel', function()
    return type(physics.getExtendedDamperTravel) end)
  ask('physics.allowed()', function() return physics.allowed() end)
  -- Online is the gate we most suspect: CSP disables physics scripting in
  -- multiplayer, since a script on the physics thread is a cheat vector.
  ask('online race', function() return ac.getSim().isOnlineRace end)
  ask('session type', function() return ac.getSim().raceSessionType end)
  -- 'extended physics' used to be read here as sim.isNewBehaviorActive.
  -- That field does not exist -- checked against the CSP SDK definitions,
  -- ac.StateSim has isOnlineRace but nothing of that name -- and reading it
  -- raised, which took the whole probe down with it. physics.allowed()
  -- above is the real answer to the same question.
end

-- true / false / nil, where nil means we could not tell. Guarded because
-- ac.getSim() raises on unknown fields and this app has already been broken
-- once by assuming a field name exists.
local function isOnlineSession()
  local ok, v = pcall(function() return ac.getSim().isOnlineRace end)
  if not ok or v == nil then return nil end
  return v and true or false
end

local function startSuspensionWorker()
  -- Belt and braces: even with every read individually guarded, the
  -- diagnostic must never be the reason the worker doesn't start.
  pcall(logWorkerEnvironment)

  -- CSP refuses to run a script on the physics thread in multiplayer, and
  -- it is right to: that thread decides what the car does, so a script on
  -- it is a cheat vector. Detect the case and say so plainly instead of
  -- reporting "Physics API not available", which reads as something being
  -- broken when nothing is. Only an explicit true suppresses it -- if the
  -- field is missing we still try, because being unable to tell is not a
  -- reason to disable the feature.
  --
  -- Render-rate capture continues either way: ride height, wheel loads and
  -- roll balance never needed the worker, and those work online.
  if isOnlineSession() == true then
    suspNote = 'multiplayer: damper capture is single-player only'
    onlineSuppressed = true
    ac.log('race_engineer: online session, not starting physics worker '
      .. '(CSP disallows physics scripting in multiplayer). Ride height '
      .. 'and load transfer still recording at render rate.')
    return
  end

  if type(physics) ~= 'table' or type(physics.startPhysicsWorker) ~= 'function' then
    suspNote = 'CSP too old for physics workers - render rate only'
    return
  end

  -- physics.allowed() reports whether this session permits physics
  -- scripting at all. When it is false the docs say only raycasting works,
  -- so there is no point starting a worker that can't read anything.
  --
  -- Deliberately only bails on an explicit false: older builds return nil
  -- here, and treating nil as "not allowed" would disable the worker on
  -- setups where it would have worked. The cost is that a session which
  -- refuses workers without saying so reaches startPhysicsWorker and fails
  -- there instead -- which is why that error is now kept and logged.
  if type(physics.allowed) == 'function' then
    local ok, allowed = pcall(physics.allowed)
    if ok and allowed == false then
      suspNote = 'physics scripting not allowed here - render rate only'
      return
    end
  end

  -- Probe the exact getter the worker will call, not a neighbouring one.
  -- If it returns nothing useful, a worker would faithfully record zeroes
  -- at 333Hz -- worse than honest render-rate data, because it looks
  -- precise.
  if type(physics.getExtendedDamperTravel) ~= 'function' then
    suspNote = 'damper channel unavailable - render rate only'
    return
  end
  local ok, v = pcall(physics.getExtendedDamperTravel, 0, 0)
  if not ok or type(v) ~= 'number' then
    suspNote = 'damper channel unavailable - render rate only'
    return
  end

  worker = ac.connect({
    ac.StructItem.key('ac_race_engineer.suspension'),
    writeIndex = ac.StructItem.int32(),
    running    = ac.StructItem.int32(),
    samples    = ac.StructItem.array(ac.StructItem.struct({
      t_ms      = ac.StructItem.int32(),
      spline    = ac.StructItem.float(),
      brake     = ac.StructItem.float(),
      speed_kmh = ac.StructItem.float(),
      damper    = ac.StructItem.array(ac.StructItem.float(), 4),
    }), WORKER_RING)
  })
  worker.writeIndex = 0
  worker.running = 0
  workerReadIndex = 0

  -- Keep pcall's second return: it is the only description of why the call
  -- failed, and discarding it left the app saying "cannot start physics
  -- worker" with no way to tell a missing script file from a wrong
  -- signature from a session that forbids workers.
  -- Signature is startPhysicsWorker(script, data, callback): the second
  -- argument is data handed to the worker as `__input`, not a car index as
  -- this code originally assumed. The 0 is harmless -- the worker ignores
  -- it -- but the assumption was wrong and worth not passing on.
  local started, startErr = pcall(physics.startPhysicsWorker,
    'suspension_worker', 0,
    function(err)
      if err then
        worker, workerProducing = nil, false
        suspNote = 'worker stopped: ' .. tostring(err)
        ac.warn('race_engineer: physics worker stopped: ' .. tostring(err))
      end
    end)
  if not started then
    worker = nil
    suspNote = 'worker start failed: ' .. tostring(startErr)
    ac.warn('race_engineer: startPhysicsWorker failed: ' .. tostring(startErr))
    return
  end
  -- Deliberately not claiming the worker tier yet. startPhysicsWorker
  -- returning without throwing only means the call was accepted; it says
  -- nothing about whether the script loaded or is producing. The tier is
  -- promoted in drainWorker() once samples actually arrive -- otherwise a
  -- worker that never runs would have its render-rate fallback labelled
  -- 333Hz, which is the one thing this whole design is trying to prevent.
  suspNote = 'physics worker starting...'
end

-- Drain whatever the worker has written since we last looked.
-- `keep` false means catch up without collecting: used while not recording,
-- so the index stays in step instead of accumulating a phantom drop count
-- for every sample produced while the driver sat in the garage.
local function drainWorker(keep)
  if not worker then return end
  if worker.running == 0 then return end        -- not producing yet

  local write = worker.writeIndex

  -- writeIndex only ever climbs, so a value below our read index means it
  -- wrapped int32 (~74 days at 333Hz) or the worker restarted and reset it.
  -- Resync rather than sit in a loop that can never make progress again.
  if write < workerReadIndex then
    workerReadIndex = write
    return
  end

  -- Lapped the ring: the oldest unread samples are already overwritten.
  -- Count them rather than pretending the trace is continuous.
  if write - workerReadIndex > WORKER_RING then
    if keep then
      suspDropped = suspDropped + (write - workerReadIndex - WORKER_RING)
    end
    workerReadIndex = write - WORKER_RING
  end

  if not keep then
    workerReadIndex = write
    return
  end

  if write > workerReadIndex and not workerProducing then
    workerProducing = true
    suspNote = 'physics worker: 333Hz damper data'
  end

  local lap = clamp(math.floor(num(car.lapCount, 0)), 0, 100000)
  while workerReadIndex < write do
    if #suspWorkerBuffer >= SUSP_BUFFER_MAX then
      suspDropped = suspDropped + (write - workerReadIndex)
      workerReadIndex = write
      break
    end
    local s = worker.samples[workerReadIndex % WORKER_RING]
    local d = s.damper
    suspWorkerBuffer[#suspWorkerBuffer + 1] = {
      lap_count = lap,
      t_ms = s.t_ms,
      spline = clamp(num(s.spline, 0), 0, 1),
      brake = clamp(num(s.brake, 0), 0, 1),
      speed_kmh = clamp(num(s.speed_kmh, 0), 0, 1000),
      travel_fl = num(d[0], nil), travel_fr = num(d[1], nil),
      travel_rl = num(d[2], nil), travel_rr = num(d[3], nil),
    }
    workerReadIndex = workerReadIndex + 1
  end
end

-- Render-rate sampling. Used as the only source when the worker is
-- unavailable, and alongside it for the channels the worker cannot see:
-- wheel loads and ride height live on ac.getCar(), not in the physics API.
local function sampleSuspension()
  if not car then return end
  if #suspAppBuffer >= SUSP_BUFFER_MAX then
    suspDropped = suspDropped + 1
    return
  end

  local row = {
    lap_count = clamp(math.floor(num(car.lapCount, 0)), 0, 100000),
    -- car.timestamp is AC's physics clock. Using it rather than a wall
    -- clock means a dropped frame shows up as a longer interval instead of
    -- a phantom velocity spike, and duplicate frames are detectable.
    t_ms = math.floor(num(car.timestamp, 0)),
    spline = clamp(num(car.splinePosition, 0), 0, 1),
    brake = clamp(num(car.brake, 0), 0, 1),
    speed_kmh = clamp(num(car.speedKmh, 0), 0, 1000),
  }

  local wheels = car.wheels
  if wheels then
    local names = { 'fl', 'fr', 'rl', 'rr' }
    for i = 0, 3 do
      local w = wheels[i]
      if w then
        row['travel_' .. names[i + 1]] = num(w.suspensionTravel, nil)
        -- load is documented as unreliable for remote cars and replays;
        -- this is our own car, so it is the right field here.
        row['load_' .. names[i + 1]] = num(w.load, nil)
      end
    end
  end

  -- rideHeight is a 2-element array: front, rear. There is no per-corner
  -- ride height in the app-side API.
  local rh = car.rideHeight
  if rh then
    row.ride_f = num(rh[0], nil)
    row.ride_r = num(rh[1], nil)
  end
  -- AC's own measure of the floor scraping, 0..1.
  row.plank_wear = num(car.maxRelativePlankWear, nil)

  -- When the worker is supplying travel, these rows exist for the channels
  -- the physics API cannot see. Sending suspension travel alongside damper
  -- travel under two different source labels is fine -- the server keeps
  -- them apart -- but it doubles the volume for a channel we already have
  -- at 333Hz, so leave it out.
  if workerProducing then
    row.travel_fl, row.travel_fr = nil, nil
    row.travel_rl, row.travel_rr = nil, nil
  end

  suspAppBuffer[#suspAppBuffer + 1] = row
end

-- One POST per tier. They are different channels on different clocks, and
-- the server stores `source` per row so the analysis can keep them apart.
local function postSuspensionBuffer(which, buffer)
  if #buffer == 0 then return false end
  suspBusy = true
  local batch = buffer
  web.post(BASE .. '/suspension', { ['Content-Type'] = 'application/json' },
    JSON.stringify{ source = which, samples = batch },
    function(err, response)
      suspBusy = false
      if err or not response or response.status ~= 200 then
        suspDropped = suspDropped + #batch
        if response and response.status and response.status ~= 200 then
          ac.warn('race_engineer: /suspension rejected: '
            .. tostring(response.status) .. ' ' .. tostring(response.body))
        end
        return
      end
      local ok, parsed = pcall(JSON.parse, response.body or '')
      if not ok or type(parsed) ~= 'table' then
        -- A 200 we cannot read is not evidence anything was stored.
        suspDropped = suspDropped + #batch
        return
      end
      -- A 200 with ok=false means the server had nowhere to file this --
      -- no session was recording. Nothing was stored, so counting it as
      -- sent would show a healthy status line while the data went nowhere.
      if parsed.ok == false then
        suspDropped = suspDropped + #batch
        suspNote = 'server not recording - suspension not stored'
        return
      end
      local stored = tonumber(parsed.stored) or 0
      suspSent = suspSent + stored
      if #batch > stored then
        suspDropped = suspDropped + (#batch - stored)
      end
      -- The server nulls out-of-range fields and says which. Almost always
      -- a units disagreement, and silent nulls would surface much later as
      -- an empty report with no explanation.
      if parsed.rejected_fields then
        ac.warn('race_engineer: /suspension rejected fields: '
          .. tostring(response.body))
      end
    end)
  return true
end

-- Which tier gets first refusal this time round. Flipped on every post.
local suspWorkerFirst = true

local function postSuspension()
  if suspBusy or sendSlots() < 1 then return end
  if not status.running then
    suspAppBuffer, suspWorkerBuffer = {}, {}
    return
  end

  -- This used to be `if worker then ... elseif app then ...`, which the
  -- comment called alternating but which is strict priority. The worker
  -- fills at 333Hz and is drained once a second, so its buffer is
  -- effectively never empty -- the app branch never ran. Ride height and
  -- wheel loads come only from the app tier, so the moment a physics
  -- worker started, the two channels that answer "is the car too low" and
  -- "which axle takes the load transfer" silently stopped arriving, while
  -- the status line happily reported a healthy 333Hz feed.
  --
  -- Alternate for real: each tier gets every other slot, and only falls
  -- through to the other when its own buffer is empty.
  local function flush(which)
    local buf = (which == 'worker') and suspWorkerBuffer or suspAppBuffer
    if #buf == 0 then return false end
    if which == 'worker' then suspWorkerBuffer = {} else suspAppBuffer = {} end
    return postSuspensionBuffer(which, buf)
  end

  local first = suspWorkerFirst and 'worker' or 'app'
  local second = suspWorkerFirst and 'app' or 'worker'
  suspWorkerFirst = not suspWorkerFirst
  if not flush(first) then flush(second) end
end

-- Snapshot every car on track. Fields that AC doesn't transmit for remote
-- cars are sent as nil rather than 0: the server distinguishes "absent" from
-- "driver wasn't braking", and silently coercing to 0 would destroy that.
local function sampleRivals()
  local n = ac.getSim().carsCount
  if not n or n < 2 then return end
  for i = 0, n - 1 do
    if i ~= 0 then
      local c = ac.getCar(i)
      -- Cars that have left, not yet spawned, or are in the pits contribute
      -- nothing useful to a speed trace.
      if c and c.isConnected and not c.isInPitlane then
        if #rivalBuffer >= RIVAL_BUFFER_MAX then
          rivalDropped = rivalDropped + 1
        else
          -- Per-sample fields only. Driver name, car model and lap times
          -- describe the car, not the moment, and the server collapses them
          -- to one row per car anyway -- stamping them onto all ten samples
          -- a second doubled the JSON we serialize on the render thread.
          -- They ride along on the first sample of each car per batch,
          -- below.
          rivalBuffer[#rivalBuffer + 1] = {
            car_index = i,
            lap_count = clamp(math.floor(num(c.lapCount, 0)), 0, 100000),
            spline = clamp(num(c.splinePosition, 0), 0, 1),
            speed_kmh = clamp(num(c.speedKmh, 0), 0, 1000),
            gear = num(c.gear, nil),
            gas = num(c.gas, nil),
            brake = num(c.brake, nil),
          }
          if not rivalMeta[i] then
            rivalMeta[i] = {
              driver_name = ac.getDriverName(i) or '',
              car_model = c.carId or '',
            }
          end
          -- Lap times change as the session runs, so refresh them each time
          -- rather than freezing the first value seen.
          rivalMeta[i].best_lap_ms = num(c.bestLapTimeMs, nil)
          rivalMeta[i].last_lap_ms = num(c.previousLapTimeMs, nil)
          rivalMeta[i].lap_count = clamp(math.floor(num(c.lapCount, 0)), 0, 100000)
        end
      end
    end
  end
end

local function postRivals()
  if rivalBusy or #rivalBuffer == 0 or sendSlots() < 1 then return end
  -- Only ship telemetry the server can file against a session.
  if not status.running then
    rivalBuffer = {}
    rivalMeta = {}
    return
  end
  rivalBusy = true
  local batch = rivalBuffer
  local meta = rivalMeta
  rivalBuffer = {}
  rivalMeta = {}
  -- Fold each car's metadata onto its first sample in this batch, so the
  -- server still gets it without it riding on every sample.
  local seen = {}
  for _, s in ipairs(batch) do
    local m = meta[s.car_index]
    if m and not seen[s.car_index] then
      seen[s.car_index] = true
      s.driver_name = m.driver_name
      s.car_model = m.car_model
      s.best_lap_ms = m.best_lap_ms
      s.last_lap_ms = m.last_lap_ms
    end
  end
  web.post(BASE .. '/rivals', { ['Content-Type'] = 'application/json' },
    JSON.stringify{ cars = batch },
    function(err, response)
      rivalBusy = false
      if err or not response then
        rivalDropped = rivalDropped + #batch
        return
      end
      if response.status ~= 200 then
        rivalDropped = rivalDropped + #batch
        ac.warn('race_engineer: /rivals rejected: '
          .. tostring(response.status) .. ' ' .. tostring(response.body))
        return
      end
      local ok, parsed = pcall(JSON.parse, response.body or '')
      if not ok or type(parsed) ~= 'table' then
        -- A 200 we cannot read is not evidence anything was stored.
        rivalDropped = rivalDropped + #batch
        return
      end
      -- A 200 with ok=false means the server had nowhere to file it -- no
      -- session was recording -- which is not the same as having stored it.
      -- Counting it as sent shows a healthy status line over lost data.
      if parsed.ok == false then
        rivalDropped = rivalDropped + #batch
        return
      end
      local stored = tonumber(parsed.stored) or 0
      rivalSent = rivalSent + stored
      if #batch > stored then
        rivalDropped = rivalDropped + (#batch - stored)
      end
    end)
end

-- What the game says about the setup menu.
--
-- ac.getSetupSpinners() reports every adjustable entry keyed by the same
-- section names the setup files use, with its legal min/max/step and the
-- two display conventions (displayMultiplier, showClicksMode). That is the
-- car's own answer to questions this project previously answered by
-- unpacking data.acd and comparing saved files against the setup screen.
--
-- Sent only when something changes: the values are static while driving,
-- and the point of resampling at all is to catch a setup swapped in the
-- pits mid-session.
local function readSetupSpinners()
  if type(ac.getSetupSpinners) ~= 'function' then return nil end
  local ok, list = pcall(ac.getSetupSpinners)
  if not ok or type(list) ~= 'table' then return nil end
  local out = {}
  for _, s in ipairs(list) do
    if type(s) == 'table' and s.name then
      out[#out + 1] = {
        name = tostring(s.name),
        label = s.label and tostring(s.label) or '',
        min = num(s.min, nil), max = num(s.max, nil),
        step = num(s.step, nil), value = num(s.value, nil),
        displayMultiplier = num(s.displayMultiplier, nil),
        showClicksMode = num(s.showClicksMode, nil),
        units = s.units and tostring(s.units) or '',
        readOnly = s.readOnly and true or false,
      }
    end
  end
  return out
end

-- Everything needed to work out fuel per lap, read from the game rather
-- than looked up.
--
-- KM_PER_LITER lives in the car's fuel_cons.ini inside data.acd. That file
-- is encrypted for paid mods, and getting at it previously meant decrypting
-- the archive by hand -- which worked once, for one car, and produced a
-- number nobody could check. ac.INIConfig.carData() reads it directly:
-- CSP already holds the key.
--
-- trackLengthM comes from the AI spline, so it is the length the game
-- itself believes in rather than whatever a layout is nominally called.
-- Between them the server computes liters per lap for any car and track
-- without being told anything.
local function readFuelBasis()
  local out = {}
  pcall(function()
    local m = ac.getSim().trackLengthM
    if type(m) == 'number' and m > 0 then out.track_length_m = m end
  end)
  pcall(function()
    local ini = ac.INIConfig.carData(0, 'fuel_cons.ini')
    local v = ini and ini:get('FUEL_EVAL', 'KM_PER_LITER', 0)
    if type(v) == 'table' then v = v[1] end
    v = tonumber(v)
    if v and v > 0 then out.km_per_liter = v end
  end)
  return out
end

local function setupFingerprint(spinners)
  local parts = {}
  for _, s in ipairs(spinners) do
    parts[#parts + 1] = s.name .. '=' .. tostring(s.value)
  end
  table.sort(parts)
  return table.concat(parts, ';')
end

local function postSetup()
  if setupBusy or sendSlots() < 1 or not status.running then return end
  local spinners = readSetupSpinners()
  if not spinners or #spinners == 0 then return end

  local fp = setupFingerprint(spinners)
  if fp == setupSent then return end        -- nothing moved

  local state, reason = '', ''
  if type(ac.getCarSetupState) == 'function' then
    local ok, s, r = pcall(ac.getCarSetupState)
    if ok then state, reason = tostring(s or ''), tostring(r or '') end
  end

  local fuel = readFuelBasis()

  setupBusy = true
  web.post(BASE .. '/setup', { ['Content-Type'] = 'application/json' },
    JSON.stringify{ car = ac.getCarID and ac.getCarID(0) or '',
                    spinners = spinners, state = state, reason = reason,
                    track_length_m = fuel.track_length_m,
                    km_per_liter = fuel.km_per_liter },
    function(err, response)
      setupBusy = false
      local outcome = classifyReply(err, response)
      if outcome == 'rejected' then
        ac.warn('race_engineer: /setup rejected: '
          .. tostring(response.status) .. ' ' .. tostring(response.body))
      end
      if outcome ~= 'ok' then
        -- Not stored. Leaving setupSent alone is what makes the next tick
        -- retry; latching it here on a refusal was how a whole session
        -- could end up with no setup values while the app looked fine.
        setupNote = outcome == 'refused' and 'setup: not recording'
          or 'setup: bridge unreachable'
        return
      end
      setupSent = fp
      setupNote = state ~= '' and ('setup: ' .. state)
        or ('setup: ' .. #spinners .. ' values')
    end)
end

local function poll()
  if pollBusy then return end
  pollBusy = true
  web.get(BASE .. '/status', function(err, response)
    pollBusy = false
    if err or not response or response.status ~= 200 then
      status.connected = false
      status.text = 'bridge offline'
      return
    end
    local d = JSON.parse(response.body)
    if not d then return end
    status.connected = true
    status.running = d.running or false
    status.text = d.status or ''
    status.laps = d.laps_recorded or 0

    -- Setup values are stored per session on the server, so a new session
    -- starts with none. The fingerprint only re-posts when the *values*
    -- change, and restarting a session with the same setup loaded -- the
    -- ordinary tuning loop -- changes nothing. Without this the new session
    -- never receives a setup, and identify_setup answers "no live setup
    -- values; is the in-game app running?", which is a wrong diagnosis of a
    -- working app.
    if d.session_id and d.session_id ~= status.sessionId then
      status.sessionId = d.session_id
      setupSent = nil
    end
    if d.message and d.message.id ~= ackedId then
      message = d.message
    elseif not d.message then
      message = nil
    end
  end)
end

local function dismissMessage()
  if not message then return end
  ackedId = message.id
  web.post(BASE .. '/ack', { ['Content-Type'] = 'application/json' },
    JSON.stringify{ id = message.id }, function(err, response) end)
  message = nil
end

-- Probe once, on the first frame rather than at load time: the sim isn't
-- fully up when the app script is first evaluated, and physics.allowed()
-- answers for the session we're actually in.
local started = false

function script.update(dt)
  if not started then
    started = true
    local ok, err = pcall(startSuspensionWorker)
    if not ok then
      worker, workerProducing = nil, false
      suspNote = 'render rate only (worker probe failed)'
      ac.warn('race_engineer: suspension worker probe failed: '
        .. tostring(err))
    end
  end

  for _, t in ipairs(TAGS) do
    if t.btn:pressed() then enqueueNote(t.tag) end
  end
  pumpNotes()

  pollTimer = pollTimer - dt
  if pollTimer <= 0 then
    pollTimer = POLL_INTERVAL
    poll()
  end

  rivalSampleTimer = rivalSampleTimer - dt
  if rivalSampleTimer <= 0 then
    rivalSampleTimer = RIVAL_SAMPLE_INTERVAL
    if status.running then sampleRivals() end
  end

  rivalPostTimer = rivalPostTimer - dt
  if rivalPostTimer <= 0 then
    rivalPostTimer = RIVAL_POST_INTERVAL
    postRivals()
  end

  -- Suspension. The worker (if we have one) is doing the fast sampling on
  -- the physics thread; here we only move its output into the send buffer.
  -- Wheel loads and ride height are not in the physics API, so the
  -- render-rate sampler runs either way -- just at a lower rate when the
  -- worker is supplying travel.
  -- Keep the read index in step even while stopped, discarding as we go.
  -- Otherwise ten minutes in the garage charges 200,000 samples to the
  -- drop counter on the next green flag, none of which were lost.
  drainWorker(status.running)

  if status.running then
    suspSampleTimer = suspSampleTimer - dt
    if suspSampleTimer <= 0 then
      -- Once the worker is supplying travel, these rows only carry wheel
      -- loads and ride height, which move with the body rather than the
      -- dampers -- no reason to sample them every frame.
      suspSampleTimer = workerProducing and 0.1 or SUSP_SAMPLE_INTERVAL
      sampleSuspension()
    end
  end

  suspPostTimer = suspPostTimer - dt
  if suspPostTimer <= 0 then
    suspPostTimer = SUSP_POST_INTERVAL
    postSuspension()
  end

  -- Setup values only move in the pits, so a slow poll is plenty; the
  -- fingerprint check means an unchanged setup costs one table build and
  -- no request.
  setupTimer = setupTimer - dt
  if setupTimer <= 0 then
    setupTimer = SETUP_POLL_INTERVAL
    postSetup()
  end

  if toastTimer > 0 then toastTimer = toastTimer - dt end
end

-- Exposed for the test harness only. Everything in this file is `local`,
-- which is right for a CSP app -- but a local function cannot be called
-- from outside, so the scheduling and gating logic had no way to be tested
-- and a starvation bug lived in it undetected while the status line
-- reported success. CSP never reads this table.
-- Hung off CSP's own `script` table rather than declared as a global: the
-- app forbids implicit globals and there is a test that enforces it.
script.__test = {
  postSuspension = function() return postSuspension() end,
  startSuspensionWorker = function() return startSuspensionWorker() end,
  isOnlineSession = function() return isOnlineSession() end,
  clamp = function(...) return clamp(...) end,
  num = function(...) return num(...) end,
  state = function()
    return {
      suspNote = suspNote,
      onlineSuppressed = onlineSuppressed,
      workerProducing = workerProducing,
      appBuffered = #suspAppBuffer,
      workerBuffered = #suspWorkerBuffer,
    }
  end,
  push = function(which, n)
    local buf = (which == 'worker') and suspWorkerBuffer or suspAppBuffer
    for _ = 1, n do buf[#buf + 1] = { t_ms = 0 } end
  end,
  setRunning = function(v) status.running = v end,
  -- The real busy flag is cleared by the web callback, which a test
  -- harness has no way to invoke.
  clearBusy = function() suspBusy = false end,
}

function windowMain(dt)
  -- status line
  if not status.connected then
    ui.textColored('● bridge offline', rgbm(1, 0.3, 0.3, 1))
  elseif status.running then
    ui.textColored('● recording', rgbm(0.3, 1, 0.3, 1))
    ui.sameLine()
    ui.text(string.format('%d laps stored', status.laps))
    if rivalSent > 0 or rivalDropped > 0 then
      ui.textColored(string.format('rivals: %d sent%s', rivalSent,
        rivalDropped > 0 and (', ' .. rivalDropped .. ' dropped') or ''),
        rgbm(0.7, 0.7, 0.7, 1))
    end
    if suspSent > 0 or suspDropped > 0 then
      ui.textColored(string.format('suspension: %d sent%s', suspSent,
        suspDropped > 0 and (', ' .. suspDropped .. ' dropped') or ''),
        rgbm(0.7, 0.7, 0.7, 1))
    end
  else
    ui.textColored('● connected, not recording', rgbm(1, 0.8, 0.2, 1))
  end
  ui.textColored(status.text, rgbm(0.7, 0.7, 0.7, 1))

  -- Which suspension tier we got. Worth showing plainly: it decides
  -- whether the damper histograms mean anything, and the driver is the
  -- only one who can see this.
  -- workerProducing, not a separate tier variable: it only goes true once
  -- samples have actually been drained, so the marker cannot claim 333Hz
  -- for a worker that was started but never produced anything.
  -- Three states, not two. Amber is "you wanted the worker and did not get
  -- it"; online is neither that nor success, so it gets its own neutral
  -- marker. Coloring an expected, correct condition as a warning trains
  -- the driver to ignore the line that matters.
  local marker, color
  if workerProducing then
    marker, color = '◆ ', rgbm(0.4, 0.9, 0.5, 1)
  elseif onlineSuppressed then
    marker, color = '○ ', rgbm(0.6, 0.7, 0.8, 1)
  else
    marker, color = '◇ ', rgbm(0.8, 0.7, 0.4, 1)
  end
  ui.textColored(marker .. suspNote, color)
  -- The setup feed is what makes lap attribution and clamping work, and
  -- the driver is the only one who can see whether it is running. It was
  -- being written and never shown.
  if setupNote ~= '' then
    ui.textColored(setupNote, rgbm(0.7, 0.7, 0.7, 1))
  end
  ui.separator()

  -- complaint buttons
  local w = ui.availableSpaceX()
  for _, t in ipairs(TAGS) do
    if ui.button(t.label, vec2(w, 30)) then enqueueNote(t.tag) end
  end

  -- toast confirmation
  if toastTimer > 0 and toast then
    ui.textColored(toast, rgbm(0.4, 0.9, 1, 1))
  else
    ui.dummy(vec2(1, ui.textLineHeight()))
  end

  -- message from Claude
  if message then
    ui.separator()
    ui.textColored('Claude:', rgbm(0.8, 0.6, 1, 1))
    ui.textWrapped(message.text)
    if ui.button('OK##dismiss', vec2(w, 24)) then dismissMessage() end
  end
end

function windowSettings(dt)
  ui.text('Bind complaint tags to wheel/keyboard:')
  ui.separator()
  for _, t in ipairs(TAGS) do
    ui.text(t.label)
    ui.sameLine(120)
    t.btn:control(vec2(180, 0))
  end
  ui.separator()
  ui.textWrapped('Bridge: ' .. BASE ..
    '  (ac-race-engineer server must be running)')
end
