# Backlog

Known gaps and bugs, ranked by whether they destroy data the driver has
already produced. Each entry says what is wrong, where it bit, and where the
code lives, so a future session can act without re-deriving any of it.

Written after the Sebring / NSX GT3 session, and kept current since. Test
suite stands at 454 passing with the Lua tooling installed,
schema at v13.

---

## 1. Lap validity was inferred, not read

**Status:** largely fixed in schema v11. The remaining gap is reading the
game's own verdict, which is reachable but not yet wired up — see the end.

`collector._loop` used to decide validity with `numberOfTyresOut > 2` and
store one boolean. That was wrong in both directions, and `compare_runs`
dropped the lap and said nothing either way.

**Where it bit:** Sebring, lap 129. A 2:06.769 that the game showed as valid
was stored `valid: 0`. Sebring is ringed with wide flat kerbs and painted
apron that put three wheels outside the surface without the game calling a
cut. And the reverse, item 8: Sebring lap 160, 7% off the pace, stayed
`valid` and blew out a comparison.

**What changed:** a lap now stores *facts* and derives the verdicts.

- `max_tyres_out`, `excursions`, `off_track_ms` — the evidence, computed
  from `samples.tyres_out` at 25 Hz, which was there all along and was being
  collapsed into one bit and thrown away.
- `invalid` — track limits, derived from that evidence at
  `db.TRACK_LIMITS_WHEELS` (now 4, was effectively 3), with a minimum
  duration so one glitched tick is not a cut.
- `out_lap`, `pitted`, `outlier`, `complete` — the other reasons a lap used
  to be silently invalid, now separate and separately readable.
- `invalid_source` — `inferred` or `game`, so a reader can tell a
  measurement from a guess.

Because the evidence is stored, the threshold is re-appliable: changing it
and running `rescore_track_limits` re-scores every lap still holding a
full-resolution trace. The v11 migration did exactly that to the existing
database, so laps wrongly marked invalid came back without anyone re-driving
them. Laps whose traces retention has thinned keep the verdict measured at
full resolution and are reported as skipped rather than re-scored from a
decimated trace.

**And nothing is dropped any more.** Laps that ran wide are compared and
reported in `ran_wide`; only laps whose *time is not a lap time* are
excluded, by name, with a reason.

### What is left: the game's own verdict

CSP does expose it, and from a context this project already runs:

```lua
-- acc-lua-internal/included-new-modes/p2p-1v1/impl_steam_worker.lua:111
ac.onLapCompleted(0, function (carIndex, lapTime, valid, cuts, lapsCount,
                               splits, lapCrossTime)
```

`valid` and `cuts` are AC's own answer. That file is started with
`physics.startPhysicsWorker(...)` (`impl_steam.lua:644`) — the same
mechanism `lua_app/assetto_mcp/suspension_worker.lua` already uses for
damper sampling at 333 Hz. So the worker could post the game's verdict over
the existing HTTP bridge and laps would carry `invalid_source: 'game'`.

Constraint: CSP forbids physics scripting online, so this is single-player
only and inference stays the fallback — the same worker/app tier split the
suspension report already has.

For completeness, what is *not* available: the plain app context has no lap
validity field at all (`ac.getCar()`'s fields are generated from CSP's C++
and none of the built-in apps read one), and `state_cphys_surface.isValidTrack`
(`acc-lua-sdk/ac_car_cphys.lua:36`) is AC's authoritative per-surface flag
but needs a per-car custom physics script with extended physics.

---

## 2. `set_session_setup` has a timing trap

**Status:** fixed — the two behaviours are now separate tools.

The tool used to do two things: label laps completed from now on, *and*
backfill every lap with no label. Since the baseline run is normally
unlabelled too, telling it "I've loaded claude_v1" stamped `claude_v1` onto
the baseline and destroyed the A/B it was being asked to set up.

**Where it bit:** twice in one week. Suzuka laps 87–90 (labelled
`claude_toe_v1`, actually `claude_press_v1`) and Sebring laps 157–161
(labelled `claude_sebring_v7`, actually `v8`).

**What changed:** `set_session_setup` is forward-only and touches nothing
already stored. It reports which earlier laps have no setup rather than
guessing at them. A new `label_laps` tool backfills, but only laps whose ids
are named and only where the setup is currently blank — the boundary is a
garage stop, which nothing in the telemetry marks, so only the driver can say
where it was. Covered by `test_naming_a_new_setup_does_not_relabel_the_baseline`.

**Still open underneath it:** attribution is a claim someone types, not a
measurement. The collector could stamp laps from the live setup fingerprint
(`setup_values` already holds one per session) and remove the question
entirely. That would also fix item 3 permanently rather than case by case.

---

## 3. Roughly a dozen laps carry the wrong setup name right now

**Status:** open, and purely mechanical.

| Track | Laps | Labelled | Actually |
|---|---|---|---|
| Suzuka | 87–90 | `claude_toe_v1` | `claude_press_v1` |
| Suzuka | 92–93 | `claude_toe_v1` | `claude_bias_v1` |
| Sebring | 136–138 | `nordschleife` | `claude_sebring_v4` |
| Sebring | 157–161 | `claude_sebring_v7` | `claude_sebring_v8` |

```
python scripts/relabel_laps.py 87,88,89,90 claude_press_v1 --apply
python scripts/relabel_laps.py 92,93 claude_bias_v1 --apply
python scripts/relabel_laps.py 136,137,138 claude_sebring_v4 --apply
python scripts/relabel_laps.py 157,158,159,160,161 claude_sebring_v8 --apply
```

Verify each against `identify_setup` history before applying — these
attributions come from reasoning about garage stops, not from recorded fact.

`label_laps` will not do this: these laps carry a *wrong* name rather than no
name, and overwriting a name is the destructive case. The script stays a
script for exactly that reason.

---

## 4. `compare_runs` cannot detect a change in consistency

**Status:** open. Cost us the clearest result of the Sebring session.

Every metric is judged by comparing means against pooled spread. When a
setup change makes the driver *more consistent*, the mean may barely move
while the spread collapses — and the t-test is blind to it.

**Where it bit:** v9 at Sebring. Six laps inside 0.97 s, zero invalidated,
after a run where one lap in three ended in a spin. Lap time reported
"within noise" — because the previous run's spin had inflated the baseline
variance so far that nothing could clear it. **The change removed the very
thing that made it detectable.**

**Fix:** add a variance-ratio (F) test alongside the mean comparison, with
the same Holm correction discipline. Critical values would have to be
computed rather than tabulated, as `_t_crit` already is — scipy is not a
dependency and must not become one.

---

## 5. No entry-phase corner metrics

**Status:** open.

`detect_corners` and `_corner_stats` describe the apex and the region around
it. Nothing measures the phase between the brake point and the apex, which
is where trail braking, entry rotation and most spins live.

**Where it bit:** `claude_sebring_v6` (coast diff 40% → 60%) was aimed
squarely at entry stability at Sunset Bend. Every metric read "within noise"
and zero corners produced a lead. The tooling could not see the thing the
change was for.

The spin *was* eventually visible, but only because it was violent enough
that the corner detector carved it out as its own corner — a side effect, not
a measurement.

**Fix:** slip balance, yaw rate and steering integrated over
`brake_point_pos` → `apex_pos`, reported per corner alongside the apex
figures.

---

## 6. Newly logged channels that nothing reads

**Status:** mostly closed. Wear, braking and body attitude all have
readers now. `heading` and `damage` do not.

Schema v8 and v9 added thirteen columns — `pos_x/y/z`, `heading`, `pitch`,
`roll`, `tc_active`, `abs_active`, `wear_fl/fr/rl/rr` and `damage`. For a
while only position had a reader, which is the same failure as not logging
them, one step later.

**Where it bit:** the driver asked whether her tyres had gone off during a
Sebring race. The answer given was argued from hot pressure and core
temperature — proxies — while the game's own wear figures sat unread in the
same rows. She also asked whether ABS was aggressive enough, and there was
no measurement of any kind to answer with.

**What changed:**

- `stint_wear` — remaining and used per corner per lap, the rate per lap,
  and whether that rate rises across the stint. That last part is the
  distinction that matters: every tyre accumulates wear, and only a rising
  rate is what a driver means by "they went off". Reported counting *up*
  from zero, because AC counts down from 100 and a number that falls as
  the tyre worsens gets misread once and trusted forever. **Per stint, not
  per session** — the laps are cut at out-laps, pit visits and any point
  where remaining wear goes back up (`analysis.split_stints`), because a
  session deliberately keeps pit laps and can span a tyre change. Reported
  whole, it would difference the first set's start against the last set's
  end and trend two different sets as one. Wear rising *within* one lap
  is a boundary too: the set changed part-way through a lap the flags
  missed, its own delta is negative, and `pitted` is only *inferred* for
  laps migrated from before v10 — so the wear is checked rather than
  trusted. An out-lap that reaches a stint counts towards the total (the
  rubber came off) but not towards the per-lap rate or the trend: it is
  shorter and always first, so averaging it in depresses the early half
  and manufactures the rising rate the report exists to detect. Note that
  at tracks where the lap counter ticks over inside the pit lane, the
  out-lap also carries `pitted` and is held out as a boundary instead —
  so on those tracks its rubber is in no total at all.
- `braking_report` — what the tyres did under braking: slip per axle under
  straight-line hard braking, which axle is nearer its limit, and runs
  where a front wheel ran away. It does **not** measure ABS or TC
  intervention; see below for why that is not available. Lockup runs are
  found by walking the lap in recorded order, not by walking the filtered
  braking samples — in the filtered list two one-tick spikes from braking
  zones half a lap apart sit next to each other and read as one run.
- `attitude_report` — roll and pitch in degrees, and the **roll gradient**:
  degrees of body roll per g of lateral acceleration. A direct measure of
  total roll stiffness, so an anti-roll bar or spring change either moves
  it or did not do what it was meant to. Every bar argument this project
  has had was previously settled by reasoning from load transfer.
  `dive_deg_per_g` is the same idea for braking. Withheld, with the count
  said out loud, when the car was never loaded enough for a slope to mean
  anything.
- `db.lap_endpoints` reads two rows per lap instead of a whole trace, so a
  stint report does not pull forty thousand dicts to obtain eight numbers.

All three distinguish *no measurement* from *a measurement of zero* —
pre-v8 and pre-v9 laps say so rather than reporting a confident zero, and
a lap with no straight-line braking refuses to judge rather than reporting
a spotless one.

### The attitude fields are absolute, and that nearly went out wrong

`roll` and `pitch` are the body's angle to the **world**, not to the road.
They carry banking, camber, road grade and static tilt and rake. The first
cut of `attitude_report` fitted `|roll|` against `|lateral g|` through the
origin, which measures none of those out: a car with a constant lean and
no suspension travel at all still produced a confident positive gradient,
and taking absolutes folded roll *opposing* the load onto the same side as
roll with it, so a sample contradicting the fit was counted as supporting
it.

It now fits **signed** attitude against **signed** g with a **free
intercept**, reports the slope magnitude as the gradient, and reports the
intercept separately as `static_offset_deg` — the attitude at zero load,
which is not suspension movement. `fit_r2` says whether a line described
the lap at all, and a lap loaded in only one direction says that the
intercept is extrapolated rather than bracketed.

**What that fixes and what it does not.** An intercept removes the
*constant* part of the track: static tilt, rake, a level banked section a
car sits on all lap. It cannot remove banking that rises with cornering
load, because that is correlated with lateral g and lands in the slope by
construction. So the roll gradient is a **car-and-track** number. Compare
it between laps on one track; treat a cross-track comparison as rough.
The payload says this rather than implying the intercept cleaned the
track out. `dive_deg_per_g` fits every sample at or below zero
longitudinal g — squat under power is a rear-suspension question and does
not belong in a dive figure, and the cut is at zero rather than at the
braking threshold because the near-zero samples are what pin the
intercept. A lap that never got off the brakes says so, the same way a
one-direction lap does for roll.

Which sign of `roll` means "leaning out of the corner" is AC's
convention and **has never been verified here**, so no direction is
claimed. The signed slope is reported as `fitted_slope_deg_per_g` next to
the magnitude: what is usable is that the sign should agree across every
lap of a car, and a lap that disagrees with its neighbours is the anomaly.

### The aid fields are not what they look like

`abs_active` and `tc_active` were added believing them to be the amount of
intervention happening right now. **The first real lap falsified that:**
both were constant to three decimal places across 3024 samples — down every
straight and through every braking zone alike. Nothing measuring
moment-to-moment intervention behaves that way. They are a setting or a
slip threshold, and an `electronics_report` built on the wrong reading was
replaced by `braking_report` before it could mislead anyone twice.

The real flags exist. CSP's `ac.CarState` carries `absInAction` and
`tractionControlInAction`, both booleans — but marked **physics-only**,
which means a physics worker, which CSP forbids online. Exactly the
constraint the damper histograms already live under, and the same
worker/app tier split would apply.

So `braking_report` answers "is ABS aggressive enough" the way it can be
answered from stored data: slip per axle under **straight-line** hard
braking, which axle is nearer its limit, and runs where a front wheel ran
away. The steering filter is load-bearing — slip under combined braking and
cornering is high by construction, and without it every trail-braked entry
reads as a lockup. `LOCKUP_SLIP` is provisional and has never been
calibrated against a lockup someone confirmed from the cockpit; the raw
distribution is reported alongside it for that reason.

**Still open:**

- **`heading`** — stored, unread. Only interesting alongside position, for
  yaw relative to the racing line.
- **`damage`** — stored, unread. Low value while the driver races with
  damage disabled, where it stays at zero all session. The wall detector
  that works either way is a speed discontinuity; see item 8.
- **The game's own aid flags**, via a physics worker, single-player only.

---

## 7. Display-mapping registry

**Status:** built, in schema v13 (`display_observations` and
`display_notes`). What remains is filling it in, which only happens at the
setup screen.

`setup_ranges` reported `show_clicks_mode` and `display_multiplier`, and
`write_setup` printed things like `"0 (click index, mode 2)"` — the tool
admitting it did not know what the setup screen would say, in a format that
reads like an answer. It then got repeated back as one.

**Corrections that were needed, and what handles each now:**

| Field | Guess | Truth | Handled by |
|---|---|---|---|
| Camber (F4) | −25 out of range | −25 *is* the maximum | game ranges (fixed earlier) |
| Toe (F4) | rear toe-out | front axle display is negated | slope −0.01, two readings |
| Rod length (F4) | ~1 mm per click | a fraction of a mm | slope from two readings |
| Toe (NSX) | stored 0 = 0.00° | stored **10** = 0.00° | offset from ONE reading |
| Traction control | unknown direction | **1 = most**, 11 = least | a note, not a number |

**What was built:** `display_observations` holds (car, field, stored →
displayed) pairs read off the screen; `display_notes` holds what is not a
number. `setups.fit_display` turns them into a line — two distinct stored
values give slope and offset outright, one borrows the game's multiplier as
the slope and fits only the offset, which is the NSX case and costs one
number. `record_display_value`, `record_display_range` (both ends of the
spinner at once) and `forget_display_value` are the tools; a misreading has
to be undoable, because a mapping fitted from one is stated with confidence.

Everything reported now carries a `source` — `observed`, `game`, `stored` or
`unknown` — and `unknown` states no value at all. That is the actual fix:
the registry gives it somewhere to put the truth, and the `source` field is
what stops it inventing one in the meantime.

**Still open:** nothing populates this automatically. CSP exposes
`displayMultiplier` and `showClicksMode` but no formatted display string —
`acc-lua-internal/lua-module/src/_hotlap_utils.lua:265` does
`e[1] * sd.displayMultiplier` itself, the same arithmetic and the same
exposure to being wrong — so a reading has to come from a driver looking at
the screen. Worth revisiting if CSP ever exposes the rendered value.

---

## 8. Smaller, known, lower stakes

**Brake-point detection is unstable between laps.** `_brake_zone_start`
reported a 0.036-of-a-lap difference at Suzuka's Spoon — about 210 m — for
two laps whose apex speeds differed by 1.2 km/h. Almost certainly two
different braking events being matched. Never chased.

**The outlier rule lets scrappy laps through.** Sebring lap 160 (2:10.161
against a 2:02.7 best) stayed `valid`, and blew that comparison's lap-time
resolution out to 11.5 seconds. It is 7% off the pace, under
`OUTLIER_FRACTION`, working exactly as designed, and still ruining results.
Consider a separate "representative lap" notion distinct from validity.

**Wall detection.** `carDamage` is now stored but reads zero all session when
the server has damage disabled, which is this driver's normal setup. The
detector that works either way is a speed discontinuity — a wall removes
100 km/h in a couple of samples and nothing filters that. Note the tension:
`_sane_channel` drops accelerations above 6 g as glitches, and a genuine
impact exceeds 6 g, so the crash and the artefact currently look identical.
Damage rising would disambiguate them when damage is on.

**Corner detection is better, not fixed.** One-sided corners fell from 7 of
17 to 3 of 18 after the shared lateral-g reference (schema-independent, in
`analysis.lat_g_reference`). Not zero.

**`_migrate` v1 unguarded ALTER** — fixed. All six ALTER sites now go through
`_add_column`, which no-ops on a missing table. Listed only so nobody
re-derives it.

---

## Measured baselines — NSX GT3 Evo on `claude_sebring_v9`

Taken on Sebring. Most are properties of the car and the setup rather than
the circuit, so they carry to the next track — but see the warnings: the
gradients are car-and-track, not car alone.

> ⚠️ **The gradients and the wear figures below were produced by code
> this branch has since corrected, and neither has been re-run.** They are
> kept because the raw laps are still in the database, so re-running them
> is a session's work rather than a re-drive. Each carries a warning
> saying what is wrong with it; do not quote either to the driver until it
> has been re-run. The brake-bias and `abs`-field observations are raw
> reads from code this branch did not change, and stand as written.

**Roll gradient 0.723 deg/g. Peak roll 3.54°. Dive 0.513 deg/g.**
(`attitude_report`, lap 202.) That is a stiff car — with the front
anti-roll bar on its minimum stop and the rear only one step up. The
115/120 N/mm springs are doing nearly all the work in roll.

> ⚠️ **Both gradients came from the origin-forced fit on absolute
> attitude, which counted static tilt and rake as roll and dive.** Peak
> roll stands; both gradients need re-running on lap 202. Re-run, they
> are car-and-track numbers rather than properties of the car: Sebring's
> banking that rises with cornering load stays in the slope even under
> the new fit, and so does road grade that varies with where she brakes.
> The *conclusion* — a stiff car, springs dominating the bars — rests on
> the setup sheet (bar on its minimum stop, 115/120 N/mm springs) as much
> as on the gradient, so it is the part least likely to move. That is an
> argument, not a measurement.

**This reframes a lot of the season's reasoning.** Weeks were spent
arguing about anti-roll bars on a car where they are a minor term on top
of the springs. If the question is how the car rolls, spring rate and ride
height carry the authority. And every future bar or spring change is now a
one-number test: the gradient moves, or the change did not do what it was
meant to.

**The front is the axle nearer locking** under straight-line braking, on
every lap examined, with `FRONT_BIAS` at 60. Consistent, and the lever if
that ever needs changing.

**The `abs` field is not constant across sessions.** Held 0.09 through
practice and qualifying, 0.06 through the race, on an unchanged setup
file. Constant *within* every lap, so it is a setting or a threshold, not
activity — but something moved it between sessions and nobody knows what.
The driver independently reported ABS feeling less aggressive in the race.
Worth asking, next time, whether the rotary was touched or the server
changed it. Unexplained is not the same as unimportant.

**Wear at racing pace: about 0.05%/lap front, 0.024%/lap rear**, rising to
~0.055% rear late in a stint when the driver started carrying more speed
through slow corners. Total over a 13-lap race: 0.7% front, 0.44% rear.
Tyre life is a non-issue for this car over a sprint distance; do not spend
setup effort on it.

> ⚠️ **These three figures — the rate, the total, and the "rising late"
> — are exactly the three outputs the un-segmented `stint_wear` got
> wrong.** They were taken across a whole session, before it cut laps
> into stints, and an out-lap counted as a full lap in the rate, which
> biases the early half low and manufactures a rising trend. Re-run
> per stint. The *conclusion* — 0.7% over a race distance is nothing,
> so tyre life is not worth setup effort — survives an order of
> magnitude of error in either direction, which is why it is still here.

---

## Driver context worth keeping

Preferences that have proven stable across two cars and three circuits, and
that should bias setup work:

- **Predictability over peak grip.** A tenth of ultimate pace is worth
  nothing if it cannot be leaned on. Zero toe suited her on both cars.
- Fast in cars that reward carrying speed and precision. Dislikes heavy,
  powerful, aero-dependent cars and turbos — the common thread is
  **linearity**: a constant relationship between input and response.
- Reports feel accurately and catches over-claims. When she says a change
  did nothing, believe it before believing a p-value.
- Diagnoses she found herself that the tooling missed: the wider line at
  Sunset Bend, the TC direction, the toe mapping, the AC spring-rate ladder.
