# The handoff document

Written at the end of a session to `exports/session-<first>-<last>-<track>-<car>.md`.
The next session starts by reading it, so it has to carry what the tools
cannot recover: which setup was on which laps, what the driver said, what
was decided and why, and what is still open.

```markdown
# Sessions <first>-<last> -- <Track> (<folder>/<layout>), <Car>

**Dates:** ... -- **Air** .. C, **Road** .. C
**Setups:** <lineage in one line, with on-wheel changes>
**Results:** <qualifying, race results, best laps, what went wrong>

---

## Setup lineage

| Setup | Change | Verdict |
|---|---|---|

One row per file written or on-wheel change, with the measured verdict or
"never driven".

## What the week established

Bullets. Each one a fact with its number and the laps it came from. Circuit
facts (which side is loaded, fuel per lap, brake points in metres before
turn-in on the best lap), car facts (wear onset, balance rules), driver
facts (the spin shape, what they corrected).

## <Race> against <race or practice>

The compare_runs result in three lines: what moved, what was suggestive,
what the run could not see.

## Rivals

Field, pole time, who the driver was closest to, where the deficit was.

## Open

Numbered, priority first. Each with the test that settles it: the change,
the laps needed, the metric to judge on.

## Notes

Display mappings observed, tool quirks hit, anything the next session
should not rediscover.
```

Keep it under two screens. Fractions only in tables that cross-reference
tool output; everything else in metres and corner names.
