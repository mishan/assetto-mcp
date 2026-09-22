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
lateral-g bar produced it. It is not the same bar in every tool: `lap_summary`
uses the one shared across its session's laps — the same bar its turn numbers
were built against — while `compare_laps` and `compare_runs` use one shared
across every lap being compared. Either way corner membership doesn't depend
on how hard an individual lap was driven, but the two bars are different
numbers, so the same lap can carry a different number of corners in the two
payloads. This is how you tell why. A lap handed to `analysis.lap_summary`
with no reference at all still falls back to its own peak, and says so.

The shared bar reduces one-sided corners rather than eliminating them: a gently
driven lap can still genuinely fall below a bar the others set.

### Turn numbers

Every corner also carries a **`turn`** — `T1`, `T2`, `T3` in track order from
the start/finish line. `corner` beside it is only that lap's ordinal: a light
corner missed on one lap closes the gap and shifts every number after it, so
"corner 5" is not reliably the same piece of road twice. `turn` is, because it
comes from the corners pooled across a whole session and grouped into pieces
of road. It is what to quote to a driver.

Corners from different laps are one piece of road when their apexes are
close and they turn the same way, or when they cover the same stretch of
track — at least half the shorter corner's length — on at least two laps
on each side. The second rule matters for long corners. On a long corner the
slowest point wanders from lap to lap, and where lateral g dips under the
threshold mid-corner the detector reports two pieces. Sebring's Sunset Bend
used to be numbered four times because of this. If most laps drove a piece
of road as one corner, it gets one number, and a lap that was split keeps
its slowest piece. The other piece still appears on that lap with
`turn: null`. If most laps drove it as two corners, it stays two.

`track_corners` is the table: where each turn starts, apexes and ends, where
it is braked for, which way it turns and how many laps cornered there.
`compare_runs` and `compare_laps` label their corners too, from the laps they
were given rather than from the session — each of those payloads carries its
own `turns` table so the labels in it can be placed without a second call.

Three things the numbering does not claim.

- **It is not the circuit's official numbering.** A kink taken flat carries
  too little lateral load to be detected, so nothing numbers it, and a circuit
  that calls a corner 3A is numbered straight through. There is no track
  database behind this; it is what the car drove.
- **It is per session, not per circuit.** A session is one car in one set of
  conditions, so all its laps share a lateral-g bar. Pooling across sessions
  would pool a road car's 1.1 g with a formula car's 3.4 g, and the bar that
  followed would erase the road car's lighter corners outright. Two sessions
  at the same circuit can therefore number differently; every payload carrying
  turn labels says which laps produced the numbering.
- **A corner only one lap drove takes no number.** It comes back under
  `unnumbered` instead. A spin violent enough for the detector to carve out as
  its own corner — which is how one at Sebring was eventually spotted — would
  otherwise renumber every turn after it on the strength of one lap. Corners
  a lap drove that the numbering has no entry for report `turn: null` rather
  than dropping the key.

A corner the start/finish line runs through is two pieces of road here, because
positions don't wrap. `compare_runs` has always matched corners the same way.

### Entry phase

Everything else about a corner is measured at the apex, where the car has
already been rotated. **`entry_phase`** covers the part before it: from the
brake point to the apex, or from turn-in on a corner taken without braking
(`from` says which).

- `slip_balance` — front minus rear, the apex definition, averaged over the
  phase. Negative here and positive at the apex is the car that's loose under
  braking and pushes in the middle, which the apex figure alone reads as plain
  understeer.
- `steer_norm` — mean steering over the phase, as a fraction of lock. A mean
  rather than a sum, because a sum grows with how early the brake point is.
- `rotation_deg` and `yaw_rate_peak_deg_s` — how far the car turned by the
  apex, and how fast at its quickest. Both come from `heading`, both are
  magnitudes (AC's sign convention has never been checked here), and both are
  null on laps recorded before heading was.

Heading wraps at ±π, so each step is unwrapped on its own; a step implying more
than 360°/s is a reset and is dropped, not clamped. The entry slip balance, peak
yaw rate and steering are corner channels in `compare_runs`, so a change aimed
at corner entry can show up as a lead when the apex figures don't move.

---

## Lap-time consistency

`compare_runs` compares means, and a change that makes the laps more repeatable
can leave the mean where it was. **`lap_time_consistency`** asks the other
question: did the spread of lap times change?

It is a **permutation test** on the ratio of the two runs' variances — not the
variance-ratio F test, which assumes normal lap times. A run where a spin now
and then costs five seconds is nothing like normal. Simulated with a 0.3 s
spread and a 15% chance of a 3–8 s spin on any lap, the same on both sides, the
F test called the spread changed in 47% of six-lap runs at 95% — and 44% even at
the corrected level. Relabelling the laps assumes nothing about their shape and
held 0.3%.

The cost is laps. The smallest p a relabelling can produce is fixed by how many
ways there are to relabel: three laps a side can never get below p = 0.1. A test
that can't clear its threshold is left out of the Holm family altogether —
it can't reject falsely, and counting it would only raise every other metric's
bar — and the payload says `too few laps to test` and how many laps a side it
would take (six, at the usual family size). Whether it's in is decided by the
lap counts alone, never by the result.

That has an uncomfortable consequence for the case that asked for this.
Sebring v9 — six laps inside a second after a run where two of six spun — comes
out at p = 0.41. If one lap in three spins by chance, six laps without one
happens 9% of the time, so six laps a side can't tell "the setup fixed it" from
"not this time". The payload says "within noise", and that is the honest
answer.

---

## Data quality flags

The analysis tools report when they had bad input rather than quietly
averaging it away.

- **`slip_quality`** — some telemetry ticks were discarded as glitched (AC
  occasionally emits a wheelSlip in the tens of thousands). It says how many
  corners were affected and how big the worst spike was, so you can judge
  whether the balance number is trustworthy.
- **`contacts`** — where bodywork damage went up during the lap, one entry per
  contact. Null both when there was no contact and when the server had damage
  off, because both read zero all lap — so a null is not evidence of a clean
  lap. A repair in the pits lowers damage and is not counted.
- **`contacts_inferred`** — the same question answered without the damage
  model. Any acceleration sample over 3 g (this car's tyres stop at about
  2.8) is an impact, a kerb launch or the floor; which one is decided by the
  nearest opponent at that instant, from the rival telemetry. The ego
  sample's wall clock is `completed_at − (lap_time_ms − t_ms)/1000`; rival
  rows carry the clock they arrived at, corrected within a batch by their
  own sim clock, and the distance is measured with both cars at the same
  instant so a close follower does not read as a hit. The threshold is 10 m,
  not a car length, because opponent positions lag: confirmed hits placed
  the other car at 6 to 8 m. With nobody near, the speed lost across the
  hardest sample itself tells a `wall` (15 km/h or more gone in the
  instant) from everything that loses speed over seconds — sand, grass,
  braking — and a rotation over 90 deg/s with the speed still there is a
  `snap`, a spin or a slide caught, whose g is the rotation itself. The
  rest is `kerb`. Verdicts are `contact`, `wall`, `snap`, `kerb` and
  `no_opponent_data`, which carries `if_alone`, the wall / snap / kerb
  reading with nobody near. The g is lateral and longitudinal combined, and
  the opponent search spans the whole impact, first spike to last, since a
  spin can stay over 3 g for seconds. `lap_summary` cuts kerb entries to
  position and g to stay inside its budget; `compare_runs`
  lists laps with a contact in `contacts` the way it lists laps that ran
  wide.
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

### The line map

`export_line_map` draws the same positions as a picture: every lap of a
session, or the laps you name, in one HTML file written to `exports/` in the
data directory. `scripts/driving_line_map.py` writes the same file from the
command line, opening the database read-only.

- **Laps** colours by lap order and picks out the fastest lap that counted —
  a real lap time (`db.lap_usability`) inside track limits. Out-laps, pit
  laps and abandoned laps are listed but hidden until ticked.
- **Setup** colours by setup name, so an A/B split shows at a glance. Only
  the first three setups driven get a colour of their own: with every line
  able to cross every other, a fourth hue can't be told apart from its
  neighbours, so the rest share one neutral and the table names each lap's.
- **Speed** and **Pedals** colour one selected lap and grey the rest.
- **Turn numbers and brake points** come from `turns.py`, the numbering
  `lap_summary` uses. Laps from several sessions are drawn without them,
  because each session numbers its own.
- **Corners** shows the reasoning behind those numbers. The session's turn
  map is drawn as blue bands, and pieces of road only one lap cornered on as
  orange ones. Over them go the corners the detector found on the selected
  lap, with a `?` where no turn matched. The table's Turns column counts
  how many turns each lap matched, plus any corners no turn claims.
- **Balance** colours the selected lap by front slip minus rear slip — the
  corner metric's quantity and sign, point by point — but only while the
  car is cornering, meaning lateral g is over the corner threshold. On a
  straight both axles idle at the same slip, and under straight-line
  braking the fronts slip more whatever the balance. Every lap shares one
  colour scale. Zero means equal slip, which is not necessarily neutral for
  a given car: on the NSX the median while cornering sits near +0.1. Read
  it by comparing laps and corners with each other.
- **Gears** colours the lap by gear and marks every shift. A shift is read
  across the neutral AC reports mid-change, and neutral held longer than a
  second counts as a stop, not a shift. The legend's per-gear table has
  upshift revs (median and 10th–90th percentile) and the revs each
  downshift landed at. The "rev ceiling" is the highest revs the car showed
  at full throttle. No redline is recorded, so it is not necessarily the
  limiter, and stretches held near it are marked. Revs come from the
  samples either side of a change, 40 ms apart.
- **Surface** is the bump map. Each suspension channel has a centred
  quarter-second average taken off it. What is left is the road, not the
  weight transfer, and it is reported as mm RMS per ~10 m stretch, the
  median over the laps that counted. It uses the in-game app's suspension
  travel where that was captured, and 25 Hz ride height where it was not.
  Pooling removes a kerb struck once but not a kerb taken every lap, which
  reads like a bump. The five roughest stretches are labelled.

The charts under the map follow the selected lap, and any of them can be
ticked on: lateral g, speed, revs and gear, balance, front and rear tyre
core temperatures, surface, ride height and elevation. Tyres are two to a
chart, left and right in the same two colours front and rear, because
four lines in one chart can't be told apart.

Marks can be toggled on top of any view:
- **Off track:** the track-limits rule, all counted wheels out for long
  enough to count.
- **Lockups:** `braking_report`'s front lockup runs. Its slip threshold has
  never been calibrated, and a spin reads as a lockup.
- **Complaint presses:** each press sits on the lap it was pressed on. A
  press made with no session running can't be placed, and the page says
  how many there are.

### Opponents on the map

The **Rival** view compares the selected lap with an opponent's lap from
the same session, picked in the sidebar; the default is the quickest
opponent's quickest lap. Your lap is coloured by who was quicker at each
point, both cars' braking points are ticks on your line, and where the
opponent's world position was recorded their line is drawn dashed. Charts
for speed, brake, throttle and the time gap can be ticked on, and the
legend has a turn-by-turn table: how much later the opponent braked and
got back on the throttle, in metres, and both cars' minimum speeds.

Opponents are sampled at 10 Hz and you at 25, at different moments, so
both are compared on one grid of 500 track positions, with gaps of up to two
steps interpolated. Each opponent carries its three quickest laps.

An opponent lap's time is `recorded` (the in-game app timed it), `timed`
(its samples carry the car's clock, since schema v14), or `estimated`
(speed integrated over the track length: 0.1–0.9% short where the true time
is known, and a spin can take it 5% out). The time gap uses the same method
for both laps, so a method's bias mostly falls out. The legend warns when
the opponent's car is a different model, or when its model was not
recorded, and when a server did not transmit its pedals.

Under the map, two charts follow the selected lap by track position. One
is lateral g — the smoothed trace `detect_corners` reads, not the raw
channel — against the exact threshold it was held to. The other is speed,
with brake points. Turn spans are shaded across both. A turn that appears
split in two, or a kink that never crossed the threshold, is visible there.

It draws raw samples rather than `driving_line`'s slices, thinned to one
every 80 ms by time rather than by count, since retention has already
thinned old laps. A lap's samples can straddle the start/finish line at
either end, so each trace is split at every wrap and only the longest run
drawn.

The file is self-contained and fetches nothing — no web fonts, no scripts
from anywhere — so opening it tells nobody anything. Laps from different
cars or layouts are refused: their coordinates don't share an origin.

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
  turns.py      a session's turn numbering, shared by tools and scripts
  line_map.py   driving-line map as one HTML file (+ line_map.html)
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
  driving_line_map.py  write a session's line map without a server
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
