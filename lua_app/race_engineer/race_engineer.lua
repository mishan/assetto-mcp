-- Race Engineer: in-game companion for the ac-race-engineer MCP server.
--
-- Talks to the localhost HTTP bridge (default port 9666):
--   * polls /status every 2s (recording state + messages from Claude)
--   * POSTs /note when a complaint tag is pressed (button or wheel binding)
--
-- CSP caps Lua apps at 2 concurrent web requests, so notes go through a
-- queue pumped one at a time alongside the status poll.

local BASE = 'http://127.0.0.1:9666'
local POLL_INTERVAL = 2.0

local car = ac.getCar(0)

local status = {
  connected = false,
  running = false,
  text = 'connecting...',
  laps = 0,
}
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

local function enqueueNote(tag)
  noteQueue[#noteQueue + 1] = JSON.stringify{
    tag = tag,
    spline = clamp(num(car.splinePosition, 0), 0, 1),
    lap_count = clamp(math.floor(num(car.lapCount, 0)), 0, 100000),
    speed_kmh = clamp(num(car.speedKmh, 0), 0, 1000),
  }
  toast, toastTimer = tag:upper() .. ' noted', 1.5
end

local function pumpNotes()
  if sendBusy or #noteQueue == 0 then return end
  sendBusy = true
  local body = table.remove(noteQueue, 1)
  web.post(BASE .. '/note', { ['Content-Type'] = 'application/json' }, body,
    function(err, response)
      sendBusy = false
      if err or not response then
        toast, toastTimer = 'send failed', 2
      elseif response.status ~= 200 then
        -- Don't let a rejected note look like a recorded one: the "noted"
        -- toast already fired at enqueue time.
        toast, toastTimer = 'note REJECTED (' .. tostring(response.status) .. ')', 3
        ac.warn('race_engineer: /note rejected: ' .. tostring(response.body))
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
