# Driver and car notes: the shape

The real notes are the driver's own and live in the data directory, never
in this repository: `<data_dir>/notes/`, where `data_dir` is what
`storage_report` returns (`~/.assetto-mcp` unless `ASSETTO_MCP_DATA` says
otherwise). This file only shows their shape, and everything in it is made
up. Create the real files from it the first time there is something to
write down.

- `notes/driver.md` -- how the driver works and what they want. One file.
- `notes/<car id>.md` -- one per car, named for the id in `sessions.car`:
  setup lineage, what has held, open items, measured rates.

Dated facts, not laws. The newest handoff in `exports/` wins when it
disagrees.

---

## notes/driver.md

### How the driver works

- Races on a league server: practice, qualifying, two 20-minute races.
- Wants the answer first, placed in meters and turn numbers.
- Changes brake bias on the wheel mid-session and says so; tag it with a
  setup-name suffix.
- Wants two laps of fuel margin.

### Preferences

- Predictability over peak grip.
- Reports feel accurately. When they say a change did nothing, believe it
  before believing a p-value.

---

## notes/example_gt4_car.md

### Setup lineage

| Setup | Track | Change | Verdict |
|---|---|---|---|
| base_v1 | Monza | starting point | |
| v2 | Monza | rear ARB one step softer | kept |
| v3 | Spa | front pressure -0.5 psi | no measurable change: rejected |

v2 as of 2026-01-15: the values that matter, in the setup screen's units.

### What has held

- Entry understeer tracks steering lock, not the front bar.

### Open

1. Wing one step up, a practice A/B.

### Measured rates

| Circuit | Length | Fuel per lap | Loaded side |
|---|---|---|---|
| Monza | 5.8 km | 2.6 L | right |
