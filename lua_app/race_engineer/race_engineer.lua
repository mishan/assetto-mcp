-- Race Engineer: in-game companion for the ac-race-engineer MCP server.
--
-- Talks to the localhost HTTP bridge (default port 9666):
--   * polls /status every 2s (recording state + messages from Claude)
--   * POSTs /note when a complaint tag is pressed (button or wheel binding)
--   * POSTs /rivals once a second (batched opponent telemetry)
--
-- CSP caps Lua apps at 2 concurrent web requests. There are three things
-- that want to talk, so they take turns: sendSlots() below is the single
-- gate, and the poll and rival timers are deliberately out of phase so they
-- don't collide every second frame. A note press wins any tie -- it is the
-- one request the driver is waiting on.

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

local car = ac.getCar(0)

local status = {
  connected = false,
  running = false,
  text = 'connecting...',
  laps = 0,
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

-- How many of CSP's concurrent web requests are still free. Notes ignore
-- this and always send: the driver is waiting on that one, and it is a few
-- hundred bytes.
local function sendSlots()
  local used = 0
  if pollBusy then used = used + 1 end
  if sendBusy then used = used + 1 end
  if rivalBusy then used = used + 1 end
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
      -- A 200 with ok=false means the server had nowhere to file it (no
      -- session recording), which is not the same as having stored it.
      local ok, parsed = pcall(JSON.parse, response.body or '')
      if ok and type(parsed) == 'table' and parsed.stored ~= nil then
        rivalSent = rivalSent + (tonumber(parsed.stored) or 0)
        local lost = #batch - (tonumber(parsed.stored) or 0)
        if lost > 0 then rivalDropped = rivalDropped + lost end
      else
        rivalSent = rivalSent + #batch
      end
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

function script.update(dt)
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

  if toastTimer > 0 then toastTimer = toastTimer - dt end
end

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
  else
    ui.textColored('● connected, not recording', rgbm(1, 0.8, 0.2, 1))
  end
  ui.textColored(status.text, rgbm(0.7, 0.7, 0.7, 1))
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
