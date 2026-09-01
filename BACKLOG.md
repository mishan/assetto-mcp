# Backlog

Known gaps and bugs, ranked by whether they destroy data the driver has
already produced. Each entry says what is wrong, where it bit, and where the
code lives, so a future session can act without re-deriving any of it.

Written after the Sebring / NSX GT3 session, and kept current since. Test
suite stands at 398 passing with the Lua tooling installed (21 of them
skip without lupa), schema at v12.

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

**Status:** open, and self-inflicted.

Schema v8 and v9 added thirteen columns — `pos_x/y/z`, `heading`, `pitch`,
`roll`, `tc_active`, `abs_active`, `wear_fl/fr/rl/rr` and `damage`. Only
position has a reader (`analysis.driving_line`). The rest go into the
database and no analysis touches them.

Highest value first:

- **Tyre wear** — degradation across a stint, and the basis of any real pit
  strategy. Changes every lap regardless of server settings.
- **`tc_active` / `abs_active`** — whether the electronics actually
  intervene, rather than what the setup screen is set to. Would have settled
  the TC argument at Sebring by measurement instead of by asking the driver.
- **`roll`** — direct body-control measurement. Anti-roll bar and ride
  height arguments have all been indirect so far.

---

## 7. Display-mapping registry

**Status:** built, in schema v12. What remains is filling it in, which only
happens at the setup screen.

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
