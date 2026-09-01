# Setup values, ranges and clamping

AC **silently ignores** setup values outside the ranges defined in the car's
`setup.ini`. The server clamps and snaps to each car's legal min/max/step so
that can't happen — and tells you when it did.

If it has no ranges for the car, it **refuses to write** rather than producing
a file that loads, looks right, and doesn't do what it says. See
[when there are no ranges](#when-there-are-no-ranges) for the two ways to fix
that, and the escape hatch if you want the file anyway.

## With the in-game app running, this needs no setup at all

The app reads `ac.getSetupSpinners()`, which reports every adjustable entry —
legal min/max/step, current value, units — keyed by the same section names the
setup files use, for whatever car is loaded. Ask for `setup_ranges` to see
them.

That also settles the units question. A stored value and the number on the
setup screen aren't always the same: camber is stored as tenths of a degree,
ride height as a click index. The game reports `display_multiplier` and
`show_clicks_mode` per entry, so neither has to be inferred.

Two further consequences:

- **`identify_setup` works out which saved setup is on the car** by comparing
  live values against your saved files. Shared memory exposes only brake bias
  and fuel, which can't separate setups differing in ARB or camber — the
  setup menu exposes everything, so the match is exact. Several identical
  setups are reported as several rather than resolved by guessing.
- **`ac.getCarSetupState()`** tells you whether AC considers the setup legal,
  so a silently-ignored value shows up as `illegal` instead of as a change
  that mysteriously did nothing.

## The spinner does not move on a grid

AC's setup spinner adds or subtracts `step` from wherever it already is and
clamps at the ends, so the reachable values are **two ladders that miss each
other**, not `min + n*step`.

The RSS Formula 4's rear wheel rate is MIN 53, MAX 88, STEP 17. Counting up
gives 53, 70, 87, 88. Counting back down from 88 gives 71, 54, 53. Six
reachable values, of which a grid anchored at the minimum can express three —
and 54 is not one of them.

That mattered: asked to write 54, a grid-snapping `write_setup` returned 53
and reported it as clamping, so a 2% softer spring looked like the request
being tidied up. Values are now snapped to what the spinner can actually
reach, and `write_setup` only reports `clamped` when it genuinely changed
something.

## When there are no ranges

`write_setup` refuses. Nothing knows the legal min, max or step, so nothing can
be checked, and AC discards whatever it doesn't like without saying so — the
setup would load, the garage would show no complaint, and the first hint would
be a run that feels exactly like the last one. That's a whole session to
notice, against one extra round trip to prevent.

Two ways to give it ranges:

**Start the game with the in-game app enabled and open the setup screen once.**
The app reports every adjustable entry automatically, for whatever car is
loaded. This is the easy route and needs no files.

**Or install a ranges file**, for working with the game closed:

1. In Content Manager: car page → unpack data (or use QuickBMS).
2. Copy the car's `setup.ini` into the ranges folder, named after the car's
   folder name — e.g. `ks_mazda_mx5_cup.ini`.

```powershell
explorer $env:USERPROFILE\.assetto-mcp\ranges
```

(cmd.exe: `explorer %USERPROFILE%\.assetto-mcp\ranges`)

Game-reported ranges always win over this file. Encrypted car data may refuse
to unpack at all, which is the main reason the in-game route is preferred.
`write_setup` reports `ranges_source` as `game`, `file` or `none`.

**If you want the file anyway**, `allow_unclamped=true` writes it and keeps the
warning in the report. Verify every value on the setup screen before driving.

## Reusing a setup name

`write_setup` also refuses to replace an existing setup file, and suggests a
free name instead. From inside the tool a setup you built by hand and one it
wrote a minute ago are the same file, and the garage gives no hint that what's
under the old name is now something else.

`overwrite=true` replaces it, after copying the previous file to
`<name>.ini.bak-<timestamp>` in the same folder. AC only lists `.ini` files, so
the backup doesn't clutter your setup menu.

## Known gap: what the setup screen will actually display

`setup_ranges` reports `show_clicks_mode` and `display_multiplier`, and
`write_setup` prints things like `"0 (click index, mode 2)"` — which is the
tool admitting it does not know what the setup screen will say. Cases where the
guess has been wrong are listed in [BACKLOG.md](../BACKLOG.md) item 7. If a
number the tool reports disagrees with the number on your screen, believe your
screen and say so.
