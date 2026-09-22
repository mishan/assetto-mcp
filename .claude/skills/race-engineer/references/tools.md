# The assetto-mcp tools, by the question they answer

Thirty-seven tools. They fall into eight groups. For each: what to pass,
what to read in the reply, and the trap that has already caught someone.
Pass every parameter explicitly, including nulls: the schemas this client
exposes have turned optional parameters into required ones between updates,
and a call refused for a missing parameter is fixed by adding it, not by
skipping the call.

## Contents

1. Recording and state
2. Setup attribution
3. Reading one lap
4. Comparing laps and runs
5. Tyres, brakes, suspension, fuel
6. Opponents and contacts
7. Setup files and the setup screen
8. The driver's side: notes, messages, the bridge

---

## 1. Recording and state

**`start_recording()`** -- idempotent; recording starts with the server
anyway, so this mostly undoes a `stop_recording`. If another instance holds
the recorder this one stands by and says so. Call it first every session.

**`recording_status()`** -- read `state`, not `running`: "never_started" and
"stopped_by_request" look identical otherwise. "standby" is healthy.
`session_id` is the id to tag and to watch. `setup_name` shows the current
tag. "waiting for AC to go live" means the car is in the pits, the menu, or
between sessions.

**`stop_recording()`** -- durable and shared across instances. Do not call it
casually; it stays off until `start_recording`.

**`live_snapshot()`** -- car, track, layout, session status, air and road
temperature, `fuel_l`, tyre pressures and core temperatures right now, last
and best lap. Poll it at each lap boundary for fuel, and whenever you want
the tyre state between laps. `status: "off"` means the game is not in a
session. The `tyre_compound` reads garbage when the car is not live.

**`list_sessions()`** -- id, car, track, lap count, best time. `track_length_m`
is on the session row in the database (`sessions.track_length_m`), which is
what turns a position into metres; the tool reply may not carry it, and a
read-only SQL query does.

**`list_laps(session_id, limit)`** -- most recent first, with ids for the lap
tools.

**`storage_report()`** -- database size and thinning. `sample_stride` above 1
on a lap means its trace was decimated; numbers are intact, resolution is
not.

**`rescore_track_limits()`** -- re-derive ran-wide verdicts after changing
the wheels-out threshold. Skips thinned laps and says so.

## 2. Setup attribution

Nothing in shared memory says which setup file is on the car. Attribution is
a claim, made three ways by how much damage a wrong one does.

**`identify_setup(session_id)`** -- matches the live setup-screen values
against every saved file for this car and track. Needs the in-game app to
have seen the setup screen. Reports several candidates when files are
identical, and the car's setup legality. Pass `session_id` explicitly.

**`set_session_setup(setup_name, session_id)`** -- forward only. Laps from
now carry the name; nothing already stored changes, including unlabelled
laps. Call it when the driver says they loaded something, after every pit
stop that changed anything, on every new session id, and for on-wheel
changes with a suffix (`claude_v15 bias58`).

**`label_laps(lap_ids, setup_name, session_id)`** -- fills only laps with no
setup recorded, only the ids named. Ask the driver which laps; the boundary
is a garage stop and nothing in the telemetry marks it. `lap_ids` is a
string: "87,88,89,90" or "87-90".

Genuinely wrong labels are fixed with `scripts/relabel_laps.py`, on purpose
not a tool.

## 3. Reading one lap

**`lap_summary(lap_id)`** -- the workhorse. Read in this order:

- `usable_for_timing` and `not_usable_because`; `ran_wide` and
  `track_limits` (wheels out, excursions, milliseconds off).
- `contacts_inferred` (see group 6) before anything else about a bad lap.
- `corners[]`: each has `turn` (the session's label, quote this),
  `corner` (this lap's ordinal, do not quote), `apex_pos`, `entry_pos`,
  `exit_pos`, `brake_point_pos`, `throttle_on_pos`, `min_speed_kmh`, `gear`,
  `peak_steer_norm` (fraction of full lock, not degrees), `peak_lat_g`,
  `slip_balance` (positive front slides more), `front_slip`, `rear_slip`,
  `turn_sign` (groups corners by direction; not left/right, AC does not
  document which sign is which), and `entry_phase` (from brake point or
  turn-in to apex: slip balance, mean steer, yaw peak, rotation).
- `tyres`: pressure average and end-of-lap, core temperature average, per
  corner of the car.
- `top_speed_kmh`, `time_full_throttle_pct`, `time_braking_pct`,
  `time_coasting_pct`, `peak_lat_g`, `peak_braking_g`.
- `overall_slip_balance` and `slip_quality`; `accel_samples_dropped`
  (spikes past 6 g dropped from the peaks; they are still in
  `contacts_inferred`).
- `corner_detection`: which bar the corners were found against. With one
  lap in the reference it is that lap's own peak, and every other lap is
  judged against it; the numbering settles after a few laps.
- `suspension` headlines when the app captured them; `suspension_report`
  for the rest.

Convert every `pos` to metres before speaking: `pos × track_length_m`.

**`track_corners(session_id)`** -- where each T-number is: start, apex, end,
brake point, sign, and how many laps cornered there. Built per session
from the laps driven, so the numbering is the session's, not the circuit's:
a flat kink is not numbered, and 3A is numbered straight through. The
numbering is also not the same in `compare_laps` (built from those two laps
only) or `compare_runs` (built from the laps compared). Quote the label
with the tool that produced it, and prefer metres and the driver's own
name for a corner when the labels disagree.

**`driving_line(lap_id, compare_lap_id, points)`** -- where the car was
across the track, slice by slice, with speed, ride height and a bump map.
With `compare_lap_id`, `separation_m` says how far apart the two lines
were at each point: the answer to "was that a wider line".

**`export_line_map(session_id, lap_ids)`** -- one HTML file in the data
directory's `exports/` folder with every lap drawn, best lap picked out,
colouring by setup or speed or pedals, tyre and rev charts, opponents'
brake points. `lap_ids` is a JSON list, or null for the whole session.
Hand the path to the driver. `scripts/driving_line_map.py` does the same
from a shell.

**`suspension_report(lap_id, lap_count, session_id)`** -- damper histograms,
ride height, roll balance. Read `source`: "app" is render-rate and the
damper histograms are body motion, not valving; "worker" is 333 Hz and
real. **`suspension_capture_status()`** explains which tier is arriving.
The worker is single-player only.

**`attitude_report(lap_id)`** -- roll and pitch per g. A car-and-track
number: banking and grade are in it, so it was useless at Kyalami and
Interlagos and is only comparable between laps at the same circuit. Check
`fit_r2` before quoting a gradient.

**`braking_report(lap_id, points)`** -- slip per axle under straight-line
braking, which axle is nearer its limit (what brake bias moves), lockup
runs. The ABS and TC fields hold one value all lap and measure nothing.
The lockup threshold is uncalibrated; read the distribution. Pass `points`.

## 4. Comparing laps and runs

**`compare_laps(lap_id_a, lap_id_b)`** -- corner by corner: min speed delta,
brake point delta, slip balance delta. Deltas are a minus b; a negative
`min_speed_delta_kmh` means lap b carried more speed. Read
`lap_time_warning` before quoting `time_delta_ms`. Its turn numbering is
its own.

**`delta_by_position(lap_id_a, lap_id_b, segments)`** -- where the time went,
including straights and sweepers no corner detector flagged. Read
`gain_ms` per segment; the cumulative figure only says a gap exists. Up to
200 segments.

**`compare_runs(baseline_laps, candidate_laps, clean_laps_only,
include_invalid)`** -- the A/B judge. Two comma-separated id lists. Read:

- `metrics[*].verdict`, `p_value_adjusted` against 0.05 (the family is
  corrected together), `change`, `resolution` (smallest change these laps
  could catch half the time), `power` (chance of catching the change
  actually measured). "Suggestive" means run more laps, not "no".
- `lap_time_consistency`: needs six a side to say anything.
- `corner_leads`: exploratory, uncorrected, about 5% of quiet corners show
  up. Where to look when a metric moved; never a finding.
- `excluded_laps` with reasons, `ran_wide` and `contacts` lists: laps
  counted but flagged. A result resting on a contact lap may be measuring
  the hit.
- `baseline_setups` and `candidate_setups`: check the tags are what you
  think they are before reading anything else.

Pass `clean_laps_only=false` and `include_invalid=null` explicitly. Both
sides must be the same car on the same layout.

## 5. Tyres, brakes, suspension, fuel

**`stint_wear(session_id, from_lap, to_lap)`** -- remaining and used per tyre
per lap, the rate, and whether the rate is rising. Segmented into stints
at out-laps, pit visits and tyre changes. `from_lap`/`to_lap` are inclusive
lap numbers and are required by this client; use the session's first and
last lap numbers for everything. Wear, not pressure, is the evidence a
tyre went off.

**`fuel_plan(race_laps, stops, session_id)`** -- needs the car's
`km_per_liter` from the in-game app, which is almost never present, and
then errors. When it does not run: per-lap rate from consecutive
lap-boundary `live_snapshot` readings, or the in-game estimator's figure
from the driver. Tank size is in `setup_ranges` under FUEL.

## 6. Opponents and contacts

Opponent data comes only through the in-game app's bridge; single-player
has none.

**`list_rivals(session_id, limit)`** -- car index, driver name, car model,
best lap, how many of their laps were well covered. Pass a session id.

**`compare_to_rival(car_index, lap_id, rival_lap_count, session_id)`** --
where they carry more speed by track position; their brake and throttle
points when the server transmits them (`rival_input_fields` says which
are live). `rival_lap_count` is required by this client: take it from
`list_rivals`' well-covered laps, quickest first.

**`contacts_inferred`** (on `lap_summary`) and **`contacts`** (on
`compare_runs`) -- impacts without the damage model. Each entry: `pos`,
`t_ms`, `peak_g`, `axis`, `speed_kmh`, `speed_lost_kmh`, `yaw_rate_deg_s`,
`gas`, `brake`, `input` (throttle, brake, both, coasting), `nearest`
(car index, driver name, distance, speed) and `verdict`:

- `contact`: a car within 10 m at the same instant. The radius is wide
  because opponent positions lag; confirmed hits read 6 to 8 m.
- `wall`: nobody near and 15 km/h or more gone in the instant of the spike.
  Sand, grass and ABS braking lose speed over seconds and do not qualify.
- `snap`: nobody near, speed kept, rotating over 90 deg/s. A spin, or a
  slide caught. The g is the rotation.
- `kerb`: nobody near, speed kept, no rotation. Kerbs, the floor, launches.
- `no_opponent_data`: nothing to place it against.

A car parked alongside on a practice-start grid reads as a contact if the
game spikes on the reset. Distance and speed are in the entry; read them.

## 7. Setup files and the setup screen

**`list_setups(car, track)`**, **`read_setup(car, track, name)`** -- internal
folder names (`ac_friends_honda_nsx_gt3_evo`, `lilski_watkins_glen`). The
setup folder is per track, so a setup carried to a new circuit has to be
copied there by the driver, and the file name may still say the old track.

**`setup_ranges(car)`** -- legal min, max, step per entry in file units,
plus `display_multiplier`, `show_clicks_mode` and `display` with its
`source`: "observed" (read off the screen), "game" (the multiplier, which
has been wrong), "unknown" (say so). Reachable values are the spinner's
ladder from min and from max, not a grid.

**`write_setup(car, track, name, values_json, base_setup, overwrite,
allow_unclamped)`** -- `values_json` is a JSON object of section to number;
unspecified sections come from `base_setup`. Clamped and snapped to what
the spinner can reach; `clamped` lists what changed. `displays_as` says
what the screen will show, with a source per entry. Refuses to reuse a
name (write under `suggested_name`, or ask; `overwrite=true` backs the old
file up first) and refuses without ranges (`allow_unclamped=true` is the
driver's call, not yours: the fix is opening the setup screen once with
the app running). Pass `allow_unclamped=false` explicitly.

**`record_display_value(field, displayed, stored, car, note, session_id)`**,
**`record_display_range(field, displayed_at_min, displayed_at_max, car,
note, session_id)`**, **`record_display_note(field, note, car, session_id)`**,
**`forget_display_value(field, car, keep_note, session_id)`** -- what the
screen shows for a stored value. Two readings pin scale, sign and zero;
the range tool does it in one exchange at the spinner's ends. Notes are
for what is not a number: TC and ABS count 1 as the most intervention.
Known on this car: camber stored in tenths of a degree (-40 is -4.0),
FINAL_RATIO 0/1/2 shows 171/181/191 mph as 6th-gear top speed, brake bias
is percent front.

## 8. The driver's side

**`get_driver_notes(session_id, limit, all_sessions)`** -- complaint tags
pressed on the wheel (understeer, oversteer, braking, traction, note) with
a spline position comparable to `apex_pos` and the lap they were pressed
on (current lap = `lap_count` + 1). Correlate with the corner's slip
balance and steer.

**`send_driver_message(text)`** -- one sentence on the in-game overlay. Fails
when the bridge port is held by an orphaned server; not worth chasing
during a session.

**`bridge_status()`** -- whether the in-game app is connected. Without it:
no opponents, no suspension, no setup-screen values, no messages.

## Outside the tools

- `scripts/watch_laps.py --session N --interval 3` -- one line per stored
  lap, this session and later ones. Run it under a Monitor so each lap
  wakes the session. `--replay` prints what is already stored.
- `scripts/driving_line_map.py` -- the line map from a shell.
- `scripts/say.py "text"` -- reads a sentence aloud over the game audio
  with the Windows voices (`--list` shows them, `--voice Zira`,
  `--rate 1`). Returns at once; `--wait` blocks until it has played. For
  live mode only, and only for what the driver acts on this lap.
- `scripts/relabel_laps.py` -- the only way to overwrite a setup label.
- The running server can predate the repository's code: a field this
  file describes (`contacts_inferred`, say) may be missing from a tool's
  reply until the server restarts. The analysis lives in
  `assetto_mcp/analysis.py` and can be called from a script against the
  read-only database (`db.get_lap`, `db.get_samples`,
  `db.rival_samples_between`, `db.rival_names`, `analysis.infer_contacts`).
  Say in the answer when you did that.
- The database at `~/.assetto-mcp/telemetry.db` is safe to read with
  `mode=ro`. `samples` has the 25 Hz trace per lap (`norm_pos`,
  `speed_kmh`, `gas`, `brake`, `steer`, `gear`, `rpm`, `acc_lat`,
  `acc_lon`, `slip_*`, `press_*`, `core_*`, `tyres_out`, `pos_x/y/z`,
  `heading`, `damage`); `laps` has the verdicts and `completed_at`;
  `rival_samples` has opponents (`spline`, not `norm_pos`); `sessions` has
  `track_length_m`. Reach for it when a tool does not answer the question:
  brake points in metres with entry speed, shift rpm by gear, time spent
  in 6th, per-zone slip ratios.
