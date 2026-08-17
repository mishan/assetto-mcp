# ac-race-engineer

MCP server that turns Claude into a race engineer for original Assetto Corsa.
It reads AC's shared memory telemetry, stores laps in SQLite, reduces them to
engineer-grade summaries, and can read/write setup files that appear directly
in the in-game setup menu.

Runs on the Windows machine running Assetto Corsa. Original AC only (uses the
`acpmf_*` shared memory layout, not ACC's).

## Install

Python 3.10+ on the gaming PC:

```
cd ac-race-engineer
pip install -e .
```

## Hook up Claude Desktop

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ac-race-engineer": {
      "command": "python",
      "args": ["-m", "ac_race_engineer.server"]
    }
  }
}
```

(If you use Claude Code instead:
`claude mcp add ac-race-engineer -- python -m ac_race_engineer.server`)

Optional environment overrides:

- `AC_DOCS_DIR` — AC documents folder (default `~/Documents/Assetto Corsa`)
- `AC_ENGINEER_DATA` — DB + ranges location (default `~/.ac-race-engineer`)

## The tuning loop

1. Start AC, get on track.
2. Tell Claude: "start recording and confirm you can see the session"
   (`start_recording`, `live_snapshot`).
3. Drive 3–5 laps. Laps store automatically as they complete; off-track
   excursions (>2 tyres out) mark a lap invalid.
4. "Summarize my last lap and read my current setup"
   (`list_laps`, `lap_summary`, `read_setup`). The summary includes per-corner
   min speed, brake points, tyre pressures/temps, and a slip-balance metric
   (positive = understeer, negative = oversteer).
5. Discuss what the car is doing; Claude writes a revised setup with
   `write_setup` (e.g. as `claude_v1`).
6. Pit, load `claude_v1` from the setup screen, drive again.
7. "Compare my best lap on the new setup against lap N" (`compare_laps`) —
   corner-by-corner min speed and brake point deltas show whether the change
   actually worked.

## Setup value clamping (recommended)

AC **silently ignores** setup values outside the ranges defined in the car's
`setup.ini` (inside `data.acd`). To let the server clamp and snap values to
each car's legal min/max/step:

1. In Content Manager: car page → unpack data (or use QuickBMS).
2. Copy the car's `setup.ini` to
   `%USERPROFILE%\.ac-race-engineer\ranges\<car_folder_name>.ini`
   e.g. `ranges\ks_mazda_mx5_cup.ini`.

Without a ranges file, writes still work but come back with a warning, and you
should sanity-check the values in the setup screen.

## In-game app (CSP Lua)

`lua_app/race_engineer/` is an in-game companion app. Copy the folder to
`assettocorsa/apps/lua/race_engineer/` (requires Custom Shaders Patch; you
already have it if you use Content Manager with CSP enabled). Enable it from
the in-game apps sidebar.

What it does:

- **Complaint tags while driving** — Understeer / Oversteer / Braking /
  Traction buttons, each bindable to a wheel button via the app's Settings
  window (they show up as CSP control bindings). Pressing one records your
  exact spline position, lap, and speed. Claude reads them with
  `get_driver_notes` and correlates them with corner telemetry: "you flagged
  understeer twice at spline 0.34 — that's the corner where front slip
  exceeds rear by 0.09".
- **Status overlay** — recording indicator + laps stored, so you never
  alt-tab to check.
- **Messages from Claude** — `send_driver_message` puts a note on the
  overlay ("claude_v2 saved — pit and load it"); dismiss with OK.

The app talks to the server's HTTP bridge on `127.0.0.1:9666` (change with
`AC_ENGINEER_BRIDGE_PORT`, and edit `BASE` in the Lua to match). The bridge
binds localhost only.

## Layout

```
ac_race_engineer/
  sim_info.py   shared memory structs (physics / graphics / static)
  collector.py  background sampler -> SQLite, lap boundary detection
  db.py         schema + storage
  analysis.py   corner detection, lap summaries, lap comparison
  setups.py     setup INI read/write, range clamping
  bridge.py     localhost HTTP bridge for the in-game app
  server.py     MCP tools
lua_app/
  race_engineer/  CSP Lua in-game app (copy to apps/lua/)
```

## Notes / future ideas

- Sampling is 25Hz — plenty for setup work while keeping the DB tiny.
  Bump `TARGET_HZ` in collector.py if you want finer traces.
- Out-laps (no valid time) are skipped automatically.
- Corner detection is generic (speed minima); a per-track corner-name map
  would make Claude's advice read nicer ("T3/Variante" vs "corner at 0.34").
- CSP/Lua could expose damper velocities and more channels than stock shared
  memory if you ever want deeper suspension work — Telemetrick's source is a
  good reference.
