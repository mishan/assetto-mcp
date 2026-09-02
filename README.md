# assetto-mcp

Turn an AI assistant into a race engineer for Assetto Corsa.

Drive a few laps, then ask it what the car is doing. It reads AC's live
telemetry, stores every lap, reduces them to the numbers an engineer actually
reasons about — corner minimum speeds, brake points, tyre pressures and temps,
understeer/oversteer balance, damper behaviour — and writes revised setups that
appear straight in your in-game setup menu.

Then you drive again, and it tells you whether the change actually worked or
whether you just had a good lap.

> Runs on the Windows PC that runs Assetto Corsa. **Original AC only** — it uses
> the `acpmf_*` shared memory layout, not ACC's.

---

## What you need

- **Assetto Corsa** on Windows, with [Custom Shaders Patch][csp] (you already
  have it if you use Content Manager with CSP enabled)
- **An MCP client** — anything that can launch a local stdio MCP server:
  Claude Desktop, Claude Code, ChatGPT Desktop, Cursor, Windsurf, VS Code with
  Copilot, or [LM Studio][lms] if you'd rather run the model locally too. This
  is a standard MCP server and doesn't care which one you use.
- **Python 3.10 or newer** from [python.org][py] — on the installer's first
  screen, tick **"Add python.exe to PATH"**. Don't use the Microsoft Store
  build; its sandboxing breaks shared-memory access.

[csp]: https://acstuff.ru/patch/
[py]: https://www.python.org/downloads/
[lms]: https://lmstudio.ai/docs/app/mcp

## Install

In this folder: **right-click `install-windows.bat` → Open** (or just
double-click it).

The installer finds Python, installs the package, locates your Assetto Corsa
install, copies the in-game app into place, and registers the server with
**Claude Desktop** — merging with any MCP servers you already have, and taking
a backup first. Re-run it any time after a `git pull`; it updates in place.

Then **fully quit your client and reopen it.** For Claude Desktop, closing the
window isn't enough — right-click the tray icon (bottom right, possibly hidden
under the `^` arrow) and choose Quit.

**Using a different client?** Add `-SkipClientConfig`. Everything else installs
the same way, and the installer prints the two lines you need to paste into
your client's config:

```
install-windows.bat -SkipClientConfig
```

[docs/INSTALL.md](docs/INSTALL.md) has the config snippet for each client.

Other flags: `-AcPath "D:\Games\steamapps\common\assettocorsa"` if it can't
find the game, `-SkipLuaApp`, `-Uninstall`.

*Prefer to do the whole thing by hand? Also [docs/INSTALL.md](docs/INSTALL.md).
Something went wrong? Run `diagnose.bat`, then see
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).*

---

## Using it

Recording starts by itself and waits for the game, so there is nothing to
remember to switch on.

**1. Get on track and check the assistant can see you.**

> *"Confirm you can see my session."*

**2. Drive 3–5 laps.** They store as they complete — every lap, including
out-laps and the one that ended in the barrier. Each is flagged for what it is,
so a lap whose time isn't a lap time can't poison your best-lap numbers.

**3. Ask what the car is doing.**

> *"Summarize my last lap and read my current setup."*

You'll get per-corner minimum speed and brake points, tyre pressures and
temperatures, ride height and damper behaviour, and a slip-balance number —
positive means understeer, negative means oversteer.

**4. Talk it through, and have it write a setup.**

> *"It pushes on entry at the second-to-last corner. Try something and save it
> as `claude_v1`."*

Values are clamped to what your car will actually accept, so the game can't
silently ignore them. Two things it will refuse rather than do quietly: reusing
an existing setup name, and writing at all when it has no ranges for the car —
which is what happens if the in-game app has never seen the setup screen.

It'll also tell you what each value reads as on the **setup screen**, which is
often a different number from the stored one — or say it doesn't know, rather
than guessing. Read a couple off the screen for it and it won't have to ask
again:

> *"At minimum front toe shows 0.40, at maximum -0.40."*

Details: [docs/SETUP-RANGES.md](docs/SETUP-RANGES.md).

**5. Pit, load the setup — then say you loaded it.**

> *"I've loaded claude_v1."*

This step matters. Nothing in shared memory reveals which setup is on the car,
so this is the only way laps get labelled correctly. It applies to laps from
here on; the baseline you already drove keeps whatever it had, including
nothing.

If you realise afterwards that some stored laps were on a setup nobody
recorded, say which ones — *"laps 41 to 44 were on `baseline`"*. It only fills
laps that have no setup yet, and only the ones you name.

**6. Drive again, and ask whether it worked.**

> *"Compare my best lap on the new setup against lap 14."*

Corner-by-corner deltas in minimum speed and brake point. For more than one lap
a side, ask it to compare the two runs — it judges the change against your own
lap-to-lap spread, so "faster" has to beat "you were just quicker that lap".

**7. Ask where you actually drove.**

> *"Where was my line different between those two laps?"*

### While you're driving

The in-game app (right edge of the screen → apps sidebar) gives you:

- **Four complaint buttons** — Understeer, Oversteer, Braking, Traction — each
  bindable to a wheel button in the app's Settings window. Press one the moment
  you feel it and it records exactly where on track you were. Those get
  correlated with the telemetry: *"you flagged understeer twice at the same corner —
  that's where front slip exceeds rear by 0.09."*
- **A status overlay** — recording state and laps stored, so you never alt-tab
  to check. If laps are finishing and none are landing, it says so plainly
  rather than repeating whatever the server claims.
- **Messages back from the assistant** — *"claude_v2 saved — pit and load it."*

---

## What gets logged

**Everything stays on your machine.** It's a SQLite file at
`~/.assetto-mcp/telemetry.db`, the in-game app talks to the server over
localhost only, and nothing is uploaded anywhere.

While recording, at 25 Hz:

- **Car state** — speed, throttle, brake, steering, gear, RPM, lateral and
  longitudinal acceleration
- **Tyres** — per-wheel slip, pressure, core temperature and wear
- **Chassis** — front and rear ride height, world position, heading, pitch and
  roll, how many tyres are off track, and bodywork damage
- **Electronics** — whether TC and ABS are actually intervening
- **From the in-game app** — suspension travel, wheel loads and plank wear, at
  up to 333 Hz. Only the last 20 laps of a session are kept; these are by far
  the biggest rows in the database.

**Every lap is kept**, including the ones that don't count: out-laps, laps
with a pit stop, laps that ran wide, and laps that ended in the barrier. Each
is flagged for what it is rather than thrown away — a lap that ran wide still
has real corner speeds and brake points on it, and the lap that ended in the
wall is often the one you most want to look at. What the flags do is stop a
lap whose *time* isn't a lap time from being ranked or averaged.

Per session it also stores the car, track and layout, tyre compound, air and
road temperature, track length, the car's fuel consumption and tank size, the
setup name you gave it, and a copy of every setup value currently on the car —
that copy is what lets it work out later which setup a lap was driven on. Per
lap: the time, and whether it counted.

Two things worth knowing:

- **Complaint tags** record which button you pressed, where on track, on which
  lap, and at what speed.
- **Other cars on track are logged too** — car index, driver name, car model,
  lap times, and speed/gear/throttle/brake traces — because that's what makes
  "compare me to the car ahead" work. This covers AI opponents as well as
  multiplayer, so in an online session other people's names end up in your
  local database.

Setup files are read from and written to your normal Assetto Corsa setup
folder. Only files whose name you ask for are written, and reusing the name of
an existing setup is refused unless you say to overwrite it — in which case the
old file is kept alongside as `<name>.ini.bak-<timestamp>`.

### How big it gets

About **0.4 MB per lap**, so roughly **12 MB per hour** of driving.

**No lap is ever deleted**, and neither is anything derived from one — lap
times, setups and the off-track evidence stay exactly as recorded. When the
database passes its size budget (2 GB by default), the *oldest* sessions'
traces are thinned instead: every 2nd sample, then 4th, and so on. An old lap
gets coarser, never absent.

Two things that *are* dropped outright, from older sessions only: the traces of
other cars on track, and the high-rate suspension samples. Both answer
questions about the session they were recorded in and not much after it.

Because lap rows are kept forever, the budget is a target rather than a hard
ceiling — a database of nothing but fully-thinned laps still grows, slowly.
Ask for a storage report to see where you are. `ASSETTO_MCP_MAX_DB_BYTES`
changes the budget; `0` turns off budget-driven thinning entirely (the
per-session cap on suspension samples stays either way).

`diagnose.bat` writes a report that redacts other MCP servers' secrets on a
best-effort basis, but still contains your username and absolute paths. Skim it
before sharing it.

---

## Going deeper

| | |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Manual install, config file locations, environment variables, upgrading from `ac-race-engineer` |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | When your client shows no tools, when laps stop landing, and the rest |
| [docs/SETUP-RANGES.md](docs/SETUP-RANGES.md) | How setup values are clamped, why AC's spinner isn't a grid |
| [docs/INTERNALS.md](docs/INTERNALS.md) | Corner detection, data-quality flags, driving line, suspension capture tiers |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Running the tests, CI, schema changes |
| [BACKLOG.md](BACKLOG.md) | What's known to be broken, worst first, with where it bit |

---

## Planned improvements

- **Read lap validity from the game.** Track limits are now scored from
  recorded wheels-off evidence rather than guessed at, and no lap is dropped
  from a comparison for it — but it's still inference. CSP will hand the
  game's own verdict to a physics worker, which is a thing this already runs,
  so this is wiring rather than research. Single-player only.
- **Entry-phase corner metrics** — trail braking, rotation and steering between
  the brake point and the apex. That's where most spins live, and nothing
  currently measures it.
- **Detect a change in consistency**, not just a change in mean pace. A setup
  that stops you spinning may not move your average lap time at all, and the
  current statistics call that "within noise".
- **Use the channels already being recorded** — tyre wear across a stint, TC
  and ABS intervention, body roll. All logged, none read yet.
- **Fill in the display registry.** It no longer guesses what your setup
  screen shows — it says "unknown" and asks — but it only knows a car once
  you've read a couple of values off the screen for it.
- **Per-track corner names**, so advice reads "T3 / Variante" rather than
  "the corner at 0.34".

---

## Feedback and pull requests

This is built by one driver against two cars and three circuits, so the places
it's wrong are mostly the places it hasn't been. If a number disagrees with what
you feel in the car, that's worth reporting — the driver has been right about
that more often than the tooling has.

**Please open an issue** for anything that surprised you: a lap marked invalid
that shouldn't have been, a setup value that didn't stick, a car whose ranges
come out wrong, an analysis that said "within noise" about a change you're
certain you felt. Say which car and track, and include the lap numbers if you
have them.

**Pull requests are very welcome.** `python run_tests.py` needs no dependencies
and runs everywhere — no Windows and no Assetto Corsa required, because the
collector is driven through a fake shared-memory source and the in-game app runs
against a stubbed CSP API. See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md), and
[BACKLOG.md](BACKLOG.md) if you'd like somewhere to start.

Support for another MCP client is a particularly welcome PR: the server itself
is client-agnostic, and `install-claude-desktop.ps1` is the only file that knows
about a specific one.

---

## License

[MIT](LICENSE). Not affiliated with Kunos Simulazioni, Assetto Corsa, or the
Custom Shaders Patch project.
