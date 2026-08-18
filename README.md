# ac-race-engineer

MCP server that turns Claude into a race engineer for original Assetto Corsa.
It reads AC's shared memory telemetry, stores laps in SQLite, reduces them to
engineer-grade summaries, and can read/write setup files that appear directly
in the in-game setup menu.

Runs on the Windows machine running Assetto Corsa. Original AC only (uses the
`acpmf_*` shared memory layout, not ACC's).

## Install (Windows — the easy way)

**Prerequisite:** Python 3.10+ from [python.org](https://www.python.org/downloads/).
On the installer's first screen tick **"Add python.exe to PATH"**. Don't use
the Microsoft Store build — its sandboxing breaks shared-memory access.

Then, in this folder: **right-click `install-windows.bat` → Open** (or just
double-click it).

That's it. The installer finds Python, installs the package, writes the Claude
Desktop config (merging with any MCP servers you already have, and taking a
backup first), locates your Assetto Corsa install, and copies the in-game Lua
app into place. Re-run it any time after a `git pull`; it updates in place.

If it can't find Assetto Corsa, tell it where to look. Flags go on the `.bat`,
not the `.ps1` — Windows blocks `.ps1` files from running directly under the
default execution policy, and the `.bat` exists to work around exactly that:

```
install-windows.bat -AcPath "D:\Games\steamapps\common\assettocorsa"
```

Other flags: `-SkipLuaApp`, `-Uninstall`.

**After installing, fully quit Claude Desktop and reopen it.** Closing the
window isn't enough — right-click the Claude icon in the system tray (bottom
right, possibly hidden under the `^` arrow) and choose Quit.

## Install (manual)

```powershell
python -m pip install -e .
```

Then add the server to `claude_desktop_config.json`. The fastest way to open
that file is **Claude Desktop → Settings → Developer → Edit Config**, which
creates it if missing and opens the containing folder — no filesystem
archaeology required.

<details>
<summary>Where that file actually lives, and why <code>%APPDATA%</code> may not work for you</summary>

`%APPDATA%` is an *environment variable*, not a literal path. It expands to
`C:\Users\<you>\AppData\Roaming` — and `AppData` is a **hidden** folder, so
browsing to it in Explorer shows nothing unless you enable View → Hidden items.

The bigger gotcha: `%VAR%` is **cmd.exe syntax**. Windows Terminal defaults to
**PowerShell**, where the same variable is spelled `$env:VAR`. So:

| Where | What to type |
|---|---|
| PowerShell / Windows Terminal | `notepad $env:APPDATA\Claude\claude_desktop_config.json` |
| cmd.exe | `notepad %APPDATA%\Claude\claude_desktop_config.json` |
| Explorer address bar | `%APPDATA%\Claude` *(Explorer expands it too)* |
| Win+R (Run dialog) | `%APPDATA%\Claude` |

To open the folder rather than the file, use `explorer $env:APPDATA\Claude`
in PowerShell.
</details>

<details>
<summary><strong>On a packaged Claude Desktop install, that path is a lie</strong></summary>

Claude Desktop for Windows is commonly an **MSIX package** — including the
build you download straight from Anthropic's site, not just the Microsoft Store
one. MSIX *can* give a package a private, redirected view of `%APPDATA%`
(whether it does depends on how the package was built, so treat this as "check
both" rather than a rule). When redirection is in play the app writes and
reads:

```
%LOCALAPPDATA%\Packages\Claude_<hash>\LocalCache\Roaming\Claude\claude_desktop_config.json
```

Once a file exists in that redirect layer it **shadows** the real
`%APPDATA%\Claude` copy. So you can edit
`%APPDATA%\Claude\claude_desktop_config.json` all day, get valid JSON and a
correct Python path, and the app will still show no tools — it never opens that
file. The `logs` folder moves with it, too, so a missing
`%APPDATA%\Claude\logs` is the tell.

To find yours:

```powershell
Get-ChildItem "$env:LOCALAPPDATA\Packages\Claude*" -Directory |
  ForEach-Object { Join-Path $_.FullName 'LocalCache\Roaming\Claude' }
```

`install-windows.bat` finds every config location, picks the one the running
Claude Desktop actually reads, writes the entry **there only**, and removes any
stale `ac-race-engineer` entry from the others. That last part matters: a config
in two places means two Claude surfaces each launching their own copy of this
server, and only one of them can hold the bridge port. It tells you which one it
chose and why.

The safest manual route is **Claude Desktop → Settings → Developer → Edit
Config**, which always opens the file the running app actually reads.
</details>

```json
{
  "mcpServers": {
    "ac-race-engineer": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": ["-m", "ac_race_engineer.server"]
    }
  }
}
```

Use the **absolute path** to `python.exe`, not bare `"python"`. Claude Desktop
launches MCP servers without your shell's `PATH`, so a bare command frequently
fails silently. Get the correct path with:

```powershell
py -c "import sys; print(sys.executable)"
```

Don't use `(Get-Command python).Source` — on stock Windows that often returns
`...\WindowsApps\python.exe`, the Microsoft Store alias stub, which is the
wrong answer. Remember JSON needs backslashes doubled (`\\`).

(If you use Claude Code instead:
`claude mcp add ac-race-engineer -- python -m ac_race_engineer.server`)

Optional environment overrides:

- `AC_DOCS_DIR` — AC documents folder (default `~/Documents/Assetto Corsa`)
- `AC_ENGINEER_DATA` — DB + ranges location (default `~/.ac-race-engineer`)
- `AC_ENGINEER_BRIDGE_PORT` — in-game app bridge port (default `9666`)

## Troubleshooting

**Claude doesn't list the tools.** Confirm you fully quit and reopened Claude
Desktop (tray icon → Quit). Then read the server log:

```powershell
notepad $env:APPDATA\Claude\logs\mcp-server-ac-race-engineer.log
```

**...and there is no `logs` folder there.** That means Claude Desktop is the
MSIX-packaged build (the normal case, whatever you downloaded) and is reading a
different config entirely — see the MSIX note in the install section above. Re-run
`install-windows.bat`. Both the config and the logs live under:

```powershell
Get-ChildItem "$env:LOCALAPPDATA\Packages\Claude*\LocalCache\Roaming\Claude" -Recurse -Filter 'mcp-server-*.log'
```

**Two Pythons.** `pip install -e .` records the pointer to this repo in *one*
interpreter's `site-packages`. If `command` in the config names a different
Python, `-m ac_race_engineer.server` dies with `ModuleNotFoundError` and Claude
shows nothing rather than an error. Check with:

```powershell
& "<the command path from your config>" -c "import ac_race_engineer; print('ok')"
```

`diagnose.bat` in this folder checks all of the above — every config location,
JSON validity, BOM, every Python on the box, who owns bridge port 9666, and a
cold-start of the server on a scratch port — and writes `diagnose-report.txt`.

Other MCP servers' secrets are redacted from that report on a best-effort
basis (`env` values become `<redacted>`, token-shaped strings are masked), but
it still contains your username and absolute paths, and redaction is pattern
matching rather than a guarantee. **Skim it before sharing it.**

**Typing `python` opens the Microsoft Store** (or says "not recognized").
Windows ships an app-execution-alias stub at
`%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` that hijacks the name when no
real Python is on `PATH`. Install Python from python.org with "Add python.exe to
PATH" ticked, or use the `py` launcher (`py -3 -m pip install -e .`) — the
python.org installer sets that up by default. You can also kill the stub in
Settings → Apps → Advanced app settings → App execution aliases.

The installer already ignores anything under `WindowsApps`, so this only bites
you on a manual install.

**`.ps1 cannot be loaded because running scripts is disabled`.** That's the
execution policy. Use `install-windows.bat` instead — it bypasses the policy
for that one script without changing any system setting.

**Setup values don't stick in-game.** You're writing outside the car's legal
range and AC is silently ignoring them — see the next section.

## The tuning loop

1. Start AC, get on track.
2. Tell Claude: "start recording and confirm you can see the session"
   (`start_recording`, `live_snapshot`).
3. Drive 3–5 laps. Laps store automatically as they complete. A lap is marked
   invalid — still stored and readable, just excluded from best-lap maths — if
   it had an off-track excursion (>2 tyres out), included a pit visit, or came
   in grossly slower than the session's reference (25s, or 25% for longer
   tracks). That last rule is why a 10:22 "lap" no longer becomes your
   session best.
4. "Summarize my last lap and read my current setup"
   (`list_laps`, `lap_summary`, `read_setup`). The summary includes per-corner
   min speed, brake points, tyre pressures/temps, and a slip-balance metric
   (positive = understeer, negative = oversteer).
5. Discuss what the car is doing; Claude writes a revised setup with
   `write_setup` (e.g. as `claude_v1`).
6. Pit, load `claude_v1` from the setup screen, and **tell Claude you've loaded
   it** (`set_session_setup`). Nothing in shared memory reveals the loaded
   setup, so this is the only way it can be recorded. Laps from this point are
   tagged `claude_v1`; laps already stored keep the setup they were driven on,
   so the baseline stays the baseline.
7. Drive again, then "compare my best lap on the new setup against lap N"
   (`compare_laps`) — corner-by-corner min speed and brake point deltas show
   whether the change actually worked. `lap_summary` reports each lap's setup.

Two things worth knowing:

- **Complaint tags pressed while nothing is recording are still saved**, but
  with no session attached — they'd otherwise be guessed onto whatever session
  ran last, which could be a different circuit. `get_driver_notes` says how
  many are orphaned; pass `all_sessions=True` to see them.
- **If `lap_summary` reports `slip_quality`**, some telemetry ticks were
  discarded as glitched (AC occasionally emits a wheelSlip in the tens of
  thousands). It tells you how many corners were affected and how big the
  worst spike was, so you can judge whether the balance number is trustworthy.

## Setup value clamping (recommended)

AC **silently ignores** setup values outside the ranges defined in the car's
`setup.ini` (inside `data.acd`). To let the server clamp and snap values to
each car's legal min/max/step:

1. In Content Manager: car page → unpack data (or use QuickBMS).
2. Copy the car's `setup.ini` into the ranges folder, named after the car's
   folder name — e.g. `ks_mazda_mx5_cup.ini`.

To open that folder (the installer already created it):

```powershell
explorer $env:USERPROFILE\.ac-race-engineer\ranges
```

(cmd.exe: `explorer %USERPROFILE%\.ac-race-engineer\ranges`)

Without a ranges file, writes still work but come back with a warning, and you
should sanity-check the values in the setup screen.

## In-game app (CSP Lua)

`install-windows.bat` copies this in for you. To do it by hand, copy
`lua_app/race_engineer/` to `assettocorsa/apps/lua/race_engineer/` (requires
Custom Shaders Patch; you already have it if you use Content Manager with CSP
enabled). Enable it from the in-game apps sidebar — move the mouse to the right
edge of the screen while in a session.

Not sure where Assetto Corsa is installed? In Steam, right-click Assetto Corsa
→ **Manage → Browse local files**.

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
install-windows.ps1  one-shot Windows installer
install-windows.bat  double-clickable wrapper for the above
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
