# Backlog

Known gaps and bugs, ranked by whether they destroy data the driver has
already produced. Each entry says what is wrong, where it bit, and where the
code lives, so a future session can act without re-deriving any of it.

Written after the Sebring / NSX GT3 session. Test suite stood at 252 passing,
schema at v9.

---

## 1. Lap validity is inferred, not read

**Status:** open. The highest-stakes item here — it is the only one that
silently discards laps the driver has already driven.

`collector._loop` decides validity with:

```python
if p.numberOfTyresOut > 2:
    lap_dirty = True
```

That is a *proxy* for AC's own judgement, not AC's judgement. Vanilla AC does
not expose lap validity in shared memory — `sim_info.py` maps `penaltyTime`,
`flag` and `isInPitLane`, and there is no `isValidLap` field the way ACC has
one.

**Where it bit:** Sebring, lap 129. A 2:06.769 that the game showed as valid
(not red on the driver's timing display) was stored `valid: 0`. Sebring is
ringed with wide flat kerbs and painted apron that put three wheels outside
the surface without the game calling a cut.

**Why it matters:** `compare_runs` drops invalid laps. A lap wrongly flagged
is a lap deleted from every analysis, and the driver is never told.

**Possible fixes, roughly in order of preference:**

- Check whether CSP exposes a real validity flag (it extends the shared
  memory layout). If it does, read it and keep the proxy only as fallback.
- Record *how much* the lap was off rather than a boolean — `tyres_out` is
  already stored per sample, so the count and duration of excursions is
  recoverable without any schema change. Let the reader decide.
- At minimum, rename the reported field so it stops implying the game said
  so: `valid` → something that reads as "3+ wheels off track at some point".

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

**Status:** proposed, never built. Cost three driver corrections in one
evening.

`setup_ranges` reports `show_clicks_mode` and `display_multiplier`, and
`write_setup` prints things like `"0 (click index, mode 2)"` — which is the
tool admitting it does not know what the setup screen will say. It then gets
guessed at anyway.

**Corrections needed so far:**

| Field | Guess | Truth |
|---|---|---|
| Camber (F4) | −25 out of range | −25 *is* the maximum |
| Toe (F4) | rear toe-out | front axle display is negated |
| Rod length (F4) | ~1 mm per click | a fraction of a mm |
| Toe (NSX) | stored 0 = 0.00° | stored **10** = 0.00° |
| Traction control | unknown direction | **1 = most**, 11 = least |

**Fix:** a small table keyed on (car, field) storing observed
(stored_value → displayed_value) pairs. Two points define a linear mapping.
`write_setup` then reports the real displayed value, and refuses to guess
when it has no mapping rather than guessing anyway.

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
