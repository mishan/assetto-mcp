# Backlog

Open work, roughly in the order it is worth doing: cheap checks first, then
work that needs a real session, then larger builds, then what is waiting on
CSP. Each item says what is wrong, why it matters, where the code lives,
and its status. Written against the database schema at v15.

---

## 1. Check entry-phase corner metrics against real laps

**Status:** built; verified on synthetic laps only.

Every figure in `entry_phase` (`analysis.py`) comes from tests on synthetic
laps. Whether a real entry snap is visible in 25 Hz `heading`, and what yaw
rate a normal entry runs at, are unmeasured; the 2°/s floor on the yaw
channel is a guess. Until someone checks a real session, treat a
`compare_runs` lead from these channels with suspicion.

---

## 2. A physics worker for what only physics can see

**Status:** open. Single-player only; inference stays the fallback.

Two things the game knows and the app tier cannot read. Both would use the
mechanism `lua_app/assetto_mcp/suspension_worker.lua` already uses for
333 Hz damper sampling, and both share its constraint: CSP forbids physics
scripting online.

**The game's own lap verdict.** Validity is inferred from `tyres_out`
evidence (`invalid_source: 'inferred'`). CSP exposes AC's own answer:

```lua
-- acc-lua-internal/included-new-modes/p2p-1v1/impl_steam_worker.lua:111
ac.onLapCompleted(0, function (carIndex, lapTime, valid, cuts, lapsCount,
                               splits, lapCrossTime)
```

That file is started with `physics.startPhysicsWorker(...)`
(`impl_steam.lua:644`). A worker could post `valid` and `cuts` over the
existing HTTP bridge, and laps would carry `invalid_source: 'game'`.

There is no other route: the plain app context has no lap validity field
(`ac.getCar()`'s fields are generated from CSP's C++ and none of the
built-in apps read one), and `state_cphys_surface.isValidTrack`
(`acc-lua-sdk/ac_car_cphys.lua:36`) needs a per-car custom physics script
with extended physics.

**ABS and TC intervention.** The stored `abs_active` and `tc_active` are a
setting or threshold, constant across a lap. The real flags are
`ac.CarState.absInAction` and `tractionControlInAction`, which are
physics-only. With them, `braking_report` could say when ABS acted rather
than inferring it from slip.

---

## 3. Scrappy laps pass as representative

**Status:** open.

A lap 7% off the pace is under `OUTLIER_FRACTION` (`analysis.py`), so it
stays in a comparison, and one such lap can inflate a run's lap-time
`resolution` to many seconds. Validity and representativeness are
different questions; consider a separate "representative lap" notion.

---

## 4. Corner detection leftovers

**Status:** open.

- **One-sided corners.** Fewer since the shared lateral-g reference
  (`analysis.lat_g_reference`), but a gently driven lap can still fall
  below a bar the other laps set.
- **Joins that happen on some laps only.** Same-direction stretches are
  joined when the load between them stays over `CORNER_RELEASE_SHARE` (0.7)
  of the bar. Where a gap sits near that line it is joined on some laps and
  not others, and the turn is found on fewer laps. Deciding a join once per
  session, from how most laps drove the gap, would settle it.
- **A kink near the bar.** A curve whose load only sometimes reaches the
  bar is found on some laps only, and a left-right pair just under it can
  look like one split corner.
- **The second piece of a split corner has no turn number.**
  `label_corners` pairs one corner per turn, so the extra piece shows as
  `?` in the line map's Corners view and `+n` in its Turns column. Labeling
  it "part of T18" needs a second, span-based pass.

---

## 5. Turn numbers for tracks without `sections.ini`

**Status:** partly done. Session-local numbering is built
(`analysis.corner_map`, `label_corners`, `track_corners`). Where the track
ships `data/sections.ini`, turns carry the circuit's own `name`
(`circuit.py`), and `where` and `locate` use it. Still open: tracks with no
`sections.ini`, and a hand-written override for them.

Without names, turn numbers are the session's, not the circuit's:

- A kink taken flat is under the lateral-g bar, so it is not numbered, and
  every corner after it is one lower than the circuit says. A circuit that
  numbers a corner 3A is numbered straight through.
- The bar is per session because it is per car, so two sessions at one
  circuit can number differently; only `built_from_laps` in each payload
  shows it.

**Data sources.** None covers Assetto Corsa's mod long tail:

- [tobi/track-atlas](https://github.com/tobi/track-atlas): MIT, about 40
  circuits, one entry per layout. Corners carry `number`, `code` (`2a`,
  `5c`, `9a`), names, and `marker`, a 0-1 lap fraction comparable to AC's
  `normalizedCarPosition`. Geometry is OSM-derived and ODbL, so
  distributing it needs attribution.
- [Lovely-Sim-Racing/lovely-track-data](https://github.com/Lovely-Sim-Racing/lovely-track-data):
  track-atlas's upstream. Files are keyed by SimHub track id, which for AC
  is the folder name in `sessions.track`, with `turn: [{name, marker}]`.
  AC coverage is one track and there is no LICENSE file, so it cannot be
  vendored.
- CrewChief's `trackLandmarksData.json` is keyed per sim, including AC
  (`ks_silverstone:national`), but holds names, not numbers, and has no
  license.
- `content/tracks/<track>/[<layout>/]data/sections.ini`
  (`[SECTION_0] IN=0.88 OUT=1 TEXT=...`) is already in AC's `norm_pos`
  space and needs no alignment. Kunos does not ship it; the OverTake
  "Corner names (track description)" pack supplies it for the Kunos set and
  many mods.

**Shape of the fix:** a per-`(track, track_config)` override, read from the
install's `sections.ini` when present and hand-written otherwise, matched to
detected corners by order and turn direction rather than absolute position.
An imported lap fraction is another sim's spline: good for ordering, not
for a 50 m tolerance.

---

## 6. Compare incident rates, not just spread

**Status:** open.

`lap_time_consistency` tests whether the spread of lap times changed. A
spin is an event, and "how often" is a count question that a spread test
answers badly. Counting incident laps per run and comparing rates is the
next step. It will need more laps than anyone wants to drive (six clean
laps after a one-in-three spin rate happen 9% of the time by chance), and
the payload should say so.

---

## 7. Put the rival data to more use

**Status:** open. Built: rival lap times from each car's own trace
(`analysis.trace_lap_times`), `time_by_turn` on `compare_to_rival`, and
`field_benchmark`. In rough order of payoff:

- **Fix the lap time at the source.** `store_rival_batch` (`db.py`) files
  the server's `last_lap_ms` under a lap the moment the lap counter ticks,
  usually before the server has updated it, so `rival_laps` holds the
  previous lap's time. Reads prefer the trace (`rival_trace_lap_times`),
  but anything reading the table directly is wrong. Either file the time
  on the next batch whose `last_lap_ms` changed, or stop writing the table
  and time from the trace at write time.
- **Cost of trace timing.** `list_rivals` reads every rival sample of the
  session to time laps: about 150k rows for a 12-lap race with ten cars,
  slow for a two-hour practice with twenty. Cache per (session, car, newest
  `lap_count`), or time laps once as they complete.
- **Gaps on track.** The time gap to the car ahead and behind at matched
  track position, lap by lap, and the closing rate. `created_at` is server
  receive time and too coarse under a few seconds; use the cars' own
  `t_ms` at a shared spline, with each car's clock offset fixed from a line
  crossing.
- **Fuel against laps remaining.** `live_snapshot` reports position, lap N
  of M and `laps_remaining`. Compute fuel laps left against that, from the
  session's own burn, so a fuel call can say "enough to the flag" or "short
  by half a lap". Unverified: that the game shows the checkered flag to a
  lapped car from the moment the leader finishes, which is what caps
  `laps_remaining` at one for a car off the lead lap.
- **Where to attack and defend.** From `field_benchmark` per rival: the
  driver's strong corners are where a pass is set up; a rival's strong exit
  onto a straight is where the driver is vulnerable.
- **Car against driver.** Split each rival's gain into straights and
  corners. A rival consistently quicker at the end of every long straight
  points at the car or the wing, not the driver, and a coaching list should
  say which gains are which.
- **Rival habits.** Late-stint fade, lap-time spread, and where each rival
  goes off.
- **Lines.** Rival `pos_x`/`pos_z` are stored; `driving_line` could overlay
  the quickest rival's line through a corner the benchmark flags.
- **A pre-race brief** from practice and qualifying: one card per rival,
  and the quickest rival lap as a ghost in the line map.
- **Rival data in the handoff.** The race-engineer skill should run
  `field_benchmark` after practice, qualifying and each race, put
  `priorities` in the handoff, and quote only trace-timed rival laps
  (`lap_time_source: "trace"`).

---

## 8. Body slip angle

**Status:** open.

Heading against the direction of travel from position gives yaw relative to
the car's own path rather than a rate. This needs AC's heading convention
pinned against position on a real straight first. Which sign of `roll`
means "leaning out of the corner" is likewise unverified and could be
pinned in the same check.

---

## 9. Check `restart_server` and the updater on Windows

**Status:** built; tested against a fake client only.

`python -m assetto_mcp.server` starts `assetto_mcp/supervisor.py`, which
runs the real server as a child and replaces it when `restart_server` exits
with code 75: in-flight requests get an error, the client's `initialize` is
replayed to the new child, and `notifications/tools/list_changed` is sent.
Only a real setup can show:

- Whether Claude Desktop re-lists tools on `list_changed`. If not, a tool
  added by an update is invisible until the client restarts, though
  existing tools keep working.
- How long the bridge takes to get port 9666 back after a restart on
  Windows with the in-game app polling it. `BIND_RETRY_SECONDS`
  (`bridge.py`) allows five minutes.
- That the job object takes the child down when Claude Desktop is quit from
  the tray, so no orphan holds the recorder claim and the port.

`apply_update` (`assetto_mcp/update.py`) is tested against a local bare
repository and a fake compare endpoint. Unverified on Windows: whether git
is found from Claude Desktop's environment, whether `pip install -e .` with
the server running fails on a locked file and says so, and whether
`tasklist` sees `acs.exe` when Content Manager launched it.

---

## 10. Fill the display-mapping registry automatically

**Status:** waiting on CSP.

The registry (`display_observations`, `display_notes`) only fills when a
driver reads values off the setup screen. CSP exposes `displayMultiplier`
and `showClicksMode` but no formatted display string;
`acc-lua-internal/lua-module/src/_hotlap_utils.lua:265` computes
`e[1] * sd.displayMultiplier` itself, with the same arithmetic and the same
exposure to being wrong. Revisit if CSP exposes the rendered value.
