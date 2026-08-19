"""Lap analysis: turn raw samples into compact, LLM-friendly summaries.

Design rule: the model never sees raw 25Hz telemetry. Everything here reduces
a lap to numbers a race engineer would actually reason about.

Pure Python on purpose - no numpy dependency to install on the gaming PC.
"""

import math
from statistics import mean

WHEELS = ("fl", "fr", "rl", "rr")

# AC's wheelSlip sits around 0..3 in normal driving and a few tens in a big
# lock-up or spin. It also emits absurd spikes -- values in the tens of
# thousands -- when a wheel is unloaded, the car is reset, or the physics
# tick straddles a teleport. One such sample poisons a mean badly enough to
# report a slip balance of 6002 on an otherwise ordinary lap, so anything
# past this ceiling is treated as a glitch rather than as data.
SLIP_SANE_MAX = 50.0

# A lap is a stop, a spin, or a tow -- not a representative lap -- when it is
# this much slower than the session's reference.
#
# Deliberately an allowance over the reference rather than a multiple of it.
# A ratio is the wrong shape for a lap time: 1.5x a 1:54 Mugello lap is a 57
# second margin, which no realistic incident reaches, while 1.5x an 8 minute
# Nordschleife lap is four whole minutes. The fraction keeps long tracks
# proportionate (more track, more places to have a moment) and the floor
# keeps short ones from being brutal.
OUTLIER_MARGIN_MS = 25_000
OUTLIER_FRACTION = 0.25

# Corner detection thresholds. The fraction is of the lap's own peak lateral
# g, so the same numbers work for a Formula car and a road car; the absolute
# floor stops a slow lap promoting its own noise into corners.
CORNER_LAT_G_FRACTION = 0.35
CORNER_MIN_LAT_G = 0.35
CORNER_MIN_PEAK_LAT_G = 0.5
# At 25Hz this is half a second of sustained load -- long enough to exclude
# a kerb strike or a twitch of correction, short enough to keep a quick chicane.
CORNER_MIN_SAMPLES = 12
# Speeds within this of the minimum count as "the apex", so a flat-bottomed
# corner puts its apex in the middle of the flat rather than at its start.
APEX_FLAT_TOLERANCE_KMH = 0.5


def outlier_reference(lap_times_ms) -> int | None:
    """The lap time to judge outliers against: the fastest lap seen.

    Takes every lap, not just the ones already marked valid. Deriving the
    reference from valid laps only makes this rule a dependent of the
    dirty-lap rule -- at a track with tight limits every lap can be dirty,
    leaving no reference at all and disabling outlier detection for the
    whole session.
    """
    times = [t for t in lap_times_ms if t and t > 0]
    return min(times) if times else None


def lap_is_outlier(lap_time_ms: int, reference_ms: int | None) -> bool:
    """True if this lap is grossly slower than the session's reference.

    False when there is no reference yet: the first flying lap of a session
    has nothing to be an outlier against.
    """
    if not reference_ms:
        return False
    allowance = max(OUTLIER_MARGIN_MS, OUTLIER_FRACTION * reference_ms)
    return lap_time_ms > reference_ms + allowance


def _sane_slip(*values: float) -> float | None:
    """Mean of the given slip values, or None if any is a glitch.

    Returns None rather than clamping: a spike means the sample is not
    describing real tyre behaviour, so silently substituting 50.0 would be
    inventing data. Callers drop the whole sample instead.
    """
    for v in values:
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            return None
        if abs(v) > SLIP_SANE_MAX:
            return None
    return mean(values)


def _smooth(values: list[float], window: int = 9) -> list[float]:
    """Centered moving average; window forced odd."""
    if window % 2 == 0:
        window += 1
    half = window // 2
    n = len(values)
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def _fmt_time(ms: int) -> str:
    return f"{ms // 60000}:{(ms % 60000) / 1000:06.3f}"


def detect_corners(samples: list[dict]) -> list[dict]:
    """Find corners as sustained regions of lateral acceleration.

    The previous implementation looked for local minima of speed, and
    required the apex to be below 92% of the lap's top speed. That is a
    slow-corner detector: Mugello's Arrabbiata is taken at ~93% of top
    speed, so it -- and every other fast sweeper, the corners that decide
    a lap -- was excluded by construction. Time lost there showed up
    nowhere, because the only corners on the list were the ones that were
    never the problem.

    Lateral load is what makes a corner a corner. A fast sweeper barely
    dents the speed trace but pulls as hard as anything on the lap, so
    that is what gets thresholded here. The apex is then the slowest point
    inside the region, which is the same apex the old code was looking
    for -- it just no longer has to be a global-ish minimum to be found.

    Returns one dict per corner with apex position, min speed, brake point,
    peak steering, and a front-vs-rear slip balance metric around the apex
    (positive = front sliding more = understeer tendency).
    """
    if len(samples) < 50:
        return []

    lat = _smooth([s.get("acc_lat", 0.0) or 0.0 for s in samples], 11)
    peak = max((abs(v) for v in lat), default=0.0)
    if peak < CORNER_MIN_PEAK_LAT_G:
        return []          # an in-lap, or a spin: nothing corner-shaped here

    # Relative to the lap's own peak so this works for a Formula car pulling
    # 3g and a road car pulling 1.1g, with an absolute floor so that a lap
    # spent trundling doesn't promote its own noise into "corners".
    thresh = max(peak * CORNER_LAT_G_FRACTION, CORNER_MIN_LAT_G)

    # A region is contiguous samples above the threshold turning the SAME
    # way. The sign test is what separates an esse into two corners rather
    # than reporting one long one straddling the direction change.
    regions: list[list[int]] = []
    cur: list[int] = []
    cur_sign = 0
    for i, v in enumerate(lat):
        sign = 1 if v > 0 else -1
        if abs(v) >= thresh and (not cur or sign == cur_sign):
            if not cur:
                cur_sign = sign
            cur.append(i)
        else:
            if len(cur) >= CORNER_MIN_SAMPLES:
                regions.append(cur)
            cur = []
            cur_sign = 0
            if abs(v) >= thresh:      # direction flipped: start the next one
                cur, cur_sign = [i], sign
    if len(cur) >= CORNER_MIN_SAMPLES:
        regions.append(cur)

    corners = []
    for r in regions:
        entry, exit_ = r[0], r[-1]
        # Apex = slowest point in the region. Where the trace bottoms out
        # flat -- a long constant-radius corner, or a coarse speed channel
        # -- min() would return whichever tie came first, putting the apex
        # at the entry of the flat section rather than its middle and
        # shifting every window that hangs off it. Take the centre of the
        # slowest band instead.
        v_min = min(samples[j]["speed_kmh"] for j in r)
        flat = [j for j in r
                if samples[j]["speed_kmh"] <= v_min + APEX_FLAT_TOLERANCE_KMH]
        apex = flat[len(flat) // 2]
        stats = _corner_stats(samples, entry, apex, exit_)
        seg_lat = [lat[j] for j in r]
        signed_peak = max(seg_lat, key=abs)
        stats["peak_lat_g"] = round(abs(signed_peak), 2)
        # Absolute left/right needs a convention AC does not document, so
        # report the sign instead: corners sharing a turn_sign turn the same
        # way, which is what correlating tyre temperatures actually needs.
        stats["turn_sign"] = 1 if signed_peak > 0 else -1
        stats["entry_pos"] = round(samples[entry]["norm_pos"], 4)
        stats["exit_pos"] = round(samples[exit_]["norm_pos"], 4)
        corners.append(stats)

    for idx, c in enumerate(corners, 1):
        c["corner"] = idx
    return corners


def _corner_stats(samples, entry_idx, apex_idx, exit_idx) -> dict:
    apex = samples[apex_idx]

    # Brake point: last sustained brake application before the apex.
    brake_pos = None
    for j in range(entry_idx, apex_idx):
        if samples[j]["brake"] > 0.2:
            brake_pos = samples[j]["norm_pos"]
            break

    seg = samples[max(0, apex_idx - 8): apex_idx + 8]

    # Keep only samples where all four wheels report believable slip, so one
    # glitched tick can't decide the corner's balance.
    slip_pairs = []
    clean = []          # the samples those pairs came from
    worst_dropped = 0.0
    for s in seg:
        f = _sane_slip(s["slip_fl"], s["slip_fr"])
        r = _sane_slip(s["slip_rl"], s["slip_rr"])
        if f is not None and r is not None:
            slip_pairs.append((f, r))
            clean.append(s)
        else:
            worst = max(abs(s[f"slip_{w}"]) for w in WHEELS
                        if isinstance(s[f"slip_{w}"], (int, float))
                        and math.isfinite(s[f"slip_{w}"]))
            worst_dropped = max(worst_dropped, worst)
    dropped = len(seg) - len(slip_pairs)

    if slip_pairs:
        front_slip = mean(f for f, _ in slip_pairs)
        rear_slip = mean(r for _, r in slip_pairs)
    else:
        front_slip = rear_slip = None

    # A tick that emits wheelSlip = 30007 is not a tick to trust for steering
    # either. Reuse the sample list the slip filter already vetted, so we
    # can't report a peak_steer_norm of 4021 from a sample we just decided
    # was not data. Fall back to the raw window only if nothing survived.
    steer_src = clean or seg
    peak_steer = max(abs(s["steer"]) for s in steer_src)

    # Throttle-on point after apex. Search well past the exit window:
    # drivers often reach half throttle only after the corner "ends".
    throttle_pos = None
    search_end = min(len(samples), exit_idx + (exit_idx - apex_idx) + 1)
    for j in range(apex_idx, search_end):
        if samples[j]["gas"] > 0.5:
            throttle_pos = samples[j]["norm_pos"]
            break

    out = {
        "apex_pos": round(apex["norm_pos"], 4),
        "min_speed_kmh": round(apex["speed_kmh"], 1),
        "gear": apex["gear"],
        "brake_point_pos": round(brake_pos, 4) if brake_pos is not None else None,
        "throttle_on_pos": round(throttle_pos, 4) if throttle_pos is not None else None,
        # steerAngle from AC is normalized -1..1 (fraction of full lock),
        # not degrees. The old "peak_steer_deg" name invited misreading 0.5
        # as half a degree rather than half lock.
        "peak_steer_norm": round(peak_steer, 2),
        "slip_balance": (round(front_slip - rear_slip, 3)
                         if front_slip is not None else None),
        "front_slip": round(front_slip, 3) if front_slip is not None else None,
        "rear_slip": round(rear_slip, 3) if rear_slip is not None else None,
    }
    if dropped:
        out["slip_samples_dropped"] = dropped
        # Magnitude, not just a count: "3 dropped" reads the same whether
        # the filter caught three 30007s or three 51s, and only one of those
        # says the ceiling is in the right place.
        out["slip_dropped_peak"] = round(worst_dropped, 1)
        out["slip_coverage_pct"] = round(100 * len(slip_pairs) / len(seg), 1)
    return out


def lap_summary(lap: dict, samples: list[dict]) -> dict:
    """Everything an engineer needs to know about one lap, in ~1KB."""
    if not samples:
        return {"error": "no samples for this lap"}

    total = len(samples)
    tyres = {}
    for w in WHEELS:
        tyres[w] = {
            "pressure_avg": round(mean(s[f"press_{w}"] for s in samples), 2),
            "pressure_end": round(mean(
                s[f"press_{w}"] for s in samples[-total // 10 or 1:]), 2),
            "core_temp_avg": round(mean(s[f"core_{w}"] for s in samples), 1),
        }

    corners = detect_corners(samples)
    slip_balances = [c["slip_balance"] for c in corners
                     if c["slip_balance"] is not None]

    # If most corners had their slip thrown away, overall_slip_balance is an
    # average of whatever survived and should not be read as confident.
    filtered = [c for c in corners if c.get("slip_samples_dropped")]
    slip_quality = None
    if corners and (len(slip_balances) < len(corners) or filtered):
        slip_quality = {
            "corners_with_balance": len(slip_balances),
            "corners_total": len(corners),
            "corners_with_dropped_samples": len(filtered),
            "peak_dropped_slip": max(
                (c["slip_dropped_peak"] for c in filtered), default=None),
        }

    return {
        "lap_id": lap["id"],
        "car": lap["car"],
        "track": lap["track"] + (f"/{lap['track_config']}"
                                 if lap.get("track_config") else ""),
        "lap_time": _fmt_time(lap["lap_time_ms"]),
        "lap_time_ms": lap["lap_time_ms"],
        "valid": bool(lap["valid"]),
        "top_speed_kmh": round(max(s["speed_kmh"] for s in samples), 1),
        "time_full_throttle_pct": round(
            100 * sum(1 for s in samples if s["gas"] > 0.95) / total, 1),
        "time_braking_pct": round(
            100 * sum(1 for s in samples if s["brake"] > 0.1) / total, 1),
        "time_coasting_pct": round(
            100 * sum(1 for s in samples
                      if s["gas"] < 0.05 and s["brake"] < 0.05) / total, 1),
        "peak_lat_g": round(max(abs(s["acc_lat"]) for s in samples), 2),
        "peak_braking_g": round(min(s["acc_lon"] for s in samples), 2),
        "avg_ride_height_f": round(mean(s["ride_f"] for s in samples), 4),
        "avg_ride_height_r": round(mean(s["ride_r"] for s in samples), 4),
        "tyres": tyres,
        "overall_slip_balance": round(mean(slip_balances), 3)
        if slip_balances else None,
        "slip_quality": slip_quality,
        "balance_note": ("positive slip_balance = front slides more "
                         "(understeer); negative = rear (oversteer)"),
        "setup": lap.get("setup_name") or None,
        "corners": corners,
    }


def _resample_by_spline(samples: list[dict], key: str,
                        pos_key: str, buckets: int) -> list[float | None]:
    """Average `key` into fixed track-position buckets.

    Comparing two cars sample-by-sample is meaningless -- they're at
    different places at any given instant. Track position is the only shared
    axis, so both traces get binned onto it before differencing.
    """
    sums = [0.0] * buckets
    counts = [0] * buckets
    for s in samples:
        val = s.get(key)
        pos = s.get(pos_key)
        if val is None or pos is None:
            continue
        if not (math.isfinite(val) and math.isfinite(pos)):
            continue
        b = min(int(pos * buckets), buckets - 1)
        sums[b] += val
        counts[b] += 1
    return [sums[i] / counts[i] if counts[i] else None
            for i in range(buckets)]


def _field_liveness(samples: list[dict], key: str) -> dict:
    """How much of `key` actually arrived, and whether it ever varies.

    Online, AC may simply not transmit a remote car's pedal inputs. The
    failure is silent: the field is present and reads 0.0 forever. A column
    that is 100% populated and completely constant is therefore evidence of
    absence, not of a driver who never brakes.
    """
    vals = [s[key] for s in samples
            if s.get(key) is not None and math.isfinite(s[key])]
    if not vals:
        return {"present": False, "populated_pct": 0.0, "varies": False}
    lo, hi = min(vals), max(vals)
    return {
        "present": True,
        "populated_pct": round(100 * len(vals) / len(samples), 1),
        "varies": (hi - lo) > 1e-6,
        "min": round(lo, 3),
        "max": round(hi, 3),
    }


def _threshold_crossings(trace: list[float | None], threshold: float,
                         buckets: int) -> list[float]:
    """Track positions where a trace rises past `threshold`."""
    out = []
    prev = None
    for i, v in enumerate(trace):
        if v is None:
            continue
        if prev is not None and prev <= threshold < v:
            out.append(round(i / buckets, 4))
        prev = v
    return out


def compare_to_rival(my_samples: list[dict], rival_samples: list[dict],
                     buckets: int = 100) -> dict:
    """Where a rival is faster than you, by track position.

    my_samples use 'norm_pos'/'speed_kmh'; rival samples use 'spline'.
    """
    if not my_samples:
        return {"error": "no samples for your lap"}
    if not rival_samples:
        return {"error": "no samples stored for that rival lap"}

    mine = _resample_by_spline(my_samples, "speed_kmh", "norm_pos", buckets)
    theirs = _resample_by_spline(rival_samples, "speed_kmh", "spline", buckets)

    covered = sum(1 for t in theirs if t is not None)
    segments = []
    for i in range(buckets):
        if mine[i] is None or theirs[i] is None:
            continue
        segments.append({
            "pos": round(i / buckets, 2),
            "my_speed_kmh": round(mine[i], 1),
            "their_speed_kmh": round(theirs[i], 1),
            "delta_kmh": round(theirs[i] - mine[i], 1),
        })

    worst = sorted(segments, key=lambda s: -s["delta_kmh"])[:8]

    inputs = {
        "gas": _field_liveness(rival_samples, "gas"),
        "brake": _field_liveness(rival_samples, "brake"),
    }
    result = {
        "track_coverage_pct": round(100 * covered / buckets, 1),
        "compared_segments": len(segments),
        "mean_delta_kmh": round(
            mean(s["delta_kmh"] for s in segments), 2) if segments else None,
        "worst_deficits": worst,
        "rival_input_fields": inputs,
        "note": "delta_kmh positive means the rival is faster at that point",
    }

    # Braking and throttle points are only meaningful if the server actually
    # sent the inputs. A constant column means it didn't.
    if inputs["brake"]["present"] and inputs["brake"]["varies"]:
        their_brake = _resample_by_spline(
            rival_samples, "brake", "spline", buckets)
        my_brake = _resample_by_spline(
            my_samples, "brake", "norm_pos", buckets)
        result["brake_points"] = {
            "theirs": _threshold_crossings(their_brake, 0.2, buckets),
            "mine": _threshold_crossings(my_brake, 0.2, buckets),
        }
    if inputs["gas"]["present"] and inputs["gas"]["varies"]:
        their_gas = _resample_by_spline(
            rival_samples, "gas", "spline", buckets)
        my_gas = _resample_by_spline(my_samples, "gas", "norm_pos", buckets)
        result["throttle_on_points"] = {
            "theirs": _threshold_crossings(their_gas, 0.5, buckets),
            "mine": _threshold_crossings(my_gas, 0.5, buckets),
        }
    if not (inputs["brake"]["present"] and inputs["brake"]["varies"]):
        result["inputs_note"] = (
            "This server is not transmitting remote-car pedal inputs, so "
            "brake and throttle points are unavailable for opponents. Speed "
            "by track position still shows where they gain.")
    return result


def _time_at_positions(samples: list[dict], grid: list[float]):
    """Elapsed lap time at each track position, or None where not covered.

    norm_pos has to be forced monotonic first. A car that runs wide, spins,
    or reverses sends it backwards, and interpolating through that produces
    a delta trace with time flowing the wrong way -- which reads as a huge
    phantom gain exactly where the driver lost the most.
    """
    pts: list[tuple[float, float]] = []
    high = -1.0
    for s in samples:
        pos, t = s.get("norm_pos"), s.get("t_ms")
        if pos is None or t is None:
            continue
        if not (math.isfinite(pos) and math.isfinite(t)):
            continue
        if pos <= high:
            continue
        high = pos
        pts.append((pos, float(t)))
    if len(pts) < 2:
        return [None] * len(grid)

    out: list[float | None] = []
    j = 0
    for g in grid:
        if g < pts[0][0] or g > pts[-1][0]:
            out.append(None)
            continue
        while j + 1 < len(pts) and pts[j + 1][0] < g:
            j += 1
        p0, t0 = pts[j]
        p1, t1 = pts[min(j + 1, len(pts) - 1)]
        span = p1 - p0
        out.append(t0 if span <= 0 else t0 + (t1 - t0) * (g - p0) / span)
    return out


def delta_by_position(lap_a: dict, samples_a: list[dict],
                      lap_b: dict, samples_b: list[dict],
                      segments: int = 20) -> dict:
    """Where one lap gained or lost time against another, by track position.

    Corner metrics only describe corners. When time goes missing between
    them -- a slower exit bleeding down a straight, a fast sweeper no
    detector flagged -- a table of apexes cannot show it, because nothing
    in that table covers the ground where it went.

    This is the standard delta trace: cumulative time at each point on
    track, differenced. Positive means lap_b is behind lap_a there. The
    per-segment `gain_ms` is what to read -- the cumulative figure only
    says the gap exists, the segment figure says where it opened.
    """
    fine = 200
    grid = [i / fine for i in range(fine + 1)]
    ta = _time_at_positions(samples_a, grid)
    tb = _time_at_positions(samples_b, grid)

    delta: list[float | None] = []
    base = None
    for x, y in zip(ta, tb):
        if x is None or y is None:
            delta.append(None)
            continue
        d = y - x
        if base is None:
            base = d          # zero the trace at the first covered point
        delta.append(d - base)

    covered = sum(1 for d in delta if d is not None)
    if covered < fine // 2:
        return {"error": "laps do not overlap enough on track to compare",
                "covered_pct": round(100 * covered / (fine + 1), 1)}

    step = max(1, fine // segments)
    rows = []
    for start in range(0, fine, step):
        end = min(start + step, fine)
        d0, d1 = delta[start], delta[end]
        if d0 is None or d1 is None:
            continue
        rows.append({
            "from": round(start / fine, 3),
            "to": round(end / fine, 3),
            "gain_ms": round(d1 - d0, 1),
            "cumulative_ms": round(d1, 1),
        })

    losses = sorted(rows, key=lambda r: -r["gain_ms"])[:3]
    gains = sorted(rows, key=lambda r: r["gain_ms"])[:3]
    return {
        "lap_a": {"id": lap_a["id"], "time": _fmt_time(lap_a["lap_time_ms"]),
                  "setup": lap_a.get("setup_name", "")},
        "lap_b": {"id": lap_b["id"], "time": _fmt_time(lap_b["lap_time_ms"]),
                  "setup": lap_b.get("setup_name", "")},
        "total_delta_ms": lap_b["lap_time_ms"] - lap_a["lap_time_ms"],
        "note": "positive = lap_b is slower there. gain_ms is time lost in "
                "that segment alone; cumulative_ms is the running total.",
        "track_covered_pct": round(100 * covered / (fine + 1), 1),
        "segments": rows,
        "worst_losses": losses,
        "biggest_gains": gains,
    }


def compare_laps(lap_a: dict, samples_a: list[dict],
                 lap_b: dict, samples_b: list[dict]) -> dict:
    """Corner-by-corner comparison of two laps, matched by track position."""
    ca = detect_corners(samples_a)
    cb = detect_corners(samples_b)

    matched = []
    for c in ca:
        best = min(cb, key=lambda x: abs(x["apex_pos"] - c["apex_pos"]),
                   default=None)
        if best and abs(best["apex_pos"] - c["apex_pos"]) < 0.02:
            matched.append({
                "apex_pos": c["apex_pos"],
                "min_speed_delta_kmh": round(
                    c["min_speed_kmh"] - best["min_speed_kmh"], 1),
                "brake_point_delta": (
                    round(c["brake_point_pos"] - best["brake_point_pos"], 4)
                    if c["brake_point_pos"] is not None
                    and best["brake_point_pos"] is not None else None),
                "slip_balance_delta": (
                    round(c["slip_balance"] - best["slip_balance"], 3)
                    if c["slip_balance"] is not None
                    and best["slip_balance"] is not None else None),
            })

    return {
        "lap_a": {"id": lap_a["id"], "time": _fmt_time(lap_a["lap_time_ms"])},
        "lap_b": {"id": lap_b["id"], "time": _fmt_time(lap_b["lap_time_ms"])},
        "time_delta_ms": lap_a["lap_time_ms"] - lap_b["lap_time_ms"],
        "note": "deltas are lap_a minus lap_b; positive min_speed_delta means"
                " lap_a carried more speed",
        "corners": matched,
    }
