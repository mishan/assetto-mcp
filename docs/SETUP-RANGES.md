# Setup values, ranges and clamping

AC **silently ignores** setup values outside the ranges defined in the car's
`setup.ini`. The server clamps and snaps to each car's legal min/max/step so
that can't happen — and tells you when it did.

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

## Fallback: a ranges file, for when the in-game app isn't running

1. In Content Manager: car page → unpack data (or use QuickBMS).
2. Copy the car's `setup.ini` into the ranges folder, named after the car's
   folder name — e.g. `ks_mazda_mx5_cup.ini`.

```powershell
explorer $env:USERPROFILE\.assetto-mcp\ranges
```

(cmd.exe: `explorer %USERPROFILE%\.assetto-mcp\ranges`)

Game-reported ranges always win over this file. Note that encrypted car data
may refuse to unpack at all, which is the main reason the in-game route is
preferred. `write_setup` reports `ranges_source` as `game`, `file` or `none`.

## Known gap: what the setup screen will actually display

`setup_ranges` reports `show_clicks_mode` and `display_multiplier`, and
`write_setup` prints things like `"0 (click index, mode 2)"` — which is the
tool admitting it does not know what the setup screen will say. Cases where the
guess has been wrong are listed in [BACKLOG.md](../BACKLOG.md) item 7. If a
number the tool reports disagrees with the number on your screen, believe your
screen and say so.
