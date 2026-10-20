# The assetto-mcp tools, by the question they answer

Thirty-eight tools. They fall into nine groups. For each: what to pass,
what to read in the reply, and the traps.
Every optional parameter takes null as "the default", so passing one
explicitly is always safe; a call refused for a missing parameter is fixed
by adding it, not by skipping the call. Every parameter that takes several
lap ids takes the same shapes: one id, a list, "87,88", "87 88" or "87-90".

## Contents

1. Recording and state
2. Setup attribution
3. Reading one lap
4. Comparing laps and runs
5. Tyres, brakes, suspension, fuel
6. Opponents and contacts
7. Setup files and the setup screen
8. The driver's side: notes, messages, the bridge
9. The server itself

---

## 1. Recording and state

**`session_check()`** -- the session-start call. `recording` (`state` as
`recording_status` reads it), `game` (what shared memory says now),
`session` (id, car, track, laps, `best_clean_lap_id`, `setup_name`,
`measured_setup`), `setup_on_car` (`identify_setup`'s match or
candidates), `app` (`connected` and `last_seen_s`: the in-game app polls
every 2 s, so more than 10 s means it is not talking to this server),
`suspension.tier` ("worker" 333 Hz, "app" render-rate, null nothing yet),
`server` (`restart_needed`, `last_update_check` from the cache; it never
asks GitHub), and `notes` (the folder, and whether `driver.md` and the
car's file exist). `to_do` is the list to work through before the first
flying lap. An `app.bridge_error` with another chat open means that chat's
server holds the in-game app: data still arrives, but
`send_driver_message` from here does not reach the driver. `storage` is
the database's size against its budget, and how many laps were thinned to
stay under it: `sample_stride` above 1 on a lap means its trace was
decimated; numbers are intact, resolution is not.

**`start_recording()`** -- idempotent; recording starts with the server
anyway, so this mostly undoes a `stop_recording`. If another instance holds
the recorder this one stands by and says so. `session_check`'s `to_do`
says when it is needed.

**`recording_status()`** -- read `state`, not `running`: "never_started" and
"stopped_by_request" look identical otherwise. "standby" is healthy.
`session_id` is the id to tag and to watch. `setup_name` shows the current
tag; `measured_setup` is the fingerprint of the values the in-game app reads
off the setup menu right now, and it changes when the setup does. "waiting for AC to go live" means the car is in the pits, the menu, or
between sessions.

**`stop_recording()`** -- durable and shared across instances. Do not call it
casually; it stays off until `start_recording`.

**`live_snapshot()`** -- car, track, layout, session status, air and road
temperature, `fuel_l`, tyre pressures and core temperatures right now, last
and best lap. Poll it at each lap boundary for fuel, and whenever you want
the tyre state between laps. `status: "off"` means the game is not in a
session. The `tyre_compound` reads garbage when the car is not live.
Judge fuel against `laps_remaining`, not against the next lap or two:
`fuel_to_finish` does that sum, and its `margin_laps` is the number to
call ("enough to the flag, 1.5 laps spare"; "short by half a lap").
`basis` "measured" is the session's median burn over its usable laps
(three or more, `measured_laps`); "nominal" is the car's rated
consumption, which traffic and lifting move. "tight" is under 0.6 laps
spare: on a nominal basis, say that the real burn is not yet known.

**`list_sessions()`** -- id, car, track, lap count, best time. Every tool
that reports a position adds meters and a `where` beside it, and says
where the track length came from in `track_length`; see "Positions into
meters" in `analysis.md`.

**`list_laps(session_id, limit, all_sessions)`** -- the active session's
laps (every session's when none is recording, or with
`all_sessions=True`), most recent first, with ids for the lap tools: lap
number, `lap_time`, `setup_name`, `usable_for_timing` (with
`not_usable_because`), `ran_wide`, `outlier` (far off the session's pace,
still usable) and `completed_at` in local time.

**`wait_for_lap(after_lap_id, timeout_s)`** -- the live loop for a client
that cannot run `scripts/watch_laps.py` in the background. Waits up to
`timeout_s` (at most 45) for a lap after `after_lap_id` (null: the newest
stored now) and returns its card: `lap_id`, `lap_number`, `lap_time`,
`usable_for_timing` (`not_usable_because`), `ran_wide`, `outlier`,
`setup_name`, `fuel_used_l` (null when the lap did not start at the line),
`contacts` as counts per verdict (null: no acceleration data; read
`lap_summary` before naming a cause), and the session's
`best_clean_lap_id`. `new_lap: false` is a timeout, with the recorder's
`recording` state: call again with the same `after_lap_id`. A card with
`more_waiting` has later laps behind it: call again with its `lap_id`.
Laps in a later session count too, so a new session id shows up here.

## 2. Setup attribution

Nothing in shared memory says which setup file is on the car. Attribution is
a claim, made three ways by how much damage a wrong one does.

**`identify_setup(session_id)`** -- matches the live setup-screen values
against every saved file for this car and track. Needs the in-game app to
have seen the setup screen. Reports several candidates when files are
identical, and the car's setup legality. Pass `session_id` explicitly.

**`set_session_setup(setup_name, session_id)`** -- forward only. Laps from
now carry the name; nothing already stored changes, including unlabeled
laps. Call it when the driver says they loaded something, after every pit
stop that changed anything, on every new session id, and for on-wheel
changes with a suffix (`claude_v15 bias58`).

Back-filling would be wrong: the baseline run is normally unlabeled too,
and stamping the new name on it destroys the A/B being set up. Call it
before the first lap on the new setup where you can.

Every lap also carries `setup_fp`, the setup it was measured on, and the
name only follows laps on the values it was bound to: the first lap after
the call binds it. A setup changed later without a call leaves the laps on
it blank rather than wrongly named, and a return to named values takes that
name back. `unlabelled_laps_on_the_setup_measured_now` in the reply lists
earlier laps already on the car's current values -- the laps to offer to
`label_laps` when the call came late. `measured_setup_now: null` means the
app is not reporting, and the name is unchecked.

**`label_laps(lap_ids, setup_name, session_id)`** -- fills only laps with no
setup recorded, only the ids named. Ask the driver which laps. Where the
setup was measured, ids spanning two measured setups are refused whole
(`laps_by_measured_setup` shows the split -- that is where the garage stop
was), and laps measured on other values than the laps already carrying the
name are left alone (`measured_on_a_different_setup`). `lap_ids` is
"87,88,89,90", "87-90", a single id, or a list of ids.

Genuinely wrong labels are fixed with `scripts/relabel_laps.py`, on purpose
not a tool.

## 3. Reading one lap

**`lap_summary(lap_id, detail)`** -- the workhorse. The default
`detail="summary"` is the lap read; `detail="full"` adds what explaining a
bad lap needs (`entry_phase`, front and rear slip, steer, entry and exit
points, the fractions, `corner_detection`). Read in this order:

- `usable_for_timing` and `not_usable_because`; `ran_wide` and
  `track_limits` (wheels out, excursions, milliseconds off).
- `contacts_inferred` (see group 6) before anything else about a bad lap.
- `corners[]`: each has `turn` (the session's label, quote this), `name`,
  `direction` ("left" or "right"), `gear`, `min_speed_kmh`, `apex_m`,
  `brake_before_turn_in_m`, `throttle_on_m`, `slip_balance` (positive front
  slides more), `peak_lat_g` and `body_slip_peak_deg`. Full adds `corner`
  (this lap's ordinal, do not quote), `turn_sign`, the `_pos` fractions,
  `peak_steer_norm` (fraction of full lock, not degrees), `front_slip`,
  `rear_slip` and `entry_phase` (from brake point or turn-in to apex: slip
  balance, mean steer, yaw peak, rotation, body slip). `body_slip_peak_deg`
  is how far the nose points inside the car's path. A GT3 car corners at 1-5
  degrees at the apex, up to 7 on entry; tens of degrees is a slide caught,
  large and negative a spin, and a slip balance that reads understeer next
  to a large body slip is a car that rotated. Entry phase is where trail
  braking, entry rotation and most spins live; a change aimed at entry
  stability shows here when the apex figures do not move. Yaw and rotation
  are null on laps recorded before heading. `brake_before_turn_in_m` is the
  board to brake at.
- `tyres`: pressure average and end-of-lap, core temperature average, per
  corner of the car.
- `top_speed_kmh`, `time_full_throttle_pct`, `time_braking_pct`,
  `time_coasting_pct`, `peak_lat_g`, `peak_braking_g`.
- `overall_slip_balance` and `slip_quality`; `accel_samples_dropped`
  (spikes past 6 g dropped from the peaks; they are still in
  `contacts_inferred`).
- `corner_detection` (full): which bar the corners were found against.
  With one lap in the reference it is that lap's own peak, and every other
  lap is judged against it; the numbering settles after a few laps.
- `suspension` headlines when the app captured them; `chassis_report`
  for the rest.

Quote `where` and the `_m` fields, never the fraction beside them.

**`track_corners(session_id)`** -- where each T-number is: start, apex, end,
brake point, direction, and how many laps cornered there. Built per session
from the laps driven, so the numbering is the session's, not the circuit's:
a flat kink is not numbered, and 3A is numbered straight through. The
numbering is also not the same in `compare_laps` (built from those
two laps only) or `compare_runs` (built from the laps compared). Quote the
label with the tool that produced it, and prefer the circuit `name` or the
driver's own name for a corner when the labels disagree. Each turn carries
`name` when the track ships a sections.ini, and `circuit_names` lists every
named section with the session turns inside it; a named section with no
turns is a kink the detector never saw. The comparisons carry apexes only,
so they name a corner by the section its apex is in or just beside: a
corner whose apex sits near a section boundary can come back with no
`name` there, or a different one from `track_corners`. `unnumbered` holds
pieces of road only one lap cornered on, kept out so that a spin carved
out as its own corner does not renumber every turn after it.

**`track_corners(session_id, meters, fractions)`** with positions -- any
position you found outside the tools (a script over `samples`, a rival
trace) said the way the tools say it: "Turn 10A braking zone, 60 m before
turn-in", in `places` and in place of the turn table. `meters` is distance
past the start line, comma-separated; `fractions` is `norm_pos`. Use it
before quoting a position from a script to the driver.

One lap's line across the track is `compare_laps` with `view="line"`
and no `lap_id_b` (group 4).

**`export_line_map(session_id, lap_ids)`** -- one HTML file in the data
directory's `exports/` folder with every lap drawn, best lap picked out,
coloring by setup or speed or pedals, tyre and rev charts, opponents'
brake points. `lap_ids` takes the usual shapes, or null for the whole
session.
Hand the path to the driver; the file opens from disk and fetches nothing,
and exporting the same session again replaces it.
`scripts/driving_line_map.py` does the same from a shell.

**`chassis_report(lap_id, aspects, points, lap_number, session_id)`** --
how the car behaved on one lap, by `aspects`: any of "braking",
"attitude" and "suspension", default the first two. An aspect that cannot
be read carries its own `error` and leaves the others.

- "braking": slip per axle under straight-line braking (`points`
  slices), which axle is nearer its limit (what brake bias moves),
  lockup runs. The ABS and TC fields hold one value all lap and measure
  nothing, so "is ABS aggressive enough" cannot be answered from them or
  from the setup value; the axle slip is what the driver feels. The
  straight-line filter matters: slip under combined braking and
  cornering is high by construction, and every trail-braked entry would
  read as a lockup. The lockup threshold is uncalibrated; read the
  distribution.
- "attitude": roll and pitch per g. The roll gradient measures total
  roll stiffness: a bar or spring change should move it, and one that
  does not did not do what it was meant to. A car-and-track number: AC
  reports attitude against the world, and the fit's free intercept moves
  the constant part of banking, camber and rake into
  `static_offset_deg`, but banking that rises with cornering load stays
  in the slope. It was useless at Kyalami and Interlagos and is only
  comparable between laps at the same circuit. Check `fit_r2` before
  quoting a gradient. Squat under power is left out of `dive_deg_per_g`;
  it is a rear-suspension question.
- "suspension": damper histograms, ride height, roll balance.
  `lap_number` is for a lap that was not stored (an out-lap). Read
  `source`: "app" is render-rate and the damper histograms are body
  motion, not valving; "worker" is 333 Hz and real. `session_check`'s
  `suspension.tier` says which tier is arriving. The worker is
  single-player only: CSP runs no scripts on the physics thread online.

**`lap_trace(lap_id, channels, from_m, to_m, step_m)`** -- the raw trace
for what no report answers: shift rpm by gear, time in 6th, the slip
ratio through one braking zone, entry speed against the brake point.
`channels` is a list of sample columns (`speed_kmh`, `gas`, `brake`,
`steer`, `gear`, `rpm`, `acc_lat`, `acc_lon`, `slip_*`, `press_*`,
`core_*`, `ride_f`/`ride_r`, `tyres_out`, `pos_x/y/z`, `heading`, `pitch`,
`roll`, `tc_active`, `abs_active`, `wear_*`, `damage`); the default is
speed, pedals and gear. `from_m`/`to_m` are meters past the start line
(the whole lap by default) and a row comes every `step_m` meters, at most
300: a longer window gets a coarser step, and `step_m_requested` says so.
Each row has `m` and `t_s` since the lap started. `from_where` and
`to_where` place the window; any other `m` goes through `track_corners`
before it goes to the driver.

## 4. Comparing laps and runs

**`compare_laps(lap_id_a, lap_id_b, view, detail, segments, points)`**
-- two laps, one `view` at a time. Read `lap_time_warning` before quoting
a lap-time difference. An out-lap or a lap that ended in the barrier still
has real corner speeds and a real line, so any two laps compare.

- `view="corners"` (the default) -- corner by corner: min speed delta,
  brake point delta in meters, slip balance delta. Deltas are a minus b; a
  negative `min_speed_delta_kmh` means lap b carried more speed, and
  `a_slower_by_ms` is lap a's time minus lap b's. Its turn numbering is
  its own; `detail="full"` lists it in `turns`.
- `view="along_lap"` -- where the time went, including straights and
  sweepers no corner detector flagged. Read `b_lost_ms` per segment
  (positive: lap b lost time there); `b_lost_so_far_ms` and
  `b_slower_by_ms` only say a gap exists. This view subtracts lap a from
  lap b, the other way round from the corners; the key names say which
  lap is slower. 1 to 200 `segments`.
- `view="line"` -- where the car was across the track, slice by slice
  (`points`), with speed, ride height and a bump map. With `lap_id_b`,
  `separation_m` says how far apart the two lines were at each point: the
  answer to "was that a wider line". Without it, one lap's line.

**`compare_runs(baseline_laps, candidate_laps, clean_laps_only,
include_invalid, baseline_setup, candidate_setup, session_id, detail)`**
-- the A/B judge. The default `detail="summary"` has the verdicts and
every warning; `detail="full"` adds `corner_leads` and the method notes.
Each side is a lap id list, or a setup name: `baseline_setup` /
`candidate_setup` take every lap in the session labeled with it, and
`laps_by_setup` lists which: a name is only as right as the labels, so
check that list before quoting the result. Read:

- `metrics[*].verdict`, `p_value_adjusted` against 0.05 (the family is
  corrected together), `change`, `resolution` (smallest change these laps
  could catch half the time), `power` (chance of catching the change
  actually measured). "Suggestive" means run more laps, not "no".
  The family is up to eight means plus `lap_time_consistency`; a channel
  the laps never carried is not counted, and `tests_in_family` is the
  number the correction used. Only a metric may be described as having
  moved. A large `resolution` means the run was too short, not that the
  change did nothing.
- `lap_time_consistency`: needs six a side to say anything.
- `corner_leads`, with `detail="full"` only: exploratory, uncorrected,
  about 5% of quiet corners show up, and with up to five channels a corner
  most runs with nothing changed carry at least one (`corner_leads_note`
  gives the figure). Where to look when a metric moved; never a finding.
- `lap_time_set_aside`: with three or more laps a side, laps more than
  1.75% slower than their side's median (a moment, or a slow first lap),
  left out of the lap-time mean so one such lap does not set the spread.
  They stay in `lap_time_consistency` and the corners. A side with many
  set aside was not driven consistently enough to judge pace.
- `excluded_laps` with reasons: laps left out. `ran_wide` and `contacts`:
  laps counted but flagged (with `clean_laps_only`, ran-wide laps move to
  `excluded_laps`). A result resting on a contact lap may be measuring
  the hit.
- `baseline_setups` and `candidate_setups`: check the tags are what you
  think they are before reading anything else.
- `measured_setup_warning` (`same_measured_setup: true`): both sides were
  driven on identical values. The change never reached the car; nothing
  below is its effect. Say so before any metric.
- `setup_changes`: the entries that actually differ between the two
  measured setups, fuel aside. Check it matches the change you meant.
  `measured_setup_note` says when a side spans two setups or has laps
  nothing measured.
- `conditions_note`: the sides' sessions started at different road
  temperatures or ran different compounds; part of any difference may be
  the track. It cannot see the track changing within one session.
- `experiment_id`: the comparison is filed in the setup book under this
  id. Annotate it once the driver has said what they felt.

Both sides must be the same car on the same layout. `clean_laps_only=true`
drops ran-wide laps (worth it when the question is a clean lap time); it
does not drop contact laps. `include_invalid=false` is honored as
`clean_laps_only=true`, and the reply says so.

What a "within noise" is worth, against this driver's own spread: a
2.2-point front load transfer change (a rear bar, against 0.3 of noise) is
caught 29% of the time at two laps a side and 97% at three. A 500 ms lap
gain against a 0.25 s spread is caught 3% at two laps, 11% at three, 39%
at five and 77% at eight. Two laps a side is a real test on a quiet
channel and none on lap time; a null lap-time answer from under five or
six laps is not evidence the change did nothing.

**`setup_book(car, entry, track, change, effect, metric)`** -- every
stored A/B for the car, by setup entry. Read it before proposing a change.
Per entry: the latest eight experiments that changed only that entry (with
the driver's `conclusion` and `note`; `experiments_not_listed` counts the
rest), and per metric the effect `per_unit` of the stored value
(`per_click` when the step is known) pooled across all of them, with `se`. `within_noise: true`
means the sign is not settled: say so, and never quote it as what the
entry does. `tracks_disagree`: the layouts differ by more than noise;
quote `by_track`, not the pool. Comparing laps that overlap an earlier
comparison replaces it, so re-running one as laps come in is safe.
`changed_together`: the latest eight A/Bs that changed several entries at
once, which cannot be attributed to any one of them. `track` is the track
folder name and takes in every layout.

Its `plan` -- laps a side for an 80% chance of seeing a change, from this
driver's own spread in stored A/Bs -- comes with the book when you give
`effect` in the `metric`'s units, or `entry` and `change` to take it from
the book; `warning` says when the book's effect is itself within noise. If `laps_per_side` is more than the driver will drive,
test a quieter metric (load transfer, slip balance) or a bigger change,
and say so before the run, not after.

**`annotate_experiment(experiment_id, note, conclusion)`** -- after an A/B,
what the driver felt and whether the change was "kept", "reverted" or left
"undecided". The setup book quotes it back.

## 5. Tyres, brakes, suspension, fuel

**`stint_wear(session_id, from_lap, to_lap)`** -- remaining and used per tyre
per lap, the rate, and whether the rate is rising. Segmented into stints
at out-laps, pit visits and tyre changes. `from_lap`/`to_lap` are inclusive
lap numbers; leave both out for the newest 200 laps. Wear, not pressure,
is the evidence a tyre went off: hot pressure and core temperature move with half a dozen
other things. A steady wear rate is not a tyre going off; only a rising
rate is. A session is not a stint, which is why it segments: differencing
the first set's start against the last set's end can call a healthy tyre
gone. AC counts remaining down from 100; `used` counts up.

**`fuel_plan(race_laps, stops, session_id, liters_per_lap)`** -- the burn
per lap is, in order: `liters_per_lap` when passed (the in-game
estimator's figure from the driver), the session's measured median once
three usable laps measured it, or nominal from the car's `km_per_liter`,
which the in-game app rarely has. `liters_per_lap_source` says which; quote
it with the plan. With none of the three it refuses, and `do_this` says
how to get one. Tank size is in `setup_ranges` under FUEL.

## 6. Opponents and contacts

Opponent data comes only through the in-game app's bridge; single-player
has none.

**`list_rivals(session_id, limit)`** -- car index, driver name, car model,
best lap, how many of their laps were well covered. Pass a session id.
Best laps are timed off each car's own trace (`laps_timed_from_trace`);
`server_best_lap_ms` is the server's claim and is not to be quoted. The
server's remote-car lap times lag and repeat: at the Glen they ranked the
driver ahead of two cars that were quicker. Each captured lap carries
`lap_time_source`, "trace" or "server".

**`compare_to_rival(car_index, lap_id, rival_lap_number, session_id,
lap_ids, car_indexes, laps_per_rival)`** -- one rival with `car_index`,
the whole field without it.

With `car_index`: where they carry more speed by track position
(`rival_faster_by_kmh`); their brake and throttle points when the server
transmits them (`rival_input_fields` says which are live). Take
`rival_lap_number` from `list_rivals`' well-covered laps, quickest first,
or omit it for their quickest. `time_by_turn` is the part to quote:
seconds through each turn and on the road after it, `rival_faster_by_s`
on each (negative where the driver was quicker), with `where_they_gain`
and `where_you_gain`. The road after the last turn runs across the line
to the first turn-in, so the rows cover the whole lap and add up to the
lap difference within a few hundredths; a stretch either lap's trace did
not cover is left out, and the sum falls short by it.

Without `car_index`, the field benchmark: the coaching list. Per turn:
your median time through it and on the road
after it, the field's, `available_s` (how much quicker the field is
there), minimum and exit speeds, and brake point in meters before
turn-in, for you and the field. `priorities` sorts by time available.
The field defaults to every rival whose traced best beats yours, each on
their three quickest laps within 2%. Their brake points read up to about
7 m early (positions arrive late) and their minimum speeds a little high
(10 Hz). Run it after practice and qualifying, not only after a race.
`lap_ids` (or `lap_id`) and `car_indexes` take "433, 434", a single
number, or a list (`lap_ids` also "433-436"); the client often sends the
text as a number or a list, and all three work.

**`contacts_inferred`** (on `lap_summary`) and **`contacts`** (on
`compare_runs`, only the `contact` verdicts) -- impacts without the damage
model. An empty list is a lap with no spike over 3 g; null is a lap with
no acceleration channels. `lap_summary`'s separate `contacts` field (from
damage) is null both for no contact and for damage switched off, so it is
never evidence of a clean lap. Each entry: `pos`,
`t_ms`, `peak_g`, `axis`, `speed_kmh`, `speed_lost_kmh`, `yaw_rate_deg_s`,
`gas`, `brake`, `input` (throttle, brake, both, coasting), `nearest`
(car index, driver name, distance, speed) and `verdict`:

- `contact`: a car within 10 m at the same instant. The radius is wide
  because opponent positions lag; confirmed hits read 6 to 8 m.
- `wall`: nobody near and 15 km/h or more gone in the instant of the spike:
  a barrier, or a car the opponent feed never carried.
  Sand, grass and ABS braking lose speed over seconds and do not qualify.
- `snap`: nobody near, speed kept, rotating over 90 deg/s. A spin, or a
  slide caught. The g is the rotation.
- `kerb`: nobody near, speed kept, no rotation. Kerbs, the floor, launches.
- `no_opponent_data`: nothing to place it against; `if_alone` gives the
  wall, snap or kerb reading it would have with nobody near.

A car parked alongside on a practice-start grid reads as a contact if the
game spikes on the reset. Distance and speed are in the entry; read them.

## 7. Setup files and the setup screen

**`list_setups(car, track)`**, **`read_setup(car, track, name)`** -- internal
folder names (`ac_friends_honda_nsx_gt3_evo`, `lilski_watkins_glen`). The
setup folder is per track, so a setup carried to a new circuit has to be
copied there by the driver, and the file name may still say the old track.

**`setup_ranges(car)`** -- legal min, max, step per entry in file units,
plus `display_multiplier`, `show_clicks_mode` and `display` with its
`source`: "observed" (read off the screen), "observed_offset" (one
reading: the zero is corrected, the scale is still the game's), "game"
(the multiplier, which has been wrong), "stored" (shows as stored),
"unknown" (say so). Reachable values are the spinner's
ladder from min and from max, not a grid.

**`write_setup(car, track, name, values_json, base_setup, overwrite,
allow_unclamped)`** -- `values_json` is an object of section to number
(JSON text also works);
unspecified sections come from `base_setup`. Clamped and snapped to what
the spinner can reach; `clamped` lists what changed. `displays_as` says
what the screen will show, with a source per entry. Refuses to reuse a
name (write under `suggested_name`, or ask; `overwrite=true` backs the old
file up first) and refuses without ranges (`allow_unclamped=true` is the
driver's call, not yours: the fix is opening the setup screen once with
the app running). Both refusals cover what is invisible from here: a
replaced hand-built setup looks like nothing from the setup screen, and AC
loads an out-of-range value, shows it, and ignores it.
Where the client can put a question to the driver itself (MCP
elicitation), both refusals become that question, and the driver's answer
decides whatever the flags say; `declined` in a refusal means they said no.
The same goes for `restart_server` mid-session and for starting
`apply_update` (`refused: "declined"`).

**`record_setup_display(field, displayed, stored, displayed_at_min,
displayed_at_max, note, car, session_id)`**,
**`forget_display_value(field, car, keep_note, session_id)`** -- what the
screen shows for a stored value. Pass what you have: `displayed` (one
reading), `displayed_at_min` and `displayed_at_max` (both ends of the
spinner, which pin scale, sign and zero in one exchange), or `note` alone.
Notes are for what is not a number: TC and ABS count 1 as the most
intervention. One reading fixes only the zero (`observed_offset`), which
is enough when the game's scale is right; ask the driver for one number,
since `stored` comes from the app. A second reading at another position,
or the two ends, makes it `observed`. Where the game reports no
scale at all, one reading is kept but leaves it `still_unknown`. Forget a
misreading at once: a wrong observation is worse than none, because the
mapping fitted from it is stated with confidence.
Known on this car: camber stored in tenths of a degree (-40 is -4.0),
FINAL_RATIO 0/1/2 shows 171/181/191 mph as 6th-gear top speed, brake bias
is percent front.

## 8. The driver's side

**`list_driver_tags(session_id, limit, all_sessions)`** -- complaint tags
pressed on the wheel (understeer, oversteer, braking, traction, note) with
a spline position comparable to `apex_pos` (with `at_m` and `where`
beside it) and the `lap_number` they were pressed on. Correlate with the
corner's slip balance and steer.

**`send_driver_message(text)`** -- one sentence on the in-game overlay. Fails
when the bridge port is held by an orphaned server; not worth chasing
during a session. Without the app (`session_check`'s `app`): no
opponents, no suspension, no setup-screen values, no messages.

**`read_notes(car)`**, **`save_notes(kind, markdown, car)`**,
**`save_handoff(markdown, name)`** -- the notes and the handoff for a
client with no filesystem tool; with one, edit the files directly. All
three stay in the data folder. `read_notes` returns `driver.md`, the car's
file (default: the session's car) and the newest handoff in `exports/`.
`save_notes` replaces a whole file (`kind` "driver" or "car"), so read,
edit and send it all back; the old file is kept as `.md.bak-<time>`.
`save_handoff` writes `exports/<name>.md` (default
`session-<id>-<track>-<car>`) and never replaces one: a name in use gets
`-2`. Hand the driver the `path`.

## 9. The server itself

**`restart_server(force)`** -- replaces the server process behind the same
connection, so new code on disk takes effect without the driver quitting
the client. Use it instead of asking them to restart; ask them first, and
never mid-lap. Refuses with `refused: "recording"` while this instance is
recording a live session -- wait for the menus, or ask; `force` is only
for when the driver has said yes. `refused: "updating"`: an
`apply_update` is still reinstalling; collect its result first, and
`force` does not override it. `refused: "not_supervised"` means the
server cannot restart itself, and then the driver does have to fully quit
and reopen the client. Afterwards: give it a few seconds, an open session
continues under a new session id (tag it again), and `session_check` may
show the port retrying for a minute or more on Windows while the in-game
app waits to reconnect.

**`check_for_updates()`** -- read-only. `status` "behind" means an update
is waiting; `commits` is what it contains, newest first, and is what you
show the driver. `needs_pip`, `needs_lua_copy` and `installer_changed` say
what applying involves. `restart_needed` means the disk is already newer
than the running server. "ahead", "diverged" and
`local_commit_not_on_github` are a checkout with its own changes: say so
and leave it. "offline" and "rate_limited" are not faults; answers are
cached for an hour and may come back `stale`.

**`apply_update(to_commit)`** -- `to_commit` is `latest` from the check.
Only after the driver has seen the commit list and said yes, and never
mid-session: a dependency reinstall can take minutes, and a live session
is refused ("recording"). A reply with `in_progress: true` is still
running: call again with the same `to_commit` to wait for the result; a
refusal "in_progress" is another apply. A
`commit_mismatch` refusal means main moved: check again and ask again. Read `pip` and `lua` in the reply and pass on any `warning` or
`lua.note` as it is -- the in-game app is not copied while the game runs.
Then `restart_server`, with the driver's OK, to load the new code.

## Outside the tools

- `scripts/watch_laps.py --session N --interval 3` -- one line per stored
  lap, this session and later ones. Run it under a Monitor so each lap
  wakes the session. `--replay` prints what is already stored.
- `scripts/driving_line_map.py` -- the line map from a shell.
- `scripts/say.py "text"` -- reads a sentence aloud over the game audio
  with the Windows voices (`--list` shows them, `--voice Zira`,
  `--rate 1`). Always pass `--wait`: without it the call returns while the
  sentence is still playing and the next one talks over it. For live mode
  only, and only for what the driver acts on this lap. Runs on Windows and
  under WSL; anywhere else it prints the sentence instead.
- `scripts/relabel_laps.py` -- the only way to overwrite a setup label.
- `scripts/rescore_track_limits.py` -- re-derive ran-wide verdicts after
  changing the wheels-out threshold in `assetto_mcp/db.py`. Skips thinned
  laps and says so: a new threshold does not reach them, so do not
  describe the pass as covering the driver's whole history.
- The running server can predate the repository's code: a field this
  file describes (`contacts_inferred`, say) may be missing from a tool's
  reply until the server restarts -- `restart_server`, between sessions
  and with the driver's say-so, fixes that. The analysis lives in
  `assetto_mcp/analysis.py` and can be called from a script against the
  read-only database (`db.get_lap`, `db.get_samples`,
  `db.rival_samples_between`, `db.rival_names`, `analysis.infer_contacts`).
  Say in the answer when you did that.
- `lap_trace` answers most questions the reports do not. Past it, and
  only with a shell, the database at `~/.assetto-mcp/telemetry.db` is safe
  to read with `mode=ro`: `samples` has the 25 Hz trace per lap,
  `laps` the verdicts, `completed_at` and `fuel_used_l`, `rival_samples`
  opponents (`spline`, not `norm_pos`), `sessions` `track_length_m`.
