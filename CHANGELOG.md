# Changelog

What changed in each release of Telemetrist, formerly assetto-mcp. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/): until 1.0, tools, the database
and the setup files may still change between minor versions.

## [Unreleased]

## [0.3.0] - 2026-11-05

assetto-mcp is now Telemetrist, and it reads Automobilista 2 as well as
Assetto Corsa. An install that updates itself keeps working under the old
names; re-run `install-windows.bat` to move Claude Desktop's entry to the
new one. Restart a pit wall or an older server left running across the
update: until then it serves no pit wall, overlay or line map.

### Added

- Automobilista 2, with `TELEMETRIST_SIM=ams2`: laps, the out-lap, corners,
  tyres, fuel and track limits from the game's shared memory, and its
  recordings replay like AC's. Opponents and setup files are not there yet.
- Assetto Corsa laps record each tyre's tread temperature, inner, middle and
  outer, and the camber each wheel runs at. Lap summaries give the spread
  across each tyre and the camber in a straight line and when loaded, and
  `lap_trace` can plot them.
- After an update, the servers in Claude Desktop's other chats restart onto
  the new code by themselves once the game is off track. Another chat's
  session holds an update back only while the game is on track.
- `session_check` and `apply_update` say when a Claude Desktop config
  still has a pre-rename entry, and that re-running the installer renames it.

### Changed

- The project is now Telemetrist: the package, commands (`telemetrist`,
  `telemetrist-update`, `telemetrist-pit-wall`), `TELEMETRIST_*` settings,
  `~/.telemetrist`, the in-game app and the GitHub repository. The old names
  keep working: `-m assetto_mcp.server` and the `assetto-mcp` commands,
  `ASSETTO_MCP_*` settings, and `~/.assetto-mcp`, which is moved at the
  first start that finds nothing holding it open. See [upgrading](docs/INSTALL.md#upgrading-from-assetto-mcp-or-ac-race-engineer).
- The Windows installer keeps the settings on an existing Claude Desktop
  entry (`env` and anything besides the command), from whichever config
  holds it, instead of writing a bare entry over them.
- A lap left mid-way for the pits (AC's Return to Pits) or the garage
  (AMS2) is kept, marked unfinished, as a crash already was, and so is the
  lap so far when the driver quits to the menus or restarts. They used to
  be lost, or a pit return joined onto the next out-lap.

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
  updates when the driver says so. Updates follow tested releases by
  default, or each change as soon as it's finished, if the driver asks.

### Install

- A Windows installer that sets up the server, the in-game app and Claude
  Desktop, and a diagnostic report for when something doesn't work.

[Unreleased]: https://github.com/mishan/telemetrist/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/mishan/telemetrist/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/mishan/telemetrist/releases/tag/v0.2.0
