# Changelog

What changed in each release of assetto-mcp. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/): until 1.0, tools, the database
and the setup files may still change between minor versions.

## [Unreleased]

## [0.2.0] - 2026-10-24

The first tagged release, covering everything since the project began.

### Recording

- Every lap is recorded from Assetto Corsa's shared memory as soon as the
  server starts, including out-laps and the lap that ended in the barrier.
  Each lap is stored with facts about it (out-lap, pitted, off track, scrappy)
  so a lap whose time isn't a lap time can't skew the best-lap numbers.
- An in-game app (Custom Shaders Patch) adds what shared memory lacks:
  opponents' positions and names, the setup on the car, suspension travel,
  and four complaint buttons (understeer, oversteer, braking, traction) that
  mark the moment on track. It also shows the engineer's messages in the
  game.
- Only one copy of the server records, however many chats are open, and a
  recording survives the chat restarting the server.
- The database is kept to a bounded size by thinning old samples; no lap is
  ever deleted.

### Analysis

- Corners are found from the driving itself and named the way the circuit
  names them, with places given in meters and turns rather than fractions of
  a lap.
- Per corner: brake point, entry, minimum and exit speed, balance
  (understeer or oversteer), and how much time it's worth against another lap.
- Time gained and lost against any other lap, by position on track, including
  an opponent's lap timed from their own trace.
- Tyre pressures, temperatures and wear; braking, lockups and body slip;
  damper histograms, ride height and roll balance.
- Contacts inferred from acceleration and opponents' positions, for servers
  that run with damage off.
- A setup change is judged against the driver's own lap-to-lap spread, so a
  good lap isn't mistaken for a better car, and every A/B goes in a setup book
  that plans the next test.
- Fuel per lap measured from the game, and whether the fuel on board reaches
  the flag; position, lap N of M and laps left in a race.

### Setups

- Revised setups are written as files that appear in the in-game setup menu,
  with values snapped to what each setting can actually reach.
- Every lap is stamped with the setup the car was measured on.

### The pit wall

- A live dashboard in the browser: lap time and gap to your best lap,
  position, fuel to the flag, tyres, and the driving-line map, where the
  engineer can put laps, zoom to a turn and add a caption. Click the map and
  type a question, and the engineer gets it.
- On a phone or tablet over the local network, at a secret link with a QR
  code.
- A stream overlay for OBS, with a track map of every car.
- `assetto-mcp-pit-wall` runs the pit wall and the recorder without a chat.
- The lap clock stops when the driver takes the checkered flag.

### The race engineer

- A skill that turns the assistant into a race engineer: the session routine,
  which tool answers which question, how to read the numbers and run an A/B,
  with a spoken channel for live sessions.
- The engineer can restart the server itself, and check for and apply
  updates when the driver says so.

### Install

- A Windows installer that sets up the server, the in-game app and Claude
  Desktop, and a diagnostic report for when something doesn't work.

[Unreleased]: https://github.com/mishan/assetto-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/mishan/assetto-mcp/releases/tag/v0.2.0
