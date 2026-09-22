# Driver and car notes

Dated facts from previous sessions. The memory directory and the newest
document in `exports/` are canonical; when they disagree with this file,
they win. The car throughout is the Honda NSX GT3 Evo
(`ac_friends_honda_nsx_gt3_evo`) on Michelin S8M.

## How the driver works

- Races on a friends' server, typically two races of 12 to 13 laps, with
  practice and qualifying before. Practice starts happen before qualifying
  and read as contacts at the grid.
- Reads you between laps. Wants the answer first and places on the track
  in meters and corner names, never spline fractions.
- Makes on-wheel changes (brake bias, TC, ABS) mid-session and tells you;
  tag them with a setup-name suffix.
- Prefers about a lap of fuel margin. Trusts the in-game estimator.
- Pushes back when the data is read against their sensation, and has been
  right each time: kerbs were slippery not bumpy, the spin was on throttle
  not brake, most spins were contacts.
- Copies the current setup file into each new track's folder, so the file
  name can say a previous track.
- Commits the repo themselves; the repository forbids assistant
  attribution in any artifact.

## Setup lineage

| Setup | Track | Change | Verdict |
|---|---|---|---|
| claude_sebring_v9 | Sebring | starting point for the season | |
| v10 | Kyalami | rear ARB 20000, ABS 3, fuel 32 | kept |
| v11 | Kyalami | front camber -4.0 both sides | peak lateral g up, balance unchanged |
| v12 | Kyalami | final ratio 0 (171 mph) | kept; 6th never reaches the shift point |
| v13 | Interlagos | rear fast rebound 4500 | wrong premise, never driven |
| v14 | Interlagos | right side -1 psi, TC 2 | pressures levelled, right front hotter and wore faster: rejected |
| v15 | Interlagos, Watkins Glen | v12 + TC 2, fuel 38 | race setup; bias 58 on the wheel at the Glen |

v15 as of 2026-09-21: ARB 30000/20000, camber -4.0/-3.3, pressures 21/18,
brake bias 60 in the file (58 on the wheel), TC 2, ABS 3, wing 6, final
ratio 0, diff coast 60 / power 40 / preload 100.

## What has held

- Slow-corner understeer tracks steering lock, not bars or camber.
- Rear wear rate more than doubles at lap 10 of a stint, loaded rear
  first, four stints out of four.
- The loaded side of the circuit runs hotter; lowering its pressure made
  it worse.
- The field brakes at the same point as the driver or earlier.
- A full tank changes neither brake points nor deceleration.
- Shortest final drive right at every circuit so far.
- Brake bias 58 at the Glen: shorter braking into T1, more entry rotation,
  rear nearer its limit, no step-out. 62 untested.

## Open

1. Rear cold pressure 18 to 19, for the rear temperature and wear.
2. Front-limited in the long fast corners at moderate lock; front bar one
   step softer if the pressure change does not move it.
3. Wing 6 against 5, a practice A/B.
4. Brake bias 62 as a separate stability test if the rear ever steps out
   on entry.

## Measured rates

| Circuit | Length | Fuel per lap | Loaded side |
|---|---|---|---|
| Kyalami 2016 | 4.5 km | 2.9 L | |
| Interlagos | 4.2 km | 2.7 to 2.85 L | right |
| Watkins Glen, Boot | 5.4 km | 3.4 L | left |
