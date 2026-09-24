# Backlog

Open work first, roughly in the order it is worth doing: cheap things the
driver will notice, then work that needs a real session, then larger
builds, then what is parked on someone else. Each entry says what is wrong,
where it bit, and where the code lives, so a future session can act without
re-deriving any of it. What has been addressed is summarized at the bottom.

Written after the Sebring / NSX GT3 session, and kept current since. Test
suite stands at 595 passing with the Lua tooling installed,
schema at v15.

---

## 1. Check entry-phase corner metrics against a real lap

**Status:** built, never seen working on real data. A look, not a build.

Every figure in `entry_phase` is tested on synthetic laps only. Whether a
real entry snap is visible in 25 Hz heading, and what yaw rate a normal
entry at Sunset Bend runs at, are unmeasured. The 2°/s floor on the yaw
channel is a guess. Look at a real v6-style run before trusting a lead
from it.

---

## 2. A physics worker for what only physics can see

**Status:** open. Single-player only; inference stays the fallback.

Two things the game knows and the app tier cannot read, both behind the
same mechanism `lua_app/assetto_mcp/suspension_worker.lua` already uses for
damper sampling at 333 Hz, and both under the same constraint: CSP forbids
physics scripting online.

**The game's own lap verdict.** Validity is inferred from `tyres_out`
evidence today (`invalid_source: 'inferred'`). CSP exposes AC's own answer:

```lua
-- acc-lua-internal/included-new-modes/p2p-1v1/impl_steam_worker.lua:111
ac.onLapCompleted(0, function (carIndex, lapTime, valid, cuts, lapsCount,
                               splits, lapCrossTime)
```

That file is started with `physics.startPhysicsWorker(...)`
(`impl_steam.lua:644`). The worker could post `valid` and `cuts` over the
existing HTTP bridge and laps would carry `invalid_source: 'game'`.

Not available elsewhere: the plain app context has no lap validity field
(`ac.getCar()`'s fields are generated from CSP's C++ and none of the
built-in apps read one), and `state_cphys_surface.isValidTrack`
(`acc-lua-sdk/ac_car_cphys.lua:36`) needs a per-car custom physics script
with extended physics.

**ABS and TC intervention.** The stored `abs_active` and `tc_active` are a
setting or threshold, constant across every sample of a lap. The real flags
are `ac.CarState.absInAction` and `tractionControlInAction`, marked
physics-only. With them, `braking_report` could say when ABS acted rather
than inferring it from slip.

---

## 3. Scrappy laps pass as representative

**Status:** open.

Sebring lap 160 (2:10.161 against a 2:02.7 best) stayed `valid` and blew
that comparison's lap-time resolution out to 11.5 seconds. It is 7% off the
pace, under `OUTLIER_FRACTION`, working exactly as designed, and still
ruining results. Consider a separate "representative lap" notion distinct
from validity.

---

## 4. Corner detection leftovers

**Status:** open. Much better than it was; not zero.

- **One-sided corners.** Fell from 7 of 17 to 3 of 18 after the shared
  lateral-g reference (`analysis.lat_g_reference`).
- **Joins that happen on some laps only.** Same-direction stretches are
  joined when the load between them stays over `CORNER_RELEASE_SHARE` (0.7)
  of the bar, and where a gap sits near that line it is joined on some laps
  and not others. Two turns lost laps to this when it went in: Kyalami (39)
  0.797, 12 laps of 12 to 9, and Interlagos 0.530, 9 of 9 to 8. Deciding a
  join once per session, from how most laps drove the gap, would settle it.
- **A kink near the bar.** Interlagos 0.587/0.605 is one curve and is now
  one turn, but found on 5 laps of 9: on the rest its load never reached
  the bar. Kyalami (39) 0.575/0.608 is not a split at all — a left then a
  right, each under the bar on some laps.
- **The second piece of a split corner has no turn number.**
  `label_corners` pairs one corner per turn, so it shows as `?` in the
  Corners view and `+n` in the Turns column. Labelling it "part of T18"
  needs a second, span-based pass.

---

## 5. Turn numbers are the run's, not the circuit's

**Status:** open. Session-local numbering is built (`analysis.corner_map`,
`label_corners`, `track_corners`); everything that would make the numbers
agree with the circuit's own is not.

- **Not the circuit's numbering.** A kink taken flat is under the
  lateral-g bar, so it is not detected and not numbered, and every corner
  after it is one lower than the circuit says. A circuit that calls a
  corner 3A — Sonoma T3/T3A, Mosport 5C — is numbered straight through.
- **Not shared between sessions.** The bar is per session because it is per
  car; two sessions at one circuit can number differently, and only the
  `built_from_laps` in each payload makes that visible.

**Sources, surveyed 2026-09-09.** None covers Assetto Corsa's mod long tail:

- [tobi/track-atlas](https://github.com/tobi/track-atlas) — MIT, 40
  circuits, one entry per layout. Corners carry `number`, `code` (which
  does hold `2a`, `5c`, `9a`), name layers, and `marker` — a lap fraction
  0-1, directly comparable to AC's `normalizedCarPosition`. Has Mugello,
  Suzuka, Sebring, Silverstone, Monza, Spa. No Sonoma. Geometry is
  OSM-derived and ODbL, so distributing it needs attribution.
- [Lovely-Sim-Racing/lovely-track-data](https://github.com/Lovely-Sim-Racing/lovely-track-data)
  — the upstream track-atlas re-publishes, and the better shape: files are
  keyed by SimHub track id, which for AC *is* the folder name in
  `sessions.track` (`rt_lime_rock_park`), with `turn: [{name, marker}]`.
  But its AC coverage is one track, and the repository carries no LICENSE
  file at all, so it cannot be vendored.
- CrewChief's `trackLandmarksData.json` is keyed per sim including AC
  (`ks_silverstone:national`) but holds names, not numbers, and no license.
- `content/tracks/<track>/[<layout>/]data/sections.ini` —
  `[SECTION_0] IN=0.88 OUT=1 TEXT=...`, IN/OUT in `norm_pos`, so it is the
  only format already in AC's own coordinate space and needs no alignment
  at all. Kunos does not ship it; the OverTake "Corner names (track
  description)" pack supplies it for the Kunos set and many mods,
  hand-installed.

**Shape of the fix:** a per-`(track, track_config)` override read from the
install's own `sections.ini` when it is there, hand-written when it is not,
matched to the detected corners by order and turn direction rather than by
absolute position — an imported lap fraction is another sim's spline, good
for ordering and not for a 50 m tolerance.

---

## 6. Compare incident rates, not just spread

**Status:** open.

`lap_time_consistency` tests whether the spread of lap times changed. A spin
is really an event, and "how often" is a count question a spread test
answers badly. Counting incident laps per run and comparing rates is the
natural next step. It will need more laps than anyone wants to drive — the
Sebring v9 shape (six clean laps after a one-in-three spin rate) is 9%
likely by chance — and should say so.

---

## 7. Body slip angle

**Status:** open.

Heading against the direction of travel from position: yaw relative to the
car's own path rather than a rate. Needs AC's heading convention pinned
against position on a real straight first; nothing here has checked it.
Which sign of `roll` means "leaning out of the corner" is likewise
unverified, and could be pinned in the same look.

---

## 8. Parked: fill the display-mapping registry automatically

**Status:** waiting on CSP.

The registry (`display_observations`, `display_notes`) only fills when a
driver reads values off the setup screen. CSP exposes `displayMultiplier`
and `showClicksMode` but no formatted display string —
`acc-lua-internal/lua-module/src/_hotlap_utils.lua:265` does
`e[1] * sd.displayMultiplier` itself, the same arithmetic and the same
exposure to being wrong. Revisit if CSP ever exposes the rendered value.

---

## Measured baselines — NSX GT3 Evo on `claude_sebring_v9`

Taken on Sebring. Most are properties of the car and the setup rather than
the circuit, so they carry to the next track — but see the warnings: the
gradients are car-and-track, not car alone.

> ⚠️ **The gradients and the wear figures below were produced by code
> that has since been corrected, and neither has been re-run.** Re-run
> `attitude_report` on lap 202 and `stint_wear` per stint on the Sebring
> race, then replace the figures and drop these warnings. Do not quote
> either to the driver until then. The brake-bias and
> `abs`-field observations are raw reads and stand as written.

**Roll gradient 0.723 deg/g. Peak roll 3.54°. Dive 0.513 deg/g.**
(`attitude_report`, lap 202.) That is a stiff car — with the front
anti-roll bar on its minimum stop and the rear only one step up. The
115/120 N/mm springs are doing nearly all the work in roll.

> ⚠️ **Both gradients came from the origin-forced fit on absolute
> attitude, which counted static tilt and rake as roll and dive.** Peak
> roll stands. Re-run, they are car-and-track numbers rather than
> properties of the car: Sebring's banking that rises with cornering load
> stays in the slope even under the new fit, and so does road grade that
> varies with where the car brakes. The *conclusion* — a stiff car, springs
> dominating the bars — rests on the setup sheet as much as on the
> gradient, so it is the part least likely to move. That is an argument,
> not a measurement.

**This reframes a lot of the season's reasoning.** Weeks were spent
arguing about anti-roll bars on a car where they are a minor term on top
of the springs. If the question is how the car rolls, spring rate and ride
height carry the authority. And every future bar or spring change is now a
one-number test: the gradient moves, or the change did not do what it was
meant to.

**The front is the axle nearer locking** under straight-line braking, on
every lap examined, with `FRONT_BIAS` at 60. Consistent, and the lever if
that ever needs changing.

**The `abs` field is not constant across sessions** — 0.09 in practice and
qualifying, 0.06 in the race, same setup file. Worth asking whether the
rotary was touched or the server changed it.

**Wear at racing pace: about 0.05%/lap front, 0.024%/lap rear**, rising to
~0.055% rear late in a stint. Total over a 13-lap race: 0.7% front, 0.44% rear.
Tyre life is a non-issue for this car over a sprint distance; do not spend
setup effort on it.

> ⚠️ **These three figures — the rate, the total, and the "rising late"
> — are exactly the three outputs the un-segmented `stint_wear` got
> wrong.** Re-run per stint. The *conclusion* — 0.7% over a race distance
> is nothing — survives an order of magnitude of error in either
> direction, which is why it is still here.

---

## Addressed

What was fixed, and the one thing about each worth not forgetting. Git
history has the detail.

- **Lap validity stored as facts** (schema v11). Laps carry the evidence —
  `max_tyres_out`, `excursions`, `off_track_ms` — and derive `invalid`,
  `out_lap`, `pitted`, `outlier`, `complete` and `invalid_source` from it.
  The threshold is re-appliable with `rescore_track_limits`; the v11
  migration used it to restore laps like Sebring 129 that had been wrongly
  marked invalid. `compare_runs` no longer drops laps silently: ran-wide
  laps are compared and named, and only laps whose time is not a lap time
  are excluded, with a reason. Open remainder: item 2.
- **`set_session_setup` no longer relabels the baseline.** It is
  forward-only; backfilling moved to `label_laps`, which only fills blanks
  on named lap ids. Covered by
  `test_naming_a_new_setup_does_not_relabel_the_baseline`.
- **Laps carry the setup they were measured on** (schema v15). The app's
  setup posts go into `setup_history` under a fingerprint of the values,
  fuel excluded, and each lap is stamped with the one on the car
  (`laps.setup_fp`, `setup_changed` for a lap through a garage stop). A
  stated name binds to the first lap after the call rather than to the
  moment of it, so "I'm loading v2" said early does not name the baseline;
  a setup changed without a call leaves laps blank instead of wrongly
  named. `label_laps` refuses ids spanning two measured setups, and
  `compare_runs` reports `setup_changes` and warns when both sides were on
  identical values. Names are still typed: the fingerprint says *whether*
  two laps shared a setup, not what the file was called. Laps before v15,
  or without the app, are unmeasured.
- **Consistency is tested**, as `lap_time_consistency`: a permutation test
  on the variance ratio, because a variance-ratio F test called identical
  spin-prone runs different 47% of the time. It joins the Holm family only
  when the lap counts can clear its threshold, and otherwise says how many
  laps it would take. The Sebring v9 case that motivated it comes out at
  p = 0.41 — six laps cannot tell a fix from luck. Open remainder: item 6.
- **Entry-phase corner metrics**, as `entry_phase` on every corner: slip
  balance, mean steering, peak yaw rate and rotation from brake point (or
  turn-in) to apex, and corner channels in `compare_runs`. Unverified on
  real laps: item 1.
- **Every stored channel has a reader.** `stint_wear` (per stint, counting
  up, out-laps in totals but not rates), `braking_report` (straight-line
  slip per axle and lockup runs — not ABS activity, since `abs_active` and
  `tc_active` turned out to be settings), `attitude_report` (signed fit
  with a free intercept, so the gradient is a car-and-track number), and
  `db.lap_endpoints` for cheap stint reads. All three distinguish no
  measurement from a measurement of zero.
- **Setup display mapping** (schema v13): `display_observations` and
  `display_notes`, fitted by `setups.fit_display`, with
  `record_display_value`, `record_display_range` and
  `forget_display_value`. Every reported value carries a `source`, and
  `unknown` states no value. Open remainder: item 8.
- **Long corners numbered once.** `_corner_clusters` joins groups whose
  corners share road on enough laps, and only chains same-direction
  corners by proximity. Sebring's Sunset Bend went from four turns to one;
  `compare_runs` uses the same grouping. Open remainder: item 4.
- **Session-local turn numbers** (`analysis.corner_map`, `label_corners`,
  `track_corners`). Open remainder: item 5.
- **Opponents recorded properly** (schema v14). The app read three fields
  CSP does not have; it now takes the car from `ac.getCarID(i)`, times
  opponent laps itself from `lapCount` and `timestamp`, and stores each
  sample's clock and world position. The test harness's fake car now
  carries only real CSP fields. Opponent laps recorded before this have no
  clock, and the line map times them by integrating speed, flagged as an
  estimate. Confirmed working in a real session.
- **Contacts inferred when damage is off**, as `contacts_inferred` on
  `lap_summary` and `contacts` on `compare_runs` (`analysis.infer_contacts`).
  Acceleration over 3 g placed against opponent positions at the same wall
  clock, 10 m radius because opponent positions lag; with nobody near,
  instant speed loss tells a wall from a kerb. This also covers wall
  detection, which the damage counter cannot when the server has damage
  off. It exists because Interlagos race 1, lap 295 was a 6.8 g hit that
  had been reported to the driver as a braking error.
- **Positions in meters and turns.** Every tool that reports a lap
  fraction now puts the same place beside it in meters past the
  start/finish line (`at_m`, `apex_m`, `brake_point_m`, ...) and, where it
  is not already a labelled turn, a `where` a driver can use: "T1, 70 m
  after turn-in", "T4 braking zone, 90 m before turn-in". Turns carry
  `brake_before_turn_in_m`, and brake-point differences come in meters
  too (`assetto_mcp/places.py`, applied in `server._placed`). The collector
  now stores AC's own track length for every session; sessions without one
  have it estimated from clean laps' speed, within 0.4% of the game's
  figure where both exist, and the payload says which.
- **Brake points are the braking that slowed the car.** `_brake_zone_start`
  took the last braking run before the apex, so a touch of the pedal
  mid-corner became the brake point: Suzuka's Spoon reported one 0.036 of a
  lap late on 16 laps of 18, and a hairpin whose trail-off sat under
  `BRAKE_ON` split in two on half its laps. Runs are now weighed by the
  speed they took off (a brake held on the grid took none), the earliest
  run with a quarter of the heaviest one's drop is the brake point, and the
  walk stops at the start/finish line. Brake points more than 0.005 of a
  lap from their corner's median, over every recorded session: 206 of 1410
  before, 77 of 1428 after.
- **A long curve is one corner** (`CORNER_RELEASE_SHARE`). Same-direction
  stretches over the bar join when the load between them stays over 70% of
  it. Suzuka's long left after the hairpin was two to four corners a lap
  and is one on every lap; turns found on fewer than three laps in four,
  over every session, went from 50 to 24. Open remainder: item 4.
- **`_migrate` v1 unguarded ALTER.** All six ALTER sites go through
  `_add_column`, which no-ops on a missing table.
