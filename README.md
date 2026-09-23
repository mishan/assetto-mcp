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
  is a standard MCP server and doesn't care which one you use. The repo also
  ships a race-engineer skill, which clients that read skill folders pick up
  (see [Starting a session](#starting-a-session)).
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

## Starting a session

The repo ships a **race-engineer skill** in `.claude/skills/race-engineer/`.
It is the session routine an engineer would otherwise have to be
told every time: what to check before the first flying lap, which tool
answers which question, how to read the numbers, how to run an A/B, and the
handoff document at the end. It is plain Markdown: a client that reads
skill folders, such as Claude Code opened in this folder, loads it by
itself; any client that can take instructions from a file can be pointed at
`.claude/skills/race-engineer/SKILL.md`. Once loaded, it is used whenever
you talk about laps, a setup, tyres or fuel, so you never have to name it:

> *"I'm at Sebring in the NSX. Start a session."*

### Live or debrief

It asks which you want, and assumes debrief if you don't say.

- **Debrief — feedback after the session.** It records and reads every lap
  and says nothing until you come in or ask. Then you get one report: the
  session lap by lap, the incidents and what caused them, what each setup
  change did, recommendations with the test that would settle each, fuel and
  tyre numbers, and the line map. It is written to `exports/`, so you can
  read it whenever you like. Pick this if you just want to drive.
- **Live — on the radio.** A short read in the chat after every lap, and
  anything you have to act on this lap or next — box, fuel is at two laps,
  a car close behind, a change confirmed — **spoken aloud** over the game
  audio. Live needs a client that can run shell commands in the background
  on the PC that runs Assetto Corsa: it wakes on each lap with
  `scripts/watch_laps.py` and speaks through `scripts/say.py` and the voices
  Windows already has.

Recording is the same in both modes, so you can switch at any point, and a
question asked in the middle of a debrief gets a live answer.

**Without the skill** you still get every tool; you ask for each step
yourself, and the steps below are what to ask for. Without background shell
commands there is no lap watcher or voice, so it's debrief only.

## Step by step

This is what the skill does for you, and what to ask for without it. Recording starts by itself and waits for the game, so
there is nothing to remember to switch on.

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

Corners come back numbered — **T1, T2, T3** in track order — so you and the
assistant can name the same one. Ask for `track_corners` to see where each
turn is. The numbering is what your car actually drove, not the circuit's
official one: a kink taken flat carries too little load to be detected and
isn't in it, and a circuit that calls a corner 3A is numbered straight
through.

Places come back the way you'd say them too: *"T4 braking zone, 90 m before
turn-in"*, *"T1, 70 m after turn-in"*, with meters from the start/finish
line beside each one and each turn's braking point given as meters before
turn-in, to line up with the boards.

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
It also asks whether you got more *consistent*, which a setup that stops you
spinning can do without moving your average pace at all — but that question
needs about six laps a side before any answer to it can count.

**7. Ask where you actually drove.**

> *"Where was my line different between those two laps?"*

Or ask to see it:

> *"Draw me a map of this session's laps."*

You get a single HTML file to open in your browser. It has every lap drawn
where you actually drove, your best line picked out, and the laps coloured
by setup if you want to see an A/B. Pick any one lap to see it by speed,
pedal, gear, or where the front or the rear was sliding. There's also a bump
map of the circuit. Charts under the map show that lap's tyre temperatures,
revs, ride height and more, and marks show where you ran wide or locked a
wheel. Pick an opponent to see where they braked and got back on the throttle
compared with you. Turns are numbered the same way the assistant numbers them. The file
lives in `exports/` in the data folder and loads nothing from the internet.

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
- **Or spoken, in live mode.** `python scripts/say.py "Box this lap"` reads
  a sentence aloud over the game audio with the voices Windows already has
  (from Windows or WSL), for the calls that can't wait for you to look at a
  screen. Nothing to install.

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
| [.claude/skills/race-engineer/](.claude/skills/race-engineer/SKILL.md) | The race-engineer skill: session routine, tool reference, how to read the numbers |
| [docs/INSTALL.md](docs/INSTALL.md) | Manual install, config file locations, environment variables, upgrading from `ac-race-engineer` |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | When your client shows no tools, when laps stop landing, and the rest |
| [docs/SETUP-RANGES.md](docs/SETUP-RANGES.md) | How setup values are clamped, why AC's spinner isn't a grid |
| [docs/INTERNALS.md](docs/INTERNALS.md) | Corner detection, data-quality flags, driving line, suspension capture tiers |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Running the tests, CI, schema changes |
| [BACKLOG.md](BACKLOG.md) | Open work, in the order worth doing, with where it bit |

---

## Planned improvements

- **Read lap validity from the game.** Track limits are now scored from
  recorded wheels-off evidence rather than guessed at, and no lap is dropped
  from a comparison for it — but it's still inference. CSP will hand the
  game's own verdict to a physics worker, which is a thing this already runs,
  so this is wiring rather than research. Single-player only.
- **Read the game's own ABS and TC flags.** The shared-memory fields that look
  like intervention are constant all lap, so they're a setting, not activity.
  CSP has the real flags, but only for a physics worker — single-player only.
- **Fill in the display registry.** It no longer guesses what your setup
  screen shows — it says "unknown" and asks — but it only knows a car once
  you've read a couple of values off the screen for it.
- **Per-track corner names and the circuit's own numbering.** Corners are
  numbered now, but from what the car drove — so advice reads "T3" rather
  than "the corner at 0.34", and not yet "T3 / Variante", nor T3A where the
  circuit has one.

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
