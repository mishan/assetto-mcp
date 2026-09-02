# Internals

How the analysis actually works, and where it is honest about not knowing.
Read this when a number surprises you.

- [Recording](#recording)
- [Storage and retention](#storage-and-retention)
- [Corner detection](#corner-detection)
- [Data quality flags](#data-quality-flags)
- [Driving line](#driving-line)
- [Suspension](#suspension)
- [The in-game bridge](#the-in-game-bridge)
- [Layout](#layout)

---

## Recording

The collector starts with the server and waits for Assetto Corsa rather than
failing when it isn't there. You do not have to remember to start it, and a
server restart mid-session doesn't silently end recording — which is how
sixteen laps went missing across three evenings.

Clients often run one copy of this server per chat surface — Claude Desktop
does — so several are usually alive at once. Only one records: they contend for
a claim in the shared database, and the rest sit in **standby**, ready to take
over within seconds if the holder's process dies. `recording_status` reports
`state`
(`recording`, `waiting`, `standby`, `never_started`, `died`,
`stopped_by_request`) with a note saying what it means — read that rather
than inferring from `running`.

`stop_recording` applies to every instance and survives a restart, because
"stop recording" is an instruction about the car and not about whichever chat
you happened to type it into. `start_recording` turns it back on. Set
`ASSETTO_MCP_NO_AUTOSTART=1` to keep one instance out of it entirely.

The in-game overlay cross-checks all of this against the game's own lap
counter: if laps are finishing and none are landing, it says **NOT STORING
LAPS** rather than repeating whatever the server claims.

Sampling is 25 Hz — plenty for setup work while keeping the database tiny.
Bump `TARGET_HZ` in `collector.py` if you want finer traces.

### What a lap records

**Every lap is stored.** Out-laps, pit laps, laps that ran wide, laps
abandoned in the barrier — all of them, with their telemetry. The single
`valid` boolean that used to gate all this was wrong in both directions and
took the lap out of every analysis without saying so.

Instead a lap records facts, and the verdicts are derived:

| Field | Means |
|---|---|
| `complete` | reached the finish line |
| `out_lap` | left the pits, so `lap_time_ms` is not a lap time |
| `pitted` | pit visit during the lap, so the time is wall clock |
| `outlier` | grossly slower than the session's own reference |
| `invalid` | exceeded track limits |
| `max_tyres_out`, `excursions`, `off_track_ms` | the track-limits evidence |
| `invalid_source` | `inferred`, or `game` if it came from the game itself |
| `sample_stride` | 1 normally; higher if the trace has been thinned |

Two questions are kept apart, because conflating them is what lost data:

- **Did it run wide?** `invalid`, from the evidence. A lap that ran wide is
  still a lap — the corner speeds and brake points happened — so it is
  compared, and reported in `ran_wide`.
- **Is its time a lap time?** `lap_usability()`. An out-lap, a pit lap or an
  abandoned lap can't be ranked or averaged. Those are excluded from
  comparisons, always by name and with a reason.

### How track limits are scored

From `samples.tyres_out`, recorded at 25 Hz since the first schema version.
An episode counts when at least `TRACK_LIMITS_WHEELS` (4) are off the
surface for at least `MIN_EXCURSION_MS` (120 ms) — three consecutive samples
at 25 Hz — so a glitched tick or two isn't a cut. Duration is measured from
where the episode starts to where it *ends*, using each sample's own `t_ms`
rather than an assumed rate, because a thinned lap isn't 25 Hz any more.

A lap whose trace has been thinned is never re-scored: its evidence was
computed at full resolution and the samples behind it no longer are. So a
changed threshold does not reach those laps, and `rescore_track_limits`
reports how many it skipped for that reason rather than describing the pass
as covering everything.

The threshold was effectively 3 wheels, which is what stored a clean 2:06.769
at Sebring as invalid — that circuit's flat kerbs put three wheels over the
line routinely and the game counted the lap.

**Because the evidence is stored, the verdict is re-derivable.** Change
`TRACK_LIMITS_WHEELS` and run `rescore_track_limits`, and every lap still
holding a full-resolution trace is re-scored from its own samples. That's the point of storing
evidence rather than a decision: the v11 migration did this to the existing
database and gave back laps that had been wrongly marked, without re-driving
anything.

**It is still inference, not the game's verdict.** Vanilla AC doesn't expose
lap validity in shared memory. CSP does — `ac.onLapCompleted` hands a
physics worker the game's own `valid` and `cuts` — and that's reachable from
the worker this project already runs, but it isn't wired up yet and it would
be single-player only. See [BACKLOG.md](../BACKLOG.md) item 1.

---

## Storage and retention

A lap costs roughly **0.4 MB**, almost all of it the 25 Hz sample rows — so
about **12 MB per hour** of driving. Left alone that grows without bound in
the driver's home directory.

Deleting old sessions would be the obvious answer and the wrong one: a
reference lap from three months ago is exactly what a change gets measured
against, and coming back to a circuit after a season away is precisely when
the old run matters.

**No lap is deleted.** When the database passes its budget, the *oldest*
sessions' traces are **thinned** — decimated to every 2nd, 4th, 8th, 16th
then 32nd sample — and the stride is recorded on the lap. Lap times, setup
attribution and track-limits evidence are computed before any thinning and
are never touched, so a thinned lap loses resolution and nothing else.

The ladder has a floor, and that is why the budget is a **target, not a
guarantee**: because lap rows and their facts are kept forever, a database
of nothing but fully-thinned laps still grows, slowly. `enforce_budget`
says so when it runs out of room rather than reporting success.

Order of sacrifice, worst value per byte first:

1. **Rival samples** from older sessions — they answer "how did I compare to
   the car ahead" only while that race is the one being discussed.
2. **Suspension samples** from older sessions — already capped at 20 laps
   per session, and by far the heaviest rows at 333 Hz.
3. **Car samples**, oldest session first, one stride step at a time, so a
   session only reaches 1-in-32 once every older one is already there.

The current session is never thinned. `ASSETTO_MCP_MAX_DB_BYTES` sets the
budget (default 2 GB); `0` disables budget-driven thinning entirely, though
the per-session cap on suspension samples applies either way. A pass runs once at
session start, so the cost lands while the car is in the garage rather than
between two flying laps. `storage_report` says what's stored and what has
been thinned; `sample_stride` on a lap says by how much.

Progress during a pass is *estimated* from rows removed rather than measured
from the file. Deleted pages sit on the freelist and in the WAL until a
VACUUM, so re-measuring after each step reads "no progress" every time and
runs to the bottom of the ladder however little was needed — and VACUUMing
per step would rewrite the whole database once per rung.

---

## Corner detection

Corners are detected from **lateral g, not speed minima**. A fast sweeper
barely dents the speed trace but pulls as hard as anything on the lap, and a
speed-minimum detector excludes it by construction. Each corner reports entry,
apex, exit, peak lateral g and a turn sign.

The sign says which corners turn the same way — it is deliberately *not*
labelled left or right, because AC does not document which sign is which, and a
consistent sign is more useful than a label that is right half the time.

**`corner_detection`** appears alongside every corner list and says which
lateral-g bar produced it. It is not the same bar in every tool: a
`lap_summary` read on its own uses that lap's own cornering load, while
`compare_laps` and `compare_runs` use one shared across every lap being
compared, so that corner membership doesn't depend on how hard an individual
lap was driven. The same lap can therefore carry a different number of corners
in the two payloads, and this is how you tell why.

The shared bar reduces one-sided corners rather than eliminating them: a gently
driven lap can still genuinely fall below a bar the others set.

---

## Data quality flags

The analysis tools report when they had bad input rather than quietly
averaging it away.

- **`slip_quality`** — some telemetry ticks were discarded as glitched (AC
  occasionally emits a wheelSlip in the tens of thousands). It says how many
  corners were affected and how big the worst spike was, so you can judge
  whether the balance number is trustworthy.
- **`accel_samples_dropped`** — the same idea for the acceleration channels.
  AC sometimes emits a 10 g spike from a reset or a kerb strike, and one of
  those used to inflate `peak_lat_g` for the lap *and* the noise estimate for
  every comparison the lap took part in. Spikes are dropped rather than
  clamped — reporting the ceiling would be a claim the car pulled it — and the
  count is stated next to `samples` so you can see how much of the lap it was.
- **Orphaned complaint tags** — tags pressed while nothing is recording are
  still saved, but with no session attached. They'd otherwise be guessed onto
  whatever session ran last, which could be a different circuit.
  `get_driver_notes` says how many are orphaned; pass `all_sessions=True` to
  see them.

---

## Driving line

`driving_line` slices the lap by track position and reports, per slice, where
the car was in the world, how fast, the mean front ride height, and how much
that ride height moved within the slice — the last of which is a bump map,
since smooth tarmac holds the car at a steady height and broken tarmac doesn't.

Track position has always said where the car was *along* the lap. This says
where it was *across* it, which is the whole of what a line is, so "I took a
wider entry there" is finally something the tooling can check. Pass a second
lap for `separation_m`: how far apart the two cars were at the same point of
the circuit.

A slice the car never reached comes back null rather than interpolated — a gap
in a driving line is worth seeing, and inventing a point draws the car through
somewhere it never went. Laps recorded before schema v8 report
`has_position: false`; there is nothing to backfill from.

---

## Suspension

Stock shared memory exposes no suspension travel, no wheel load and no ride
height, so all of this comes from the in-game Lua app. Ask for
`suspension_report` after a lap, or look at the `suspension` block in
`lap_summary`.

Three questions, in the order you'd ask them:

- **Are the dampers doing the right thing?** A velocity histogram per axle,
  split bump vs rebound. Most of a lap should sit in the low-speed bins; a fat
  high-speed bump tail means the valving is packing down over kerbs.
- **Is the car running low enough, or too low?** Min/median/max ride height
  front and rear, rake, and the five places on track where it runs lowest,
  plus AC's plank wear as a bottoming indicator.
- **Which axle takes the load transfer?** The front's share of total lateral
  load transfer. Above 50% biases toward understeer, and it should agree with
  the slip-balance metric — when those two disagree, something else is going on
  and that's worth knowing.

### Two capture tiers, and why the report tells you which one it used

| Tier | Rate | Good for | Not good for |
|---|---|---|---|
| `worker` | 333 Hz | everything, including damper valving | — |
| `app` | render rate, 60–144 Hz | ride height, loads, roll balance | damper histograms |

The app tries to start a **CSP physics worker** — a script CSP runs on the
physics thread at 333 Hz — and falls back to sampling on the render thread if
physics scripting isn't available. That fallback matters: damper velocity is a
fast signal, and differentiating a 60 Hz sample of it aliases exactly the band
the valving lives in. A histogram built that way describes body motion, not
dampers. Rather than quietly present one as the other, the report labels the
tier and adds a caution when it's render-rate.

The app's own window shows which tier it got (`◆` worker, `○` online,
`◇` render-rate fallback), and `suspension_capture_status` explains it from
the server's side.

> **Damper histograms are single-player only.**
>
> **CSP does not allow scripts on the physics thread in an online session**,
> and that is the right call — the physics thread decides what the car does, so
> a script running on it is a cheat vector. In multiplayer you will get the
> `app` tier no matter what, and there is no setting that changes it.
>
> The app detects an online session and says so plainly rather than reporting a
> physics API failure, because nothing is broken: this is the rule working. Do
> damper work in a solo practice session on the same car and track, then race
> with whatever you learned.
>
> **Everything else keeps working online.** Ride height, rake, wheel loads and
> roll balance are read on the render thread and never needed the worker — and
> those are the channels that answer "which axle takes the load transfer",
> which is usually the question that matters.

### The sign convention

CSP documents neither the units nor the direction of suspension travel, and
whether a rising number means compression decides whether "add bump" or "add
rebound" is the right advice. So it isn't assumed — it's **inferred from your
data**: under braking the front suspension compresses, which is about as
dependable as vehicle dynamics gets, so the report compares where the front
axle sits on the brakes against where it sits off them. If a lap has no usable
braking, the direction is reported as unknown and the bump/rebound split is
withheld rather than guessed. `sign_convention` in the report shows the
reasoning and a confidence figure.

---

## The in-game bridge

The Lua app talks to the server over HTTP on `127.0.0.1:9666` (change with
`ASSETTO_MCP_BRIDGE_PORT`, and edit `BASE` in the Lua to match). The bridge
binds localhost only.

Only one server instance can hold the port, which is the same instance that
holds the recording claim — see [Recording](#recording). `bridge_status` says
whether this instance is the one listening.

---

## Layout

```
assetto_mcp/
  sim_info.py   shared memory structs (physics / graphics / static)
  collector.py  background sampler -> SQLite, lap boundary detection
  db.py         schema + storage
  analysis.py   corner detection, lap summaries, lap comparison
  setups.py     setup INI read/write, range clamping
  bridge.py     localhost HTTP bridge for the in-game app
  config.py     data dir + environment, including pre-rename fallbacks
  server.py     MCP tools
  suspension.py damper histograms, ride height, roll balance
lua_app/
  assetto_mcp/  CSP Lua in-game app (copy to apps/lua/)
    assetto_mcp.lua        the app itself, render thread
    suspension_worker.lua  CSP physics worker, 333Hz damper sampling
install-windows.ps1  one-shot Windows installer
install-windows.bat  double-clickable wrapper for the above
diagnose.ps1 / .bat  what-is-broken report
run_tests.py         run and summarise the suite, no dependencies
tests/               behavior-named test modules + shared harness
scripts/
  relabel_laps.py    fix laps stamped with the wrong setup name
BACKLOG.md           what is known to be broken, worst-first
```

### Setup attribution, in three pieces

Nothing in shared memory says which setup is on the car, so attribution is
always a claim rather than a measurement. The three ways to make it are
separated by how much damage a wrong one does:

- **`set_session_setup`** is forward-only. It records what's on the car now,
  and laps completed from here carry it. It touches nothing already stored.
- **`label_laps`** fills in laps that have *no* setup recorded, and only the
  ids you name. It refuses to change a lap that already carries a name.
- **`scripts/relabel_laps.py`** overwrites an existing name, and is
  deliberately not a tool.

The middle one used to be part of the first, filling every blank lap in the
session automatically. That sounds helpful and was the bug: the baseline run is
normally unlabelled too, so "I've loaded claude_v1" relabelled the baseline as
claude_v1 and destroyed the comparison. The boundary between two runs is a
garage stop, and nothing in the telemetry marks one — so the ids have to come
from the person who was there.

### Why `relabel_laps.py` is not an MCP tool

A late correction applied to the wrong half of an A/B split destroys the
comparison it exists to enable. The one case where overwriting is right is a
label that was wrong when it was written — and that is rare, destructive, and
worth making someone type:

```
python scripts/relabel_laps.py 87,88,89,90 claude_press_v1
python scripts/relabel_laps.py 87,88,89,90 claude_press_v1 --apply
```

Without `--apply` it only shows what it would change.
