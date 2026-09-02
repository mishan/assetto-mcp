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

## What the setup screen actually shows

A stored value and the number on the setup screen are often different, and the
game's own `display_multiplier` is not enough to bridge them. It has been wrong
about the **sign** (the F4 negates the front toe axis), the **scale** (rod
length is a fraction of a millimetre per click, not about one), and the
**zero** (the NSX stores 10 for 0.00° of toe). And `show_clicks_mode` says only
that the number is a position, not what that position reads as.

So the mapping is **measured, not derived**. Every value reported carries a
`source`:

| `source` | Means |
|---|---|
| `observed` | a line fitted from two readings off your actual screen |
| `observed_offset` | the zero is measured, the scale is still the game's |
| `game` | the game's `display_multiplier`, applied as documented |
| `stored` | the game says the screen shows the stored number itself |
| `unknown` | nothing here can say; it will not invent one |

`unknown` is the important one. It used to print `"20 (click index, mode 2)"`,
which is an admission of ignorance shaped like an answer, and got repeated back
as one.

### Teaching it what your screen says

Open the setup screen and read a value off it:

> *"Front toe shows -0.20 right now."*

The stored value comes from the game, so that's one number from you.

**Two readings at different spinner positions** pin the mapping exactly —
scale, sign and zero. **One reading** fixes only the zero, borrowing the
game's scale, and reports `observed_offset` so you can tell the two apart.
That's enough for the NSX toe case, where the scale was right all along.

For an entry the game describes as a *click index* there is no scale to
borrow, so one reading fits nothing at all — it says so and asks for a second
rather than reporting success.

Faster still, run the spinner to each end:

> *"At minimum front toe shows 0.40, at maximum -0.40."*

The stored limits are already known, so that's a complete mapping in one
exchange. If both ends read the same number it refuses, rather than fitting a
flat line and calling it observed.

Misread something? Ask it to forget that entry and start again — readings and
note both go. A wrong reading is worse than none, because the mapping fitted
from it is stated with confidence. If you give it three readings and one
disagrees with the other two, it says which one.

Some things aren't numbers at all. Traction control counts **1 as the most
intervention** and 11 as the least, which no multiplier can express:

> *"Note that TC 1 is the most intervention, 11 is the least."*

That needs no reading off the screen and doesn't ask for one. Notes ride along
with whatever else is known about the entry and show up wherever it's
reported.

Everything is stored per car, so you teach it once.
