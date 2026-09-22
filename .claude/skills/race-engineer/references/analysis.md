# Reading the numbers

What the fields mean once they are on the screen, and the rules that have
held across the sessions so far. Where a rule is specific to this driver's
car it says so; the rest is general.

## Positions into meters

`pos × track_length_m` is meters past the start line. Meters before
turn-in is `(entry_pos − pos) × track_length_m`. One thousandth of position
is 4.2 m at Interlagos, 4.5 m at Kyalami, 5.4 m at Watkins Glen. Quote
brake points as meters before turn-in so they line up with the 50 m and
100 m boards, and name the corner the way the driver does.

## Balance: slip and steering lock

`slip_balance` is front slip minus rear slip. Positive is the front sliding
more. Whole-lap values around +0.2 to +0.4 are normal for a GT3 driven
hard; a single corner at +0.7 or more is a real complaint.

Before calling a corner front-limited, look at `peak_steer_norm` on it. On
the NSX GT3, slow corners read +0.8 to +1.1 whenever lock is over about
0.5, and near zero under 0.35, on the same setup and the same lap. Two
setup A/Bs (rear bar, front camber) moved nothing there. Over 0.5 of lock
it is a driving question; under 0.4 it is a setup one. Fast corners with
high slip balance at 0.3 lock are the car: at Watkins Glen the long right
after the Bus Stop, the fast left before the last corner and the last
corner itself read +0.23 to +0.45 at 0.31 to 0.37 lock on every clean lap.

`turn_sign` groups corners by direction and nothing more. Tyre
temperatures correlate with it: the side that runs hotter is the side
loaded in the corners that share a sign.

## Entry phase and spins

`entry_phase` runs from the brake point (or turn-in when the corner is
taken without braking) to the apex. `rotation_deg` over about 150 with a
`yaw_rate_peak_deg_s` over 100 is a spin. Clean corners rotate 15 to 90
degrees at 20 to 45 deg/s.

The spin shape that has cost this driver most: brake applied after turn-in
on a loaded corner. `brake_point_pos` later than `entry_pos`, entry-phase
rear slip two to four times front, yaw peak 115 to 136 deg/s. On every
clean fast lap the braking for that corner is finished before turn-in. Say
this first when it is the shape; it is not a setup property and no setup
change has touched it.

Check the contact list before saying any of that. See tools.md group 6.

Throttle on a slippery kerb is the other recurring spin: full throttle at
0.4 lock on a painted kerb, then the rear goes. TC one step more
intervention was the fix that held (3 to 2 on the NSX), not dampers.

An excursion on exit with the throttle at 100% and lock still at 0.4 is
an entry overshoot: the car arrived faster than the clean lap at the
brake point, braked deeper, coasted longer on lock, and picked up the
throttle later and with more steering. Compare the speed trace against
the clean lap from 100 m before the brake point.

## Braking and brake bias

`braking_report` gives slip per axle under straight-line braking.
Front-to-rear ratio above 1 means the front is nearer its limit, which is
where a GT3 at 60% front bias usually sits (1.06 to 1.11 on the NSX).
Moving bias rearward two points brought it to 0.91 to 0.98, shortened the
braking distance into the first corner by 10 to 15 m at the same entry
speed, raised entry yaw, and tightened the lap-time spread from 0.76 s to
0.04 s. Below about 0.9 the rear is doing the stopping and a second brake
input inside the corner becomes dangerous.

Braking distance is measured from the first brake sample over 0.3 to the
last, in meters, with the entry speed beside it. The field does not brake
later than this driver; when they lose ground in a pack it is the rear
stepping out on the brakes, not the marker.

A full tank moved neither brake points nor deceleration.

## Tyres

Read hot pressures and core temperatures at the end of the lap, all four,
and say which side and which axle. What has held:

- The loaded side of a circuit runs 8 to 12 °C and about a psi hotter.
  Interlagos loads the right, Watkins Glen the left. Lowering the loaded
  side's cold pressure made the temperature split and the wear worse.
- Rears 3 psi or more under the fronts with the loaded rear the hottest
  tyre on the car wants a psi more in the rear, and it is the first change
  to make at a stop.
- Rear wear rate on the NSX has jumped two to three times at lap 10 of a
  stint in every stint measured (Kyalami twice, Interlagos twice), the
  loaded rear first, with no temperature rise to warn of it. Fronts stay
  flat. Budget for it in a race longer than 12 laps. `stint_wear` shows it
  in `trend` for the rear corners: `late_per_lap` well above `early_per_lap`,
  and a positive `change`.
- Cold tyres explain the first flying lap and nothing after it.

## Fuel

Measured rates: 2.9 L/lap at Kyalami, 2.7 to 2.85 at Interlagos, 3.4 at
Watkins Glen, all NSX GT3 at race pace. Scale by track length for a first
guess at a new circuit. The in-game estimator is right; a stint
arithmetic that counts an out-lap as a full lap is not. The driver wants
one lap of margin, no more: "you can't lose a race from having 1 to 2 L
too much fuel, but you can sure lose from not having enough", and 1.5 laps
was "kind of a lot".

Read `fuel_l` at each lap boundary and difference readings taken at the
same point in the lap. When the tank is at three laps, say pit within two.

## Gearing and aero

Judge the final drive from 6th-gear rpm at the top-speed point on the
fastest clean lap against the shift rpm seen in the lower gears. Within
200 rpm of it solo means the limiter in a tow, which costs little; at the
limiter solo means the next ratio up. The shortest ratio has been right at
Kyalami, Interlagos and Watkins Glen; 6th never reaches the shift point.
Over-revs show as one downshift with rpm above the others.

Wing is a planned test, not a race-day change: share of the lap over
1.5 g above 180 km/h against share at full throttle in a straight line
says which way the trade leans; the run comparison's `top_speed_kmh` and
`peak_lat_g` are both in the metric family.

## Run comparisons

Three clean laps a side is the floor; lap time needs five or six to clear
its own noise. Report the mechanism (braking distance, slip ratio, entry
yaw, temperatures) alongside the statistical verdict, and say which
metrics the run was long enough to see. "Within noise" with a `resolution`
larger than the change is "too short". A `corner_lead` alone is where to
look, never a result.

Exclude nothing silently. A warm-up lap in the baseline widens its spread;
say so rather than dropping it.

## Suspension

Render-rate capture is fine for ride height and front load transfer, which
is the quietest channel there is: a rear bar change moves it by a point
against 0.3 of noise. Damper histograms need the 333 Hz worker, which is
single-player only. `attitude_report` carries banking and grade and was
useless at the circuits driven so far.

## Contacts, in one paragraph

Over 3 g is not tyres. A car within 10 m at the same instant is a hit.
Nobody near and 15 km/h gone in the instant is a wall. Nobody near, speed
kept, rotating over 90 deg/s is a snap. The rest is a kerb. A hit on the
throttle with the brake arriving after it is the other car's fault; say it
that way.
