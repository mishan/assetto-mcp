"""Every lap of a session drawn as a driving line, in one HTML file.

The map is drawn from the world position each lap stores at 25 Hz, so it
shows where the car actually went -- across the road, not just along it.
Every lap is a line; the fastest lap that counted is picked out; hovering
any point gives lap, setup, position, speed, gear and pedals. The laps can
be coloured by setup, so an A/B split shows at a glance, and any one lap
by speed or by what the pedals were doing, so a corner reads as a braking
zone, a coast and a throttle pick-up.

Turn numbers and brake points come from the session's own corner map --
the same T1, T2, T3 that lap_summary and compare_runs speak in. The
Corners view shows the reasoning behind them rather than only the result:
the turn map as bands on the track, the corners the detector found on the
selected lap, and underneath, that lap's lateral g against the bar it was
held to. A turn split in two, or a kink that never reached the bar, can be
seen there without reading a payload.

The file is self-contained: the samples are embedded, nothing is fetched,
and it can be opened from disk, mailed or published as-is. That is a
promise about privacy as much as convenience. Nothing is supposed to leave
the driver's machine, and a page that loaded a web font would tell a third
party every time it was opened.
"""

import html
import json
import math
from pathlib import Path
from statistics import median

from . import analysis, db, turns

TEMPLATE = Path(__file__).with_name("line_map.html")

# The least time between two drawn points. 25 Hz is more than a line needs
# and doubles the file; 80 ms keeps about 12 Hz. A time gap rather than
# "every Nth sample" because retention thins old laps -- every 2nd sample,
# then every 4th -- and a fixed stride on top would thin them again.
EVERY_MS = 80

# Fewer placed samples than this is not a line worth drawing: a lap given
# up seconds after the start, or the fragment a reset leaves behind.
MIN_POINTS = 50

# A drop in norm_pos bigger than this between consecutive samples is the
# start/finish line, not the car going backwards.
WRAP_JUMP = 0.5

# AC reports neutral for a sample or two while a shift goes through. Neutral
# held longer than this is the car stopped or rolling, and whatever gear
# follows is not a shift from the one before it.
SHIFT_MAX_NEUTRAL_MS = 1000

# Held at the rev ceiling: at least this many drawn points (about a quarter
# of a second) within this fraction of it, at full throttle.
LIMITER_BAND = 0.01
LIMITER_MIN_POINTS = 3
FULL_THROTTLE = 0.9

# The bump map. The lap is cut into this many stretches, about 10 m each on
# a 4 km circuit, and each suspension channel has a centred moving average
# of this length taken off it, so what is left is what the road did rather
# than what the driver did: weight moving under braking and cornering takes
# longer than a quarter second, a bump or a kerb takes less. Centred, not
# trailing -- a trailing average lags a braking weight transfer enough to
# report five millimetres of bumps that are not there.
SURFACE_SLICES = 400
BUMP_WINDOW_MS = 250
TRAVEL_KEYS = ("travel_fl", "travel_fr", "travel_rl", "travel_rr")
RIDE_KEYS = ("ride_f", "ride_r")

# Opponents. Each of their laps is put on one grid of track positions --
# about 10 m a step on a 5 km circuit -- because an opponent is sampled at
# 10 Hz and you at 25, at different moments, and the only axis the two cars
# share is where on the lap each was. Finer than the opponent's own sampling
# is worse, not better: at 1000 steps a 10 Hz lap left every other step
# empty. The few quickest laps of each car are what anyone compares
# against, and every lap carried costs file size.
RIVAL_GRID = 500
# Empty steps between two filled ones are interpolated up to this many --
# a car at speed covers a step between samples -- and left empty past it,
# where the lap was simply not seen.
RIVAL_FILL_STEPS = 2
RIVAL_LAPS_EACH = 3
RIVAL_MAX_CARS = 8
# The app clamps speed at 1000 km/h. A sample at the clamp is a car being
# teleported to the pits or the grid, not a car being driven.
RIVAL_TELEPORT_KMH = 999

TYRES = ("fl", "fr", "rl", "rr")

TURN_KEYS = ("turn", "apex_pos", "entry_pos", "exit_pos", "brake_point_pos",
             "laps_seen", "laps_total")
CLUSTER_KEYS = ("apex_pos", "entry_pos", "exit_pos", "laps_seen",
                "laps_total")


def _r(v, digits=None):
    return None if v is None else round(v, digits)


def trace(samples: list[dict], every_ms: int = EVERY_MS,
          derived: dict[str, list] | None = None) -> dict | None:
    """One lap's line as parallel arrays, or None if there is none to draw.

    A lap's samples can straddle the start/finish line at either end: the
    first few can still read 0.999 from the lap before, the last few 0.001
    of the lap after. Drawn as they are, either one puts a chord across the
    map. Cutting each lap at its highest norm_pos handles the end and not
    the start -- a lap whose first sample read 0.999 has its highest point
    first and is cut to nothing. So the trace is split at every wrap and
    the longest run kept, which also keeps a lap that began mid-circuit
    because recording started partway round.

    `derived` maps a name to a channel aligned with `samples` -- the
    lateral-g trace corner detection reads, the slip balance, the tyres --
    each carried through at the points kept.
    """
    placed = [(i, s) for i, s in enumerate(samples)
              if s.get("norm_pos") is not None
              and s.get("pos_x") is not None and s.get("pos_z") is not None]
    runs, run = [], []
    for i, s in placed:
        if run and run[-1][1]["norm_pos"] - s["norm_pos"] > WRAP_JUMP:
            runs.append(run)
            run = []
        run.append((i, s))
    if run:
        runs.append(run)
    longest = max(runs, key=len, default=[])
    if len(longest) < MIN_POINTS:
        return None

    kept, last = [], None
    for i, s in longest:
        t = s.get("t_ms")
        if last is None or t is None or t - last >= every_ms:
            kept.append((i, s))
            if t is not None:
                last = t
    out = {
        "p": [round(s["norm_pos"], 4) for _, s in kept],
        "x": [round(s["pos_x"], 2) for _, s in kept],
        "z": [round(s["pos_z"], 2) for _, s in kept],
        "v": [_r(s.get("speed_kmh")) for _, s in kept],
        "g": [s.get("gear") for _, s in kept],
        "t": [_r(s.get("gas"), 2) for _, s in kept],
        "b": [_r(s.get("brake"), 2) for _, s in kept],
    }
    for name, values in (derived or {}).items():
        out[name] = [values[i] for i, _ in kept]
    return out


def _skip_reason(samples: list[dict]) -> str:
    if not samples:
        return "no telemetry samples stored"
    if not any(s.get("pos_x") is not None and s.get("pos_z") is not None
               for s in samples):
        return ("no position data: recorded before schema v8, and the "
                "coordinates were never captured")
    return "too little of the lap was recorded to draw"


def shifts(samples: list[dict]) -> list[dict]:
    """Every gearchange on a lap: where, which way, and the revs either side.

    Read from the last sample in the old gear to the first in the new one,
    across the neutral AC reports mid-shift. The revs either side are what
    was recorded there, so at 25 Hz they can be a few hundred rpm from the
    instant of the change at high revs. Reverse is ignored.
    """
    out, last = [], None
    for i, s in enumerate(samples):
        g = s.get("gear")
        if not isinstance(g, (int, float)) or g < 1:
            continue
        g = int(g)
        if last is not None:
            j, prev = last
            gap = (s.get("t_ms") or 0) - (samples[j].get("t_ms") or 0)
            if g != prev and gap <= SHIFT_MAX_NEUTRAL_MS:
                out.append({"pos": round(samples[j]["norm_pos"], 4),
                            "from": prev, "to": g,
                            "rpm_before": _r(samples[j].get("rpm")),
                            "rpm_after": _r(s.get("rpm"))})
        last = (i, g)
    return out


def _pct(values: list[float], q: float):
    v = sorted(values)
    return v[int(q * (len(v) - 1))]


def gearing(shift_lists: list[list[dict]], ceiling: int | None) -> dict:
    """Upshift revs per gear, and the revs each downshift landed at.

    Upshifts one gear at a time only; a skipped gear on the way up is rare
    enough to be a mistake and would muddy the gear it started from. The
    spread is the 10th to 90th percentile -- how repeatable the shift is,
    which matters more than any one figure while no power curve is stored
    to say where the shift should be.
    """
    ups: dict[int, list] = {}
    downs: dict[tuple, list] = {}
    gears = set()
    for shs in shift_lists:
        for s in shs:
            gears.update((s["from"], s["to"]))
            if s["rpm_before"] is None or s["rpm_after"] is None:
                continue
            if s["to"] == s["from"] + 1:
                ups.setdefault(s["from"], []).append(s["rpm_before"])
            elif s["to"] < s["from"]:
                downs.setdefault((s["from"], s["to"]), []).append(
                    s["rpm_after"])
    return {
        "ceiling_rpm": ceiling,
        "max_gear": max(gears) if gears else None,
        "up": [{"from": g, "to": g + 1, "n": len(v),
                "rpm_med": _pct(v, 0.5), "rpm_lo": _pct(v, 0.1),
                "rpm_hi": _pct(v, 0.9)} for g, v in sorted(ups.items())],
        "down": [{"from": a, "to": b, "n": len(v), "rpm_med": _pct(v, 0.5),
                  "rpm_max": max(v)}
                 for (a, b), v in sorted(downs.items(), reverse=True)],
    }


def _limiter_runs(lap: dict, ceiling: int) -> list[dict]:
    """Stretches held at the rev ceiling at full throttle, from the drawn points."""
    runs, start = [], None
    floor = ceiling * (1 - LIMITER_BAND)
    n = len(lap["p"])
    for i in range(n + 1):
        hot = (i < n and lap["rpm"][i] is not None and lap["t"][i] is not None
               and lap["t"][i] >= FULL_THROTTLE and lap["rpm"][i] >= floor)
        if hot and start is None:
            start = i
        elif not hot and start is not None:
            if i - start >= LIMITER_MIN_POINTS:
                runs.append({"from": lap["p"][start], "to": lap["p"][i - 1]})
            start = None
    return runs


def bump_profile(rows: list[dict], keys, pos_key: str = "norm_pos",
                 slices: int = SURFACE_SLICES,
                 window_ms: int = BUMP_WINDOW_MS) -> list[float | None]:
    """How rough each stretch of the lap was: mm RMS, after the slow part is gone.

    Every channel in `keys` (metres) contributes to the same stretches, so
    four wheels of travel pool into one figure per stretch. A stretch with
    fewer than four readings is None rather than a figure from almost
    nothing. In the first and last half-window of a lap the average can only
    look one way, so it lags like a trailing one and the first stretch past
    the line reads a little rough.
    """
    buckets: list[list[float]] = [[] for _ in range(slices)]
    half = window_ms / 2
    for key in keys:
        pts = [(r["t_ms"], r[key], r[pos_key]) for r in rows
               if isinstance(r.get(key), (int, float))
               and r.get(pos_key) is not None and r.get("t_ms") is not None]
        n = len(pts)
        if n < 3:
            continue
        pre = [0.0]
        for _, v, _ in pts:
            pre.append(pre[-1] + v)
        lo = hi = 0
        for i, (t, v, p) in enumerate(pts):
            while pts[lo][0] < t - half:
                lo += 1
            hi = max(hi, i)
            while hi + 1 < n and pts[hi + 1][0] <= t + half:
                hi += 1
            avg = (pre[hi + 1] - pre[lo]) / (hi + 1 - lo)
            b = min(slices - 1, max(0, int(p * slices)))
            buckets[b].append((v - avg) * 1000.0)
    return [round(math.sqrt(sum(x * x for x in b) / len(b)), 2)
            if len(b) >= 4 else None for b in buckets]


def off_track(samples: list[dict]) -> list[dict]:
    """Where all the wheels the track-limits rule counts were off, and for how long.

    The rule db.score_excursions applies -- TRACK_LIMITS_WHEELS out for at
    least MIN_EXCURSION_MS -- so a stretch drawn here is one that made the
    lap `wide`, not a kerb clipped with two wheels.
    """
    gaps = [b["t_ms"] - a["t_ms"] for a, b in zip(samples, samples[1:])
            if a.get("t_ms") is not None and b.get("t_ms") is not None]
    step = median(gaps) if gaps else 0
    runs, start = [], None
    for i in range(len(samples) + 1):
        out = (i < len(samples) and (samples[i].get("tyres_out") or 0)
               >= db.TRACK_LIMITS_WHEELS)
        if out and start is None:
            start = i
        elif not out and start is not None:
            a, b = samples[start], samples[i - 1]
            if ((b.get("t_ms") or 0) - (a.get("t_ms") or 0) + step
                    >= db.MIN_EXCURSION_MS and a.get("norm_pos") is not None):
                runs.append({"from": round(a["norm_pos"], 4),
                             "to": round(b.get("norm_pos") or a["norm_pos"], 4)})
            start = None
    return runs


def integrate_time(speeds: list, track_length_m: float) -> list[int]:
    """Cumulative ms along a grid of track positions, from speed alone.

    For a lap with no clock on its samples. Checked against laps that have
    one, it runs 0.1-0.9% short -- the grid follows the AI line, not the
    line driven -- and a spin, where speed stops meaning distance covered,
    can take it 5% out. Good enough to rank laps and to show where time went;
    not a gap to the tenth.
    """
    step = track_length_m / len(speeds)
    out, t, last, last_i = [], 0.0, None, None
    for i, v in enumerate(speeds):
        if v is not None and last is not None:
            ms = (v + last) / 2 / 3.6
            if ms > 1:
                # Across every step since the last reading, not just one: an
                # empty step is road the car still covered, and skipping it
                # ran a real 2:00 lap four seconds short.
                t += (i - last_i) * step / ms * 1000
        if v is not None:
            last, last_i = v, i
        out.append(round(t))
    return out


def _fill_gaps(values: list, most: int = RIVAL_FILL_STEPS) -> list:
    """Interpolate runs of up to `most` empty steps between two filled ones."""
    out, n, i = list(values), len(values), 0
    while i < n:
        if out[i] is not None:
            i += 1
            continue
        j = i
        while j < n and out[j] is None:
            j += 1
        if 0 < i and j < n and j - i <= most:
            a, b = out[i - 1], out[j]
            for k in range(i, j):
                out[k] = a + (b - a) * (k - i + 1) / (j - i + 1)
        i = j
    return out


def rival_lap(samples: list[dict], track_length_m: float | None,
              grid: int = RIVAL_GRID) -> dict:
    """One opponent lap on the shared grid, and how its time was known.

    `clock` says where the time along the lap came from: `timed`, the car's
    own clock on every sample (recorded since schema v14), or `estimated`,
    speed integrated over the track length. Older laps also have no world
    position, so only newer ones can be drawn as a line.
    """
    samples = [s for s in samples
               if (s.get("speed_kmh") or 0) < RIVAL_TELEPORT_KMH]

    def res(key):
        return _fill_gaps(analysis.resample_by_position(
            samples, key, "spline", grid))

    v = res("speed_kmh")
    out = {"v": [_r(x) for x in v],
           "t": [_r(x, 2) for x in res("gas")],
           "b": [_r(x, 2) for x in res("brake")],
           "g": [None if x is None else int(round(x)) for x in res("gear")]}
    if any(s.get("pos_x") is not None and s.get("pos_z") is not None
           for s in samples):
        out["x"] = [_r(x, 1) for x in res("pos_x")]
        out["z"] = [_r(x, 1) for x in res("pos_z")]
    timed = [s for s in samples if s.get("t_ms") is not None]
    if samples and len(timed) >= 0.9 * len(samples):
        tm = res("t_ms")
        first = next(x for x in tm if x is not None)
        out["tm"] = [None if x is None else round(x - first) for x in tm]
        out["clock"] = "timed"
    elif track_length_m:
        out["tm"], out["clock"] = integrate_time(v, track_length_m), "estimated"
    else:
        out["tm"], out["clock"] = None, None
    # Online, a server may not transmit a remote car's pedals: the field
    # arrives and never moves, which is absence and not a driver who never
    # brakes.
    out["inputs"] = {k: len({round(s[k], 3) for s in samples
                             if s.get(k) is not None}) > 1
                     for k in ("gas", "brake")}
    return out


def _grid_lap_ms(lap: dict) -> int | None:
    """A lap time read off the grid, where the whole lap was covered."""
    tm, v = lap["tm"], lap["v"]
    if not tm or v[0] is None or v[-1] is None or tm[-1] is None:
        return None
    if sum(x is not None for x in v) < 0.95 * len(v):
        return None
    return round(tm[-1] * len(tm) / (len(tm) - 1))


def rivals(conn, session_id: int, track_length_m: float | None) -> list[dict]:
    """The opponents seen in a session, each with their few quickest laps.

    Pace comes from the lap time the app recorded where there is one, from
    the lap's own clock where its samples carry one, and from speed alone
    otherwise -- and each lap says which, as `time_src`.
    """
    out = []
    for d in db.list_rivals(conn, session_id, limit=100):
        car, laps = d["car_index"], []
        for l in db.well_covered_rival_laps(conn, session_id, car):
            g = rival_lap(db.get_rival_lap_samples(
                conn, session_id, car, l["lap_count"]), track_length_m)
            if l["lap_time_ms"]:
                ms, src = l["lap_time_ms"], "recorded"
            else:
                ms = _grid_lap_ms(g)
                src = g["clock"] if ms else None
            laps.append({**g, "lap_count": l["lap_count"], "time_ms": ms,
                         "time_src": src})
        laps.sort(key=lambda l: (l["time_ms"] is None, l["time_ms"] or 0))
        if laps:
            out.append({"car_index": car,
                        "name": d.get("driver_name") or "",
                        "car_model": d.get("car_model") or "",
                        "laps": laps[:RIVAL_LAPS_EACH]})
    out.sort(key=lambda r: (r["laps"][0]["time_ms"] is None,
                            r["laps"][0]["time_ms"] or 0))
    return out[:RIVAL_MAX_CARS]


def build(conn, session_id: int | None = None,
          lap_ids: list[int] | None = None, *,
          every_ms: int = EVERY_MS) -> dict:
    """Everything the page draws, for one session or for named laps.

    Named laps may come from several sessions -- today's run against last
    week's -- but not from different cars or layouts. World coordinates
    only share an origin within one layout, so two layouts would be drawn
    over each other as though they were one circuit; and a line is only
    comparable with one driven in the same car, the rule driving_line
    already applies.

    Raises ValueError, with a sentence meant for the driver, when there is
    nothing to draw.
    """
    if lap_ids:
        laps = []
        for lap_id in dict.fromkeys(lap_ids):
            lap = db.get_lap(conn, lap_id)
            if not lap:
                raise ValueError(f"no lap with id {lap_id}")
            laps.append(lap)
        if len({(l["car"], l["track"], l.get("track_config") or "")
                for l in laps}) > 1:
            raise ValueError(
                "those laps are not all on the same car and layout, so "
                "their coordinates do not share an origin and cannot be "
                "drawn on one map")
    else:
        if session_id is None:
            latest = db.latest_session(conn)
            if latest is None:
                raise ValueError("no sessions recorded yet")
            session_id = latest["id"]
        if db.get_session(conn, session_id) is None:
            raise ValueError(f"no session with id {session_id}")
        laps = db.list_laps(conn, session_id, limit=None)
        if not laps:
            raise ValueError(f"session {session_id} has no laps yet")
    laps.sort(key=lambda l: l["id"])

    # One session's turn numbering, and the bar it was built against. Every
    # lap's corners are found against that same bar, so a corner drawn here
    # is the corner lap_summary reports. Laps from several sessions have no
    # shared numbering, and each is held to its own cornering load -- the
    # way a lap read on its own is.
    asked = sorted({l["session_id"] for l in laps})
    cmap = None
    if len(asked) == 1:
        cmap = turns.corner_map_from(
            conn, turns.basis_lap_ids(conn, asked[0], lap_id=laps[0]["id"]))
    ref_g = cmap["reference"]["reference"] if cmap else None
    turn_list = cmap["turns"] if cmap else []

    drawn, skipped, loaded_all = [], [], []
    shift_lists, full_throttle_rpm = [], []
    for lap in laps:
        samples = db.get_samples(conn, lap["id"])
        ct = bal = derived = None
        if samples:
            ct = analysis.corner_trace(samples, ref_g)
            bal = [analysis.sample_slip_balance(s) for s in samples]
            derived = {
                "lat": [round(v, 2) for v in ct["lat"]],
                "bal": [_r(v, 3) for v in bal],
                "rpm": [None if s.get("rpm") is None
                        else int(round(s["rpm"], -1)) for s in samples],
                "rf": [_r(None if s.get("ride_f") is None
                          else 1000 * s["ride_f"], 1) for s in samples],
                "rr": [_r(None if s.get("ride_r") is None
                          else 1000 * s["ride_r"], 1) for s in samples],
                "y": [_r(s.get("pos_y"), 1) for s in samples],
                "tm": [s.get("t_ms") for s in samples],
                **{f"c{w}": [_r(s.get(f"core_{w}"), 1) for s in samples]
                   for w in TYRES},
                **{f"p{w}": [_r(s.get(f"press_{w}"), 1) for s in samples]
                   for w in TYRES},
            }
        line = trace(samples, every_ms, derived)
        if line is None:
            skipped.append({"lap_id": lap["id"],
                            "lap_number": lap["lap_number"],
                            "why": _skip_reason(samples)})
            continue
        corners = analysis.detect_corners(samples, ref_g)
        if turn_list:
            analysis.label_corners(corners, turn_list)
        usable, why = db.lap_usability(lap)
        # Balance while cornering only. On a straight both axles idle at the
        # same small slip, and under straight-line braking the fronts slip
        # more whatever the balance -- neither says anything about it.
        loaded = [b for b, g in zip(bal, ct["lat"])
                  if b is not None and abs(g) >= ct["threshold_g"]]
        if usable:
            loaded_all.extend(abs(b) for b in loaded)
        lap_shifts = shifts(samples)
        # The surface from the in-game app's suspension travel where it was
        # captured -- faster, and four wheels -- and from the 25 Hz ride
        # height where it was not. Suspension rows are keyed by laps
        # completed when sampled, so this lap is the one before its number.
        lc = lap["lap_number"] - 1
        src = db.best_suspension_source(conn, lap["session_id"], lc)
        if src:
            surf = bump_profile(db.get_suspension_samples(
                conn, lap["session_id"], lc, src), TRAVEL_KEYS, "spline")
        if not src or not any(v is not None for v in surf):
            src, surf = "ride", bump_profile(samples, RIDE_KEYS)
        lockups = analysis.braking_report(lap, samples).get(
            "front_lockup_runs", [])
        if usable:
            shift_lists.append(lap_shifts)
            full_throttle_rpm.extend(
                s["rpm"] for s in samples if s.get("rpm") is not None
                and (s.get("gas") or 0) >= FULL_THROTTLE)
        drawn.append({
            "id": lap["id"],
            "n": lap["lap_number"],
            "session": lap["session_id"],
            "ms": lap["lap_time_ms"],
            "usable": usable,
            "why": why,
            "complete": bool(lap.get("complete", 1)),
            "out": bool(lap.get("out_lap")),
            "pit": bool(lap.get("pitted")),
            "wide": bool(lap.get("invalid")),
            "outlier": bool(lap.get("outlier")),
            "setup": lap.get("setup_name") or "",
            "thr": round(ct["threshold_g"], 3),
            "bal_med": round(median(loaded), 3) if loaded else None,
            "shifts": lap_shifts,
            "surf": surf,
            "surface_src": src,
            "off": off_track(samples),
            "lockups": [{"from": r["from_pos"], "to": r["to_pos"]}
                        for r in lockups],
            "corners": [{"turn": c.get("turn"), "entry": c["entry_pos"],
                         "apex": c["apex_pos"], "exit": c["exit_pos"],
                         "brake": c["brake_point_pos"],
                         "peak_g": c["peak_lat_g"],
                         "min_kmh": c["min_speed_kmh"]} for c in corners],
            **line,
        })
    if not drawn:
        raise ValueError(
            "none of these laps carry position data, so there is no line "
            "to draw. Position recording was added in schema v8; laps "
            "recorded before it cannot be backfilled.")

    # The fastest lap that counted: a real lap time, inside track limits. A
    # lap that ran wide is drawn and is usable everywhere else, but a lap
    # that cut the circuit is not what anyone means by the best line.
    counted = [l for l in drawn if l["usable"] and not l["wide"]]
    best = min(counted, key=lambda l: l["ms"])["id"] if counted else None

    # The highest revs the car showed at full throttle. No redline is
    # recorded anywhere, so this is what the car did rather than its
    # limiter -- a car that is always short-shifted never shows its limiter.
    ceiling = (int(round(_pct(full_throttle_rpm, 0.995), -1))
               if full_throttle_rpm else None)
    for l in drawn:
        l["limiter"] = _limiter_runs(l, ceiling) if ceiling else []

    if cmap:
        turn_marks = [{k: t.get(k) for k in TURN_KEYS} for t in cmap["turns"]]
        unnumbered = [{k: u.get(k) for k in CLUSTER_KEYS}
                      for u in cmap["unnumbered"]]
        detection = (analysis.corner_detection_note(
            ref_g, cmap["reference"]["laps"],
            spread_g=cmap["reference"]["spread_g"],
            shared_basis="the laps these turn numbers were built from")
            if ref_g is not None else None)
        turns_from = cmap["basis_lap_ids"]
        turns_note = (None if turn_marks else
                      "no corners were numbered for this session")
    else:
        turn_marks, unnumbered, detection, turns_from = [], [], None, []
        turns_note = (
            f"these laps come from sessions "
            f"{', '.join(str(s) for s in asked)}, and turn numbers are "
            f"built per session -- two sessions can number the same circuit "
            f"differently -- so none are drawn, and each lap's corners are "
            f"found against its own cornering load")

    session_ids = sorted({l["session"] for l in drawn})
    session = db.get_session(conn, session_ids[0])

    # Complaint presses, on the lap they were pressed on: a press stores how
    # many laps were complete, so it belongs to the next one. A press whose
    # lap is not drawn keeps its place on the track and says so.
    # Opponents are compared within the session they were recorded in: the
    # grid is that session's track, and nothing ties a car index in one
    # session to the same car in another.
    rival_data = (rivals(conn, session_ids[0], session.get("track_length_m"))
                  if len(session_ids) == 1 else [])

    lap_ids_by_number = {(l["session"], l["n"]): l["id"] for l in drawn}
    notes = []
    for sid in session_ids:
        for nt in db.list_notes(conn, sid, limit=100000):
            n = nt["lap_count"] + 1
            notes.append({"session": sid, "n": n,
                          "lap_id": lap_ids_by_number.get((sid, n)),
                          "pos": round(nt["spline"], 4), "tag": nt["tag"],
                          "kmh": _r(nt.get("speed_kmh"))})

    # The surface every lap agrees on: the median of each stretch over the
    # laps that counted. A kerb struck once is pooled away; a kerb taken
    # every lap is not, and reads like a bump.
    profiles = ([l["surf"] for l in drawn if l["usable"]]
                or [l["surf"] for l in drawn])
    pooled = []
    for i in range(SURFACE_SLICES):
        vals = [p[i] for p in profiles if p[i] is not None]
        pooled.append(round(median(vals), 2) if vals else None)
    one = len(session_ids) == 1
    return {
        "session_ids": session_ids,
        "from_laps": bool(lap_ids),
        "car": session["car"],
        "track": session["track"],
        "track_config": session.get("track_config") or "",
        # One session's conditions, or none: the first session's air
        # temperature printed over a map of three would be a claim about
        # the other two.
        "air": session.get("air_temp") if one else None,
        "road": session.get("road_temp") if one else None,
        "best": best,
        "laps": drawn,
        "skipped": skipped,
        "turns": turn_marks,
        "unnumbered": unnumbered,
        "detection": detection,
        "turns_from_laps": turns_from,
        "turns_note": turns_note,
        # One colour scale for every lap's balance, so a lap that pushed
        # reads as pushing next to one that did not: the 95th percentile of
        # the laps that counted, while cornering.
        "balance_scale": (round(_pct(loaded_all, 0.95), 3)
                          if loaded_all else None),
        "gearing": gearing(shift_lists, ceiling) if shift_lists else None,
        "surface": {"slices": SURFACE_SLICES, "rms_mm": pooled,
                    "laps": len(profiles),
                    "sources": sorted({l["surface_src"] for l in drawn})},
        "notes": notes,
        "orphan_notes": db.count_orphan_notes(conn),
        "lockup_slip": analysis.LOCKUP_SLIP,
        "rivals": rival_data,
        "rival_grid": RIVAL_GRID,
        "track_length_m": session.get("track_length_m"),
    }


def render(data: dict) -> str:
    """The page, with the data embedded."""
    # "<" is escaped inside the JSON so nothing in it can close the script
    # block. A setup name is whatever the driver typed, and "</script>" in
    # one would otherwise end the page's code partway through its data.
    payload = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    track = html.escape(data["track"].replace("_", " ").title())
    page = TEMPLATE.read_text(encoding="utf-8")
    page = page.replace("<title>Driving Lines</title>",
                        f"<title>{track} Driving Lines</title>", 1)
    return page.replace("/*__DATA__*/null", payload, 1)


def default_name(data: dict) -> str:
    """line-map-session-41.html, or named for the laps when laps were asked for."""
    if not data["from_laps"]:
        return f"line-map-session-{data['session_ids'][0]}.html"
    ids = [l["id"] for l in data["laps"]]
    tag = ("-".join(str(i) for i in ids) if len(ids) <= 6
           else f"{ids[0]}-{ids[-1]}-{len(ids)}-laps")
    return f"line-map-laps-{tag}.html"


def write(data: dict, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data), encoding="utf-8")
    return path
