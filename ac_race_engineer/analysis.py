"""Lap analysis: turn raw samples into compact, LLM-friendly summaries.

Design rule: the model never sees raw 25Hz telemetry. Everything here reduces
a lap to numbers a race engineer would actually reason about.

Pure Python on purpose - no numpy dependency to install on the gaming PC.
"""

import math
from collections.abc import Iterable
from statistics import mean, median

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
# Ceiling on believable lateral g, the sibling of SLIP_SANE_MAX above. No
# car in AC sustains this; anything past it is a reset, a wall strike or a
# physics tick straddling a teleport. It matters more here than it looks:
# the corner threshold is a fraction of the lap's own peak, so a single
# spiked sample raises the bar above every genuine corner on the lap.
LAT_G_SANE_MAX = 6.0
# Longitudinal is the same artefact on a different axis, and had no ceiling
# at all: peak_braking_g took the raw minimum of acc_lon, so one spiked
# sample reported a braking figure no car produced.
LON_G_SANE_MAX = 6.0

CORNER_LAT_G_FRACTION = 0.35
CORNER_MIN_LAT_G = 0.35
CORNER_MIN_PEAK_LAT_G = 0.5
# At 25Hz this is half a second of sustained load -- long enough to exclude
# a kerb strike or a twitch of correction, short enough to keep a quick chicane.
CORNER_MIN_SAMPLES = 12

# Median window for the lateral-g trace. A median removes runs shorter than
# half its width, so this clears anything up to 7 samples -- comfortably
# below CORNER_MIN_SAMPLES, which means it can never eat something this
# module would have called a corner.
LAT_G_MEDIAN_WINDOW = 15
# Speeds within this of the minimum count as "the apex", so a flat-bottomed
# corner puts its apex in the middle of the flat rather than at its start.
APEX_FLAT_TOLERANCE_KMH = 0.5

# Pedal travel that counts as "on the brakes". Below this is a driver resting
# a foot on the pedal, not a braking zone.
BRAKE_ON = 0.2
# How many consecutive samples below BRAKE_ON may sit inside one braking zone
# before it counts as two. Drivers modulate, trail off over a bump and pick
# the pedal back up, and at 25Hz a fifth of a second of that is one brake
# application, not two. Wide enough to bridge modulation, far short of the
# coast between one corner's exit and the next one's braking.
BRAKE_ZONE_GAP_SAMPLES = 5


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
    describing real tyre behavior, so silently substituting 50.0 would be
    inventing data. Callers drop the whole sample instead.
    """
    for v in values:
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            return None
        if abs(v) > SLIP_SANE_MAX:
            return None
    return mean(values)


def _median_filter(values: list[float], window: int = 11) -> list[float]:
    """Centerd running median; window forced odd.

    Removes impulses rather than spreading them, which is what a mean does.
    A run shorter than half the window is replaced by its neighbours; a real
    corner, which holds its load for far longer, passes through unchanged.
    """
    if window % 2 == 0:
        window += 1
    half = window // 2
    n = len(values)
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        chunk = sorted(values[lo:hi])
        out.append(chunk[len(chunk) // 2])
    return out


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


def _sane_channel(samples: list[dict], field: str,
                  limit: float) -> list[float]:
    """One acceleration channel with the glitches removed.

    detect_corners has filtered these spikes since the day a six-sample 9g
    burst invented a corner. The two headline figures in lap_summary did
    not: peak_lat_g took the raw maximum and peak_braking_g the raw minimum,
    so the same signal was sanitised for one purpose and passed through
    untouched for another.

    That is worse than a wrong number on a summary, because both are
    compare_runs metrics. Measured on one Sebring run: a single ~10g lateral
    spike reported peak_lat_g as 6.32g averaged over two laps against a real
    2.4, and inflated the metric's own noise estimate until compare_runs
    gave it a resolution of 15g -- a channel that could no longer detect any
    change of any size. A spike does not just misreport the lap it is on, it
    silently disables the comparison it takes part in.
    """
    out = []
    for s in samples:
        v = s.get(field)
        if isinstance(v, (int, float)) and math.isfinite(v) and abs(v) <= limit:
            out.append(v)
    return out


def _lat_g_trace(samples: list[dict]) -> tuple[list[float], int]:
    """The smoothed lateral-g trace, and how many samples were dropped.

    Split out of detect_corners so the cornering load can be measured
    without committing to a threshold -- lat_g_reference needs the trace
    from several laps before any of them can be thresholded.
    """
    # Drop implausible lateral g before anything is derived from it. AC
    # emits the same class of spike here that it does on wheelSlip -- a
    # reset, a wall strike, a tick straddling a teleport -- and the
    # threshold is a fraction of a measured peak, so one bad sample raises
    # the bar above every real corner. Measured: a six-sample 9g spike on a
    # 1.1g road-car lap left one "corner", the artefact, with a fabricated
    # peak_lat_g. Dropped rather than clamped, for the same reason as
    # wheelSlip: a spike is not a hard corner, it is not data.
    raw = [s.get("acc_lat", 0.0) or 0.0 for s in samples]
    sane = [v if (math.isfinite(v) and abs(v) <= LAT_G_SANE_MAX) else 0.0
            for v in raw]
    dropped = sum(1 for a, b in zip(raw, sane) if a != b)

    # Median first, then mean. A moving average does not remove an impulse,
    # it spreads it: a six-sample spike smeared across an 11-sample window
    # becomes a sixteen-sample run above the threshold -- longer than
    # CORNER_MIN_SAMPLES, and so reported as a corner.
    #
    # A median filter removes any run shorter than half its width. Sized
    # from CORNER_MIN_SAMPLES rather than picked: a burst too short to be a
    # corner is, by this module's own definition, not one -- so the same
    # number that decides what counts as a corner decides what counts as a
    # glitch. This catches spikes of plausible magnitude too, which the
    # LAT_G_SANE_MAX ceiling above cannot: 2g on a road car is impossible to
    # rule out by value and obvious by duration.
    return _smooth(_median_filter(sane, LAT_G_MEDIAN_WINDOW), 11), dropped


def _lat_g_peak(lat: list[float]) -> float:
    """The lap's cornering load: a high percentile, not the maximum.

    The threshold is a fraction of this, so basing it on the single largest
    sample lets one unusually hard moment -- or a spike small enough to pass
    the ceiling in _lat_g_trace, like 4g on a road car -- raise the bar above
    the rest of the lap. A real corner holds its load for many samples, so
    the 99th percentile is still a real cornering load, just not a lone one.
    """
    mags = sorted(abs(v) for v in lat)
    return mags[int(0.99 * (len(mags) - 1))] if mags else 0.0


def lat_g_reference(
        sample_sets: list[list[dict]] | Iterable[list[dict]]
) -> float | None:
    """One cornering load for a set of laps, to detect corners against.

    `sample_sets` is one entry per lap, each the sample list that lap's other
    analysis takes -- NOT a flat list of samples, which is the shape every
    other function in this module wants and therefore the mistake a call
    site is most likely to make.

    That misuse is refused rather than absorbed. Passed a flat list, every
    "lap" is a single sample dict, `len(dict) < 50` skips all of them, and
    the answer is None -- which detect_corners reads as "no shared bar" and
    quietly falls back to per-lap thresholds. The caller would get the exact
    behaviour this function exists to replace, from a call that appeared to
    work.

    Pass every lap that is going to be compared. Returns None when none of
    them carries enough lateral load to have corners at all, which leaves
    detect_corners on its per-lap fallback. That is the real "nothing here
    corners" answer, and it should not be reachable by a typo as well.

    The median rather than the mean or the max: one scrappy lap with a big
    correction on it, or one lap driven far harder than the rest, should not
    move the bar for the whole run. The median of five laps is unmoved by
    either.
    """
    peaks = []
    for samples in sample_sets:
        if isinstance(samples, dict):
            raise TypeError(
                "lat_g_reference takes one entry per lap, each a list of "
                "samples; this looks like a flat list of samples. Left "
                "alone it returns None, which drops every lap back to its "
                "own corner-detection threshold without saying so.")
        if not samples or len(samples) < 50:
            continue
        lat, _ = _lat_g_trace(samples)
        peak = _lat_g_peak(lat)
        # An in-lap contributes nothing: including its near-zero peak would
        # drag the reference down and promote noise on every other lap.
        if peak >= CORNER_MIN_PEAK_LAT_G:
            peaks.append(peak)
    return median(peaks) if peaks else None


def detect_corners(samples: list[dict],
                   reference_peak_g: float | None = None) -> list[dict]:
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

    # `dropped` is read at the bottom of this function, where it is attached
    # to every corner as lat_g_samples_dropped.
    lat, dropped = _lat_g_trace(samples)
    own_peak = _lat_g_peak(lat)
    # An in-lap, or a lap spent trundling: nothing corner-shaped here. Note
    # this is not the spin case -- a spin produces a very large lateral g,
    # which the ceiling in _lat_g_trace deals with, not a small one.
    #
    # Judged on the lap's own load and never on the shared reference: a lap
    # spent limping round is cornerless whatever the rest of the run did,
    # and a reference borrowed from four quick laps would otherwise conjure
    # corners out of its noise.
    if own_peak < CORNER_MIN_PEAK_LAT_G:
        return []

    # Relative to a cornering load, so this works for a Formula car pulling
    # 3g and a road car pulling 1.1g, with an absolute floor so that a lap
    # spent trundling doesn't promote its own noise into "corners".
    #
    # Whose cornering load is the whole question. Taking each lap's own peak
    # made the bar a property of how hard that particular lap was driven:
    # across one Suzuka run peak_lat_g ran 2.78 to 3.42, moving the
    # threshold by 23% and taking every corner near it in and out of
    # existence. Seven corners of seventeen were found on only one side of a
    # comparison. That is worse than untidy -- a corner missing from a lap
    # is not compared on that lap at all, and the corners nearest the
    # threshold are the marginal, low-load ones a setup change is most
    # likely to move. The evidence went missing exactly where it mattered.
    #
    # So the caller passes one reference for every lap it means to compare,
    # and each lap is measured against the same bar. A lap analysed on its
    # own still falls back to its own peak, which is the best available
    # answer when there is nothing to compare it to.
    peak = reference_peak_g if reference_peak_g else own_peak
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
    # Where the search for a braking zone may not go back past: the previous
    # corner's exit. Braking for a corner starts on the straight before it,
    # which is ground no corner region covers, so the lookback has to be free
    # to leave this region -- but not so free that it walks into the last
    # corner's braking and reports it as this one's.
    prev_exit = 0
    for r in regions:
        entry, exit_ = r[0], r[-1]
        # Apex = slowest point in the region. Where the trace bottoms out
        # flat -- a long constant-radius corner, or a coarse speed channel
        # -- min() would return whichever tie came first, putting the apex
        # at the entry of the flat section rather than its middle and
        # shifting every window that hangs off it. Take the center of the
        # slowest band instead.
        v_min = min(samples[j]["speed_kmh"] for j in r)
        flat = [j for j in r
                if samples[j]["speed_kmh"] <= v_min + APEX_FLAT_TOLERANCE_KMH]
        apex = flat[len(flat) // 2]
        stats = _corner_stats(samples, apex, exit_, prev_exit)
        prev_exit = exit_
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
        if dropped:
            # Say it per corner rather than once: the caller reads corners,
            # and a lap that needed glitch filtering is one to look at twice.
            c["lat_g_samples_dropped"] = dropped
    return corners


def _brake_zone_start(samples, apex_idx: int, floor_idx: int) -> int | None:
    """Index where braking for this corner began, or None if it never did.

    The brake point is the moment the driver first got on the pedal for this
    corner, and normal driving does that in a straight line -- before the
    car is loaded up laterally, so before the corner region starts. Searching
    forward from the corner's entry therefore found braking already in
    progress and reported the brake point at turn-in: a corner braked from
    0.400 to 0.485 with turn-in at ~0.48 reported ~0.48, and the 8% of the
    lap where the driver was actually slowing the car was invisible. Since
    compare_laps differences these positions between laps, the "brake point
    delta" it produced was a turn-in delta.

    So work backwards from the apex instead: find the last sample on the
    brakes (skipping the coast between brake release and the apex), then walk
    back through that braking run to its first sample, tolerating
    BRAKE_ZONE_GAP_SAMPLES of modulation. `floor_idx` bounds the walk so it
    cannot reach the previous corner's braking.
    """
    end = None
    for j in range(apex_idx, floor_idx - 1, -1):
        if samples[j]["brake"] > BRAKE_ON:
            end = j
            break
    if end is None:
        return None

    start, gap = end, 0
    for j in range(end - 1, floor_idx - 1, -1):
        if samples[j]["brake"] > BRAKE_ON:
            start, gap = j, 0
        else:
            gap += 1
            if gap > BRAKE_ZONE_GAP_SAMPLES:
                break
    return start


def _corner_stats(samples, apex_idx, exit_idx, brake_floor_idx=0) -> dict:
    apex = samples[apex_idx]

    # Brake point: where braking for this corner began. See _brake_zone_start
    # for why this is not a forward search from the corner's entry.
    brake_idx = _brake_zone_start(samples, apex_idx, brake_floor_idx)
    brake_pos = None if brake_idx is None else samples[brake_idx]["norm_pos"]

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


def lap_summary(lap: dict, samples: list[dict],
                reference_peak_g: float | None = None) -> dict:
    """Everything an engineer needs to know about one lap, in ~1KB.

    reference_peak_g comes from lat_g_reference over every lap being looked
    at together. Omit it for a lap read on its own; pass it whenever two
    summaries are going to be compared, or the two corner lists are drawn
    against different bars and need not contain the same corners.
    """
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

    sane_lat = _sane_channel(samples, "acc_lat", LAT_G_SANE_MAX)
    sane_lon = _sane_channel(samples, "acc_lon", LON_G_SANE_MAX)
    accel_dropped = (total - len(sane_lat)) + (total - len(sane_lon))

    corners = detect_corners(samples, reference_peak_g)
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
        # None rather than a number when every sample of a channel was a
        # glitch. Clamping would report the ceiling as though the car had
        # actually pulled it, which is the failure this guard exists to stop.
        "peak_lat_g": (round(max(abs(v) for v in sane_lat), 2)
                       if sane_lat else None),
        "peak_braking_g": round(min(sane_lon), 2) if sane_lon else None,
        # Said out loud rather than filtered silently: a lap that needed
        # this is one to look at twice, and it is the only warning that a
        # comparison including it may be reading a kerb strike as physics.
        "accel_samples_dropped": accel_dropped or None,
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

    # `segments` is an MCP tool argument, so it should mean what it says: the
    # number of rows returned. `step = fine // segments` did not -- integer
    # division made segments=7 return 8 rows and segments=300 return 200,
    # with nothing saying so. Divide the grid into exactly this many spans
    # instead, clamped to what the grid can express: one row per grid point
    # is the finest division there is, and asking for more cannot produce it.
    requested = segments
    segments = max(1, min(int(segments), fine))
    edges = [round(i * fine / segments) for i in range(segments + 1)]

    # A row spans two grid points, and the endpoints of the lap are never
    # both covered: sampling starts just after the line and stops when
    # completedLaps ticks over, so grid[0] and grid[200] are outside the
    # sampled range by a sample or two. Dropping a row because an *endpoint*
    # is uncovered discarded the first and last twentieth of every lap --
    # including the final corner and the run to the line, which is exactly
    # where a bad exit shows up. So each row falls back to the nearest
    # covered points inside its own span, and says when it did.
    rows = []
    for start, end in zip(edges, edges[1:]):
        i0 = _first_covered(delta, start, end)
        i1 = _last_covered(delta, start, end)
        if i0 is None or i1 is None or i0 == i1:
            # Nothing inside this span was sampled on both laps at all.
            rows.append({
                "from": round(start / fine, 3),
                "to": round(end / fine, 3),
                "gain_ms": None,
                "cumulative_ms": None,
                "covered": False,
            })
            continue
        row = {
            "from": round(start / fine, 3),
            "to": round(end / fine, 3),
            "gain_ms": round(delta[i1] - delta[i0], 1),
            "cumulative_ms": round(delta[i1], 1),
        }
        if i0 != start or i1 != end:
            # Report the span actually measured rather than implying the
            # nominal one, so a partial row cannot be read as a full one.
            row["measured_from"] = round(i0 / fine, 3)
            row["measured_to"] = round(i1 / fine, 3)
        rows.append(row)

    timed = [r for r in rows if r["gain_ms"] is not None]
    losses = sorted(timed, key=lambda r: -r["gain_ms"])[:3]
    # Only rows that actually gained: taking the bottom three of the same
    # list reported the smallest loss as the biggest gain, so a lap slower
    # everywhere came back with three "gains" that were all losses.
    gains = [r for r in sorted(timed, key=lambda r: r["gain_ms"])
             if r["gain_ms"] < 0][:3]

    # The rows should account for the whole gap. When they don't -- a
    # telemetry dropout, or a span neither lap covered -- say so rather
    # than let the reader assume the segments add up.
    accounted = round(sum(r["gain_ms"] for r in timed), 1)
    total = lap_b["lap_time_ms"] - lap_a["lap_time_ms"]
    unaccounted = round(total - accounted, 1)

    out = {
        "lap_a": {"id": lap_a["id"], "time": _fmt_time(lap_a["lap_time_ms"]),
                  "setup": lap_a.get("setup_name", "")},
        "lap_b": {"id": lap_b["id"], "time": _fmt_time(lap_b["lap_time_ms"]),
                  "setup": lap_b.get("setup_name", "")},
        "total_delta_ms": total,
        "note": "positive = lap_b is slower there. gain_ms is time lost in "
                "that segment alone; cumulative_ms is the running total. "
                "A segment with gain_ms null was not sampled on both laps.",
        "track_covered_pct": round(100 * covered / (fine + 1), 1),
        "accounted_ms": accounted,
        "unaccounted_ms": unaccounted,
        "segments": rows,
        "worst_losses": losses,
        "biggest_gains": gains,
    }
    if requested != segments:
        # Say it rather than quietly returning a different number of rows
        # than was asked for.
        out["segments_requested"] = requested
    return out


def _first_covered(delta: list, lo: int, hi: int) -> int | None:
    for i in range(lo, hi + 1):
        if delta[i] is not None:
            return i
    return None


def _last_covered(delta: list, lo: int, hi: int) -> int | None:
    for i in range(hi, lo - 1, -1):
        if delta[i] is not None:
            return i
    return None


# --- Student's t, computed rather than looked up -----------------------
#
# This was a fourteen-entry table of 95% critical values with a lookup that
# rounded an untabulated df UP to the next key, so df=21 was judged at
# df=20's 2.09 and anything past df=30 at 1.96 -- always the smaller
# multiplier, always erring toward calling a change real. That is the wrong
# direction for a tool whose entire job is refusing to overclaim.
#
# The table also could not express any confidence level other than 95%,
# which is the thing that had to change: one run asks eight questions of the
# same laps, so each one is judged at 0.05/8 or thereabouts, and there is no
# table of those. A p-value function answers at any level.
#
# The regularised incomplete beta is all that is needed, and math.lgamma
# makes it about forty lines of standard library. No numpy, no scipy: the
# gaming PC gets a stock Python and nothing else. Checked against
# scipy.stats.t across df 1..40 and t 0..8, worst absolute disagreement
# 2.8e-14; the critical values against scipy.stats.t.isf, worst relative
# disagreement 7.3e-15.


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Lentz's method)."""
    maxit, eps, tiny = 400, 3e-16, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, maxit + 1):
        m2 = 2 * m
        for num in (m * (b - m) * x / ((qam + m2) * (a + m2)),
                    -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1.0 + num * d
            if abs(d) < tiny:
                d = tiny
            c = 1.0 + num / c
            if abs(c) < tiny:
                c = tiny
            d = 1.0 / d
            step = d * c
            h *= step
        if abs(step - 1.0) < eps:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log1p(-x))
    # The continued fraction converges quickly on only one side of this
    # point, so the far side is evaluated through the symmetry instead.
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _t_p_value(t: float, df: int) -> float:
    """Two-tailed p for a t statistic: P(|T| >= |t|) under no difference."""
    if df < 1:
        return 1.0
    t = abs(t)
    if not math.isfinite(t):
        return 0.0
    return _betainc(df / 2.0, 0.5, df / (df + t * t))


def _t_crit(df: int, alpha: float) -> float:
    """The |t| a two-tailed test at level `alpha` has to clear."""
    if df < 1 or alpha <= 0.0:
        return float("inf")
    if alpha >= 1.0:
        return 0.0
    lo, hi = 0.0, 2.0
    while _t_p_value(hi, df) > alpha:      # p falls monotonically in t
        lo, hi = hi, hi * 2.0
        if hi > 1e300:
            return float("inf")
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if mid <= lo or mid >= hi:
            break
        if _t_p_value(mid, df) > alpha:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _phi(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def _t_power(delta: float, df: int, t_crit: float, steps: int = 200) -> float:
    """Chance a true difference of `delta` standard errors gets flagged.

    `resolution` alone is misleading: it is the detection threshold, so a
    real change exactly that size is called out about half the time -- 56%
    at df=4, measured. This is what puts a number on it for the difference a
    run actually measured: a noncentral t tail, integrated over the sampling
    distribution of the pooled spread rather than pretending that spread is
    known. Checked against scipy.stats.nct for df 2..40 at several levels,
    worst absolute disagreement 3.7e-7.
    """
    if df < 1 or not math.isfinite(t_crit):
        return 0.0
    delta = abs(delta)
    top = 1.0 + 14.0 / math.sqrt(df)       # covers the chi density's mass
    logc = (-(df / 2.0) * math.log(2.0) - math.lgamma(df / 2.0)
            + math.log(2.0 * df))

    def f(u):                              # u = s/sigma
        if u <= 0.0:
            return 0.0
        lg = (logc + (df / 2.0 - 1.0) * math.log(df * u * u)
              - df * u * u / 2.0 + math.log(u))
        g = math.exp(lg) if lg > -700 else 0.0
        return g * (_phi(delta - t_crit * u) + _phi(-t_crit * u - delta))

    # Two panels. When the threshold is strict the rejection region collapses
    # into a sliver near u=0, and a single evenly spaced rule walks straight
    # past it -- that error reached 1e-2, which is visible in a reported
    # figure.
    split = min(top, max(4.0, delta + 4.0) / t_crit)
    total = 0.0
    for lo, hi in ((0.0, split), (split, top)):
        if hi <= lo:
            continue
        h = (hi - lo) / steps
        acc = 0.0
        for i in range(steps + 1):
            w = 1 if i in (0, steps) else (4 if i % 2 else 2)
            acc += w * f(lo + i * h)
        total += acc * h / 3.0
    return min(1.0, max(0.0, total))


def _sig(x: float, digits: int = 3) -> float:
    """Round to significant figures: p-values are useless rounded to 3dp."""
    if not x or not math.isfinite(x):
        return x
    return round(x, -int(math.floor(math.log10(abs(x)))) + digits - 1)


# What to pull out of a lap summary, and how much of a change is worth
# reporting at all. `floor` guards against a run that happens to be very
# repeatable declaring a physically meaningless difference significant.
RUN_METRICS = [
    ("lap_time_ms", "lap time", ("lap_time_ms",), 1.0, "ms"),
    ("slip_balance", "slip balance", ("overall_slip_balance",), 0.02, ""),
    ("front_load_transfer_pct", "front load transfer",
     ("suspension", "front_load_transfer_pct"), 0.1, "%"),
    ("fl_core_temp", "front-left core temp",
     ("tyres", "fl", "core_temp_avg"), 0.2, "C"),
    ("fl_pressure_end", "front-left end pressure",
     ("tyres", "fl", "pressure_end"), 0.05, "psi"),
    ("top_speed_kmh", "top speed", ("top_speed_kmh",), 0.3, "km/h"),
    ("peak_lat_g", "peak lateral g", ("peak_lat_g",), 0.02, "g"),
    ("coasting_pct", "coasting", ("time_coasting_pct",), 0.2, "%"),
]


def _dig(d: dict, path):
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d if isinstance(d, (int, float)) else None


def _stats(values):
    n = len(values)
    if n == 0:
        return 0, None, None
    mean = sum(values) / n
    if n < 2:
        return n, mean, None
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return n, mean, math.sqrt(var)


# Family-wise confidence. The confirmatory family is RUN_METRICS and
# nothing else, so this is the chance of ANY false "moved" among the
# metrics, and it does not change with how many corners the circuit has.
FAMILY_ALPHA = 0.05

# Key holding a test's raw ingredients until the whole family is known. It
# is stripped before the payload goes out -- a verdict cannot be reached
# metric by metric any more, so the measurement and the judgement have to be
# separate passes.
_TEST = "_test"

# Clears 95% on its own, does not clear the corrected level. Reporting this
# as "within noise" throws away the one part of a null answer that is
# actionable: "no evidence" and "not enough laps yet" call for opposite
# next steps, and only one of them is worth running the test again for.
SUGGESTIVE = "suggestive -- clears 95% alone, not after correction"

# How many corners the exploratory list carries. Fifteen corners on two
# channels is thirty tests, and a payload that prints all of them buries
# the eight that were actually judged.
CORNER_LEADS_SHOWN = 6


def _measure(base, cand, floor):
    """Measure one channel. The verdict waits until the family is known.

    Splitting this out is the whole point: judging each metric the moment it
    was measured is what made a run of 38 questions answer "something moved"
    82.5% of the time with nothing changed. Nothing here decides anything --
    that is _holm for a confirmatory test and _explore for an exploratory
    one, and neither can run until every question has been asked.
    """
    n1, m1, s1 = _stats(base)
    n2, m2, s2 = _stats(cand)
    out = {"baseline_n": n1, "candidate_n": n2}
    if n1 == 0 or n2 == 0:
        return {**out, "verdict": "not measured"}
    out["baseline"] = round(m1, 3)
    out["candidate"] = round(m2, 3)
    out["change"] = round(m2 - m1, 3)
    if n1 < 2 or n2 < 2:
        return {**out, "verdict": "need at least 2 laps a side to see noise"}

    # Pooled within-run spread: how much this metric moves when nothing
    # changed. That is the yardstick, not any absolute threshold.
    df = n1 + n2 - 2
    pooled_var = ((n1 - 1) * s1 ** 2 + (n2 - 1) * s2 ** 2) / df
    se = math.sqrt(pooled_var) * math.sqrt(1 / n1 + 1 / n2)
    diff = m2 - m1
    # Two runs that each repeated exactly is not evidence of anything on its
    # own -- t is infinite for any difference at all -- which is exactly the
    # case the floor below exists to catch.
    t = (0.0 if not diff else math.inf * (1 if diff > 0 else -1)) \
        if se <= 0 else diff / se
    out[_TEST] = {"df": df, "se": se, "t": t, "diff": diff, "floor": floor,
                  "sd": math.sqrt(pooled_var), "p": _t_p_value(t, df)}
    return out


def _effect(t) -> float:
    """The change in units of the channel's own lap-to-lap spread.

    Ranks corners against each other, which p cannot do across channels
    measured on different numbers of laps, and which the raw change cannot
    do across channels measured in different units. A run that repeated
    exactly has no spread to divide by and sorts first.
    """
    if t["sd"] <= 0:
        return math.inf if t["diff"] else 0.0
    return abs(t["diff"]) / t["sd"]


def _holm(entries, alpha=FAMILY_ALPHA):
    """Holm-Bonferroni across the confirmatory family, then decide.

    The family is the run metrics, and only those. Everything here is a
    trade between two ways of being wrong, and both of them were measured
    on this project's own figures -- a 0.25s lap-time spread, a rear
    anti-roll bar moving front load transfer 2.2 points against under 0.3
    of noise -- at a fifteen-corner circuit.

    Judging each of 38 tests at 95% on its own and reporting whatever
    cleared: the per-test false-positive rate was a correct 5%, and the
    chance of a null run naming something that had "moved beyond this
    driver's own lap-to-lap spread" was 82.5%. Four runs in five, and the
    summary line stated it as fact.

    Correcting across all 38 fixed that -- 4.8% -- and cost more than it
    bought. At alpha/38 a real 2.2-point load transfer change was caught 7%
    of the time at two laps a side and 70% at three; a real 500ms lap gain,
    3% at three laps a side. A two-lap test on a quiet channel is the
    premise of this whole tool, and 7% is not a test, it is a coin weighted
    against finding anything. An 82.5% false-positive rate traded for a 93%
    false-negative rate on the headline case is not an improvement.

    What inflated 8 into 38 was the corners, and the corners are not
    hypotheses. They answer "where did it change", which only has an answer
    once something changed; they are diagnostic detail on the metrics, not
    38 independent claims. So they are exploratory (see _explore): measured,
    ranked, reported with an uncorrected p and no verdict, and not counted
    in the family. The confirmatory family stays at 8 however many corners
    the track has, which is the other half of the point -- Suzuka must not
    be a stricter test of a rear bar change than a corner-free oval.

    Measured on the design that shipped, over 2500 null runs at fifteen
    corners: the chance of a false confirmatory "moved" anywhere in the
    payload is 5.0%. At alpha/8 the load transfer change is caught 29% of
    the time at two laps a side and 97% at three, and the 500ms lap gain
    11% at three laps and 39% at five. Lap time is still a poor instrument;
    it is just no longer a poor instrument made worse by the corner count.

    Holm rather than plain Bonferroni because it is uniformly more powerful
    at the same guarantee, and power is scarce here: these are two- and
    three-lap runs. Sorted ascending, test i is judged at alpha/(m-i), and
    the step-down is expressed as an adjusted p-value so each metric can
    still carry its own evidence.
    """
    m = len(entries)
    running = 0.0
    for rank, e in enumerate(sorted(entries, key=lambda x: x[_TEST]["p"])):
        t = e[_TEST]
        running = max(running, min(1.0, (m - rank) * t["p"]))
        t["p_adj"] = running
        # The level this one test is really being held to, defined so that
        # "p below it" and "Holm rejects" are the same statement. That
        # identity is what lets `resolution` be reported in the metric's own
        # units from the same alpha the verdict came from, rather than the
        # two being computed separately and drifting apart.
        t["alpha_used"] = (alpha / m if t["p"] <= 0
                           else min(alpha, alpha * t["p"] / running))
        real = abs(t["diff"]) > t["floor"]
        e["verdict"] = ("moved" if real and running <= alpha
                        else SUGGESTIVE if real and t["p"] <= alpha
                        else "within noise")


def _explore(entries, alpha=FAMILY_ALPHA):
    """Judge an exploratory test on its own, uncorrected, and say so.

    No correction, because these are not members of the family and nothing
    in the payload asserts them: `lead` says where to look next, and the
    word "moved" is reserved for the confirmatory metrics. The price is
    stated rather than paid quietly: 5% of quiet corner tests come back as
    a lead, and over 2500 null runs at fifteen corners 77.6% of payloads
    carried at least one. A lead standing alone under eight metrics that
    all read "within noise" is most likely one of those, which is why the
    payload says so in the same breath as the lead.

    `resolution` and `power` here come from the uncorrected 95%, which is
    the level the lead was picked at, so they mean the same thing they mean
    on a metric: what this corner could have resolved, and how likely the
    difference it actually measured was to stand out.
    """
    for e in entries:
        t = e[_TEST]
        t["alpha_used"] = alpha
        t["p_adj"] = None
        e["lead"] = ("worth a look" if abs(t["diff"]) > t["floor"]
                     and t["p"] <= alpha else "quiet")


def _report(entry):
    """Fill in the numbers behind a verdict, for a test being published.

    Split from _holm because the critical value and the power cost a
    bisection and an integration apiece, and a fifteen-corner run measures
    38 tests to publish eight metrics plus a handful of leads.
    """
    t = entry.pop(_TEST, None)
    if t is None:
        return entry
    crit = _t_crit(t["df"], t["alpha_used"])
    entry["resolution"] = round(max(crit * t["se"], t["floor"]), 3)
    # An exploratory p is named for what it is rather than carrying a
    # footnote: nothing corrected it, and `p_value` next to `p_value` on a
    # metric would read as the same number judged the same way.
    if t["p_adj"] is None:
        entry["p_value_uncorrected"] = _sig(t["p"])
    else:
        entry["p_value"] = _sig(t["p"])
        entry["p_value_adjusted"] = _sig(t["p_adj"])
    if t["se"] > 0:
        entry["power"] = round(
            _t_power(abs(t["diff"]) / t["se"], t["df"], crit), 2)
    return entry


# AC's fuel-usage assist, as a multiplier: 1.0 is 100%, 0.5 the half-rate
# setting a league runs to make a long race a no-stopper, 0 a practice
# session that burns nothing. Bounded because anything past this is not a
# multiplier -- it is a misread page, or a percentage nobody converted --
# and quietly multiplying every figure here by 100 is worse than refusing.
MAX_FUEL_RATE = 10.0

# Floors for the three numbers a fuel plan is built from. Each is what the
# smallest real version of that thing is, rounded down hard.
#
# Loose on purpose: the job is to exclude the absurd, not to adjudicate an
# unusual mod. A tight floor would eventually refuse somebody's kart track or
# their 1960s tiddler, and being wrong about a real car is worse here than
# letting an odd one through -- so each sits well below the smallest real
# thing it is about.
#
#   MIN_TRACK_LENGTH_M  the shortest real circuit. Kart tracks start around
#                       600 m and the shortest car circuits are over a
#                       kilometer, so 100 m -- shorter than a pit lane -- is
#                       not a lap of anywhere.
#   MIN_KM_PER_LITER    the thirstiest plausible car. A turbo-era F1 car or a
#                       Group C prototype does roughly 1.5 km/L flat out, so
#                       0.1 km/L is ten liters per kilometer: nothing burns
#                       that.
#   MIN_TANK_LITERS     the smallest real fuel tank. A kart holds about 8 L
#                       and the smallest cars here are in the tens, so a tank
#                       under a liter is a misread number, not a car.
#
# db.set_fuel_basis refuses against these same three, so there is no band
# where a basis can be stored and then rejected here a session later. The
# bridge already checked its own HTTP arguments against 100 m and 0.1 km/L,
# which is where two of the three come from.
MIN_TRACK_LENGTH_M = 100.0
MIN_KM_PER_LITER = 0.1
MIN_TANK_LITERS = 1.0


def _fuel_input_error(name, value, low, high, integer=False) -> str | None:
    """Why `value` is not a usable number for `name`, or None if it is.

    fuel_plan is reachable from an MCP tool argument, and every one of these
    used to fall straight through into arithmetic: km_per_liter=-2.18 came
    back as liters_per_lap -2.406, a non-integer stop count raised a
    TypeError out of range(), and a stop count above the lap count produced
    stints of zero laps. None of it is reachable through the in-game app,
    all of it is reachable by any other caller, and a plan built on a
    negative burn rate is indistinguishable from a good one until it is
    driven.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{name} must be a number, got {value!r}"
    if integer and value != int(value):
        return f"{name} must be a whole number, got {value!r}"
    # NaN fails this too: every comparison against it is False.
    if not low <= value <= high:
        return f"{name} must be between {low:g} and {high:g}, got {value!r}"
    return None


def fuel_plan(race_laps: int, km_per_liter: float, track_length_m: float,
              tank_liters: float | None = None, stops: int = 1,
              margin_laps: float = 0.6,
              fuel_rate: float | None = None) -> dict:
    """Fuel for a race distance, and whether a stop is forced.

    Every number here was worked out by hand for Mugello, twice, from a
    KM_PER_LITER decrypted out of data.acd and a track length looked up
    rather than measured. Both now arrive from the game, so this holds for
    any car and any circuit without being told anything.

    margin_laps is deliberately fractional: arriving at the flag with two
    thirds of a lap in hand costs about 0.02s a lap in weight and covers a
    burn rate slightly above the nominal, which racing in traffic produces.
    It is applied to the totals and to the no-stop verdict as well as to the
    stints. Deciding "no stop needed" at exactly total == tank, from a
    function whose own stints ask for 0.6 lap more than that, told drivers
    the distance was reachable on a plan that arrives at the flag dry.

    The stop count is a floor, not an instruction. Asking for one stop over
    a distance needing three used to return two stints, "the stop" singular,
    and a negative number in a field called spare -- a plan that cannot be
    driven, presented as a plan.

    fuel_rate is AC's fuel-usage multiplier. None means it was not reported,
    which is said in the payload rather than assumed away: at 200% every
    figure here is half what the car needs.
    """
    bad = (_fuel_input_error("race_laps", race_laps, 1, 100_000, integer=True)
           or _fuel_input_error("stops", stops, 0, 1000, integer=True)
           or _fuel_input_error("margin_laps", margin_laps, 0, 100))
    if bad:
        return {"error": bad}
    if not km_per_liter or not track_length_m:
        return {"error": "no fuel basis recorded for this session; the "
                         "in-game app supplies track length and the car's "
                         "km_per_liter"}
    # The floors are the module constants above, which is what
    # db.set_fuel_basis writes against: a basis the database accepted cannot
    # then be refused here.
    bad = (_fuel_input_error("km_per_liter", km_per_liter,
                             MIN_KM_PER_LITER, 1000)
           or _fuel_input_error("track_length_m", track_length_m,
                                MIN_TRACK_LENGTH_M, 1e6)
           or (tank_liters is not None
               and _fuel_input_error("tank_liters", tank_liters,
                                     MIN_TANK_LITERS, 100_000))
           or (fuel_rate is not None
               and _fuel_input_error("fuel_rate", fuel_rate, 0,
                                     MAX_FUEL_RATE)))
    if bad:
        return {"error": bad}
    if stops >= race_laps:
        return {"error": f"{stops} stops means {stops + 1} stints over "
                         f"{race_laps} laps, and a stint of no laps is not a "
                         f"stint"}

    # 22.0 laps and 1.0 stops are whole numbers that arithmetic below would
    # otherwise carry into a range() call and a lap count of 11.0.
    race_laps, stops = int(race_laps), int(stops)
    rate = 1.0 if fuel_rate is None else float(fuel_rate)
    per_lap = (track_length_m / 1000.0) / km_per_liter * rate
    out = {
        "liters_per_lap": round(per_lap, 3),
        "race_laps": race_laps,
        "margin_laps": margin_laps,
        # Two totals rather than one, named for what they are. The margin
        # belongs in the number that gets put in the car; the distance on
        # its own is what to compare against someone else's arithmetic.
        "total_liters": round(per_lap * (race_laps + margin_laps), 1),
        "distance_liters": round(per_lap * race_laps, 1),
        "totals_include_margin": (
            f"total_liters and laps_per_tank carry the {margin_laps:g}-lap "
            f"margin; distance_liters and laps_per_tank_dry are the distance "
            f"alone. The margin covers a burn rate above the nominal figure "
            f"-- traffic, a restart -- and not a formation lap, which is a "
            f"whole lap more."),
        "track_length_km": round(track_length_m / 1000.0, 3),
        "km_per_liter": km_per_liter,
        "fuel_rate_pct": None if fuel_rate is None else round(rate * 100, 1),
    }
    if fuel_rate is None:
        out["fuel_rate_unknown"] = (
            "AC's fuel-usage multiplier was not reported, so every figure "
            "here assumes 100%. A 50% session needs half of this and a 200% "
            "session twice it, which is usually the thing that decides "
            "whether a stop is needed at all.")

    if per_lap <= 0:
        # A real setting, and the one case where the rest of this is a
        # division by zero rather than a plan.
        out["stop_required_for_fuel"] = False
        out["note"] = ("fuel usage is set to 0% for this session, so nothing "
                       "is burned and no stop can be forced by fuel")
        out["stints"] = [{"stint": 1, "laps": race_laps,
                          "start_with_liters": 0.0}]
        return out

    stints = stops + 1
    if tank_liters:
        dry = tank_liters / per_lap                 # laps to the last drop
        usable = dry - margin_laps                  # laps with the margin kept
        out["tank_liters"] = tank_liters
        out["laps_per_tank"] = round(usable, 1)
        out["laps_per_tank_dry"] = round(dry, 1)
        if usable < 1:
            out["error"] = (
                f"a full tank ({tank_liters:g} L) does not cover one lap "
                f"plus the {margin_laps:g}-lap margin at {per_lap:.3f} L/lap, "
                f"so there is no stint plan to give")
            out["stop_required_for_fuel"] = True
            return out

        longest = int(math.floor(usable))            # laps one stint can hold
        needed_stints = math.ceil(race_laps / longest)
        forced = needed_stints > 1
        out["stop_required_for_fuel"] = forced
        out["minimum_stops"] = needed_stints - 1
        out["note"] = (
            f"a full tank covers {usable:.1f} laps of {race_laps} with the "
            f"margin intact ({dry:.1f} to the last drop), so "
            + (f"{needed_stints - 1} stops are mandatory, not tactical"
               if needed_stints > 2 else
               "the stop is mandatory, not tactical")
            if forced else
            f"a full tank covers {usable:.1f} laps with the margin intact, "
            f"so the distance can be run without stopping for fuel")

        if stints < needed_stints:
            # Never the caller's number when the caller's number cannot be
            # driven. Clamping each stint to the tank instead, which is what
            # this did, produced a two-stint plan for a three-stop race whose
            # only symptom was a negative number in a field called spare.
            out["stops_requested"] = stops
            out["stops_planned"] = needed_stints - 1
            out["stops_note"] = (
                f"{stops} stop{'' if stops == 1 else 's'} cannot cover "
                f"{race_laps} laps: a tank reaches {longest} laps with the "
                f"margin intact, so at least {needed_stints - 1} "
                f"{'is' if needed_stints == 2 else 'are'} required. Planned "
                f"with {needed_stints - 1}.")
            stints = needed_stints

    # An even split makes the longest stint as short as possible, which is
    # what matters when the limit is tyre life rather than fuel.
    base = race_laps // stints
    stint_laps = [base + (1 if i < race_laps % stints else 0)
                  for i in range(stints)]
    plan, carried = [], 0.0
    for i, laps in enumerate(stint_laps):
        # Fuel goes in at a stop and never comes out, so a stint that starts
        # with more than it needs adds nothing -- but it is never short of
        # what it needs, and nothing is clamped to the tank here.
        need = per_lap * (laps + margin_laps)
        add = max(0.0, need - carried)
        entry = {
            "stint": i + 1,
            "laps": laps,
            "start_with_liters" if i == 0 else "add_liters": round(add, 1),
        }
        if tank_liters and carried + add > tank_liters + 1e-9:
            entry["cannot_be_fuelled"] = (
                f"this stint needs {carried + add:.1f} L on board and the "
                f"tank holds {tank_liters:g} L, so {laps} laps cannot be "
                f"driven in one stint")
        # Deliberately unclamped, both here and in what carries forward. The
        # old max(0.0, ...) hid a deficit and then costed the next stint as
        # if the previous one had finished normally.
        carried = carried + add - per_lap * laps
        entry["spare_at_end_liters"] = round(carried, 1)
        plan.append(entry)
    out["stints"] = plan
    if not tank_liters:
        # Nothing is known about the tank, so nothing can be said about
        # whether the distance fits in it. Saying that is not the same as
        # dropping the three keys and leaving a two-stint plan behind,
        # which reads as a stop that was reasoned about.
        out["tank_liters"] = None
        out["stop_required_for_fuel"] = None
        out["note"] = (
            "tank capacity is unknown, so whether a stop is forced by fuel "
            "could not be worked out. The stints below split the distance as "
            "asked and have not been checked against a tank.")
    return out


def compare_runs(baseline: list[dict], candidate: list[dict],
                 corner_tolerance: float = 0.01) -> dict:
    """Did a setup change do anything, given how repeatable the driver is?

    Lap time is the noisiest instrument on the car. Measured spread across
    four laps of an unchanged setup runs 0.3-0.6s, so a change worth less
    than roughly half a second cannot be seen in a short run however
    carefully it is driven -- while front load transfer moved 2.2 points
    for a rear bar change against under 0.3 of noise. Same run, same laps,
    an order of magnitude difference in what each channel can resolve.

    So every metric is judged against its own within-run spread rather than
    against a fixed threshold, and `resolution` reports the smallest change
    the run could have detected -- about half the time, which is what a
    detection threshold means and what `power` puts a number on.

    The metrics are one family, judged together (see _holm); asking them at
    95% each and reporting whatever cleared found something in 82.5% of runs
    where nothing had been changed. Up to eight of them, not eight: the
    family is built from the metrics these laps actually carried, so a run
    recorded without suspension data is corrected across fewer, and
    `tests_in_family` in the payload is the real count rather than a
    constant this docstring can promise. The corners are not in
    that family. They say where a change landed, not whether one happened,
    and counting them made the correction depend on the circuit -- 38 tests
    at Mugello, at which a real 2.2-point load transfer change was caught 7%
    of the time in two laps a side. They are reported as leads instead:
    uncorrected, ranked by effect size, asserted by nothing.

    What that is worth, at alpha/8 against this project's own measured
    noise. A 2.2-point load transfer change (0.3 of spread): caught 29% of
    the time at two laps a side, 97% at three. A 500ms lap gain (0.25s of
    spread): 3% at two laps, 11% at three, 39% at five, 77% at eight. A
    "within noise" on lap time from a short run means almost nothing, and
    `resolution` and `power` are there to say so in the metric's own units.

    corner_tolerance is how far an apex may wander between laps and still
    be the same corner: 0.01 of a lap, about 50m at Mugello. It used to be
    0.02, used as a bucket width rather than a tolerance, which is 105m
    there -- wide enough to average a hairpin together with the kink after
    it and call the result one corner.
    """
    if not baseline or not candidate:
        return {"error": "need laps on both sides of the comparison"}

    metrics = {}
    for key, label, path, floor, units in RUN_METRICS:
        b = [v for v in (_dig(l, path) for l in baseline) if v is not None]
        c = [v for v in (_dig(l, path) for l in candidate) if v is not None]
        r = _measure(b, c, floor)
        r["label"] = label
        if units:
            r["units"] = units
        metrics[key] = r

    corners, unmatched, compared = _compare_corners(
        baseline, candidate, corner_tolerance)

    # The confirmatory family: the metrics, and nothing else. Corners are
    # measured the same way and judged separately, because "where did it
    # change" is not another answer to "did anything change" -- and because
    # a family that grows with the corner count makes the same setup change
    # harder to confirm at Suzuka than at Monza, which is nonsense.
    family = [e for e in metrics.values() if _TEST in e]
    _holm(family)
    leads = [t for c in corners for t in c["tests"] if _TEST in t]
    _explore(leads)

    moved = [m["label"] for m in metrics.values()
             if m.get("verdict") == "moved"]
    suggestive = [m["label"] for m in metrics.values()
                  if m.get("verdict") == SUGGESTIVE]
    flagged = sum(1 for c in corners
                  if any(t.get("lead") == "worth a look" for t in c["tests"]))

    for entry in metrics.values():
        _report(entry)

    # Ranked by effect size rather than filtered by significance: an
    # exploratory list that only shows what cleared 95% is a significance
    # filter wearing a different name, and reads as a finding.
    ranked = sorted(corners, key=lambda c: -max(
        [_effect(t[_TEST]) for t in c["tests"] if _TEST in t] or [-1.0]))
    shown = ranked[:CORNER_LEADS_SHOWN]
    for c in shown:
        for t in c["tests"]:
            if _TEST in t:
                size = _effect(t[_TEST])
                # A run that repeated exactly divides by zero spread; the
                # floor and the p-value still judge it, so the rank is
                # infinite but the reported number is honestly absent.
                t["effect_size"] = _sig(size, 2) if math.isfinite(size) \
                    else None
            _report(t)

    # This is the line a model quotes, so it asserts the confirmatory
    # family and nothing else. It used to be built from the metrics alone
    # while a corner list sat underneath it saying the opposite; now the
    # corners are in it, and are in it as leads.
    if moved:
        head = (f"moved beyond this driver's own lap-to-lap spread, out of "
                f"{len(family)} metrics judged together -- "
                f"{', '.join(moved)}")
    else:
        head = (f"nothing moved beyond noise across {len(family)} metrics "
                f"judged together; check each metric's resolution and power "
                f"before concluding the change did nothing rather than that "
                f"the run was too short")
    if suggestive:
        head += (f". Suggestive but not confirmed, and worth more laps: "
                 f"{', '.join(suggestive)}")
    if flagged:
        where = ", ".join(f"{c['apex_pos']:.3f}" for c in
                          [c for c in ranked
                           if any(t.get("lead") == "worth a look"
                                  for t in c["tests"])][:4])
        head += (f". Separately, {flagged} corner(s) stand out as "
                 f"exploratory leads (apex {where}) -- uncorrected, not "
                 f"findings, and about 5% of quiet corners do this")
    summary = head

    leads_note = (
        f"EXPLORATORY, not findings. {compared} corner(s) were compared on "
        f"up to two channels each; the {len(shown)} with the largest effect "
        f"size are listed, largest first, and {flagged} of all those "
        f"compared cleared an uncorrected 95%. These p-values are NOT "
        f"corrected for how many corners were looked at, so roughly 5% of "
        f"unchanged corner tests come back 'worth a look' and 77.6% of "
        f"fifteen-corner runs with nothing changed at all carried at least "
        f"one. A lead says where to look when a metric moved; standing "
        f"alone under metrics that all read 'within noise', the likeliest "
        f"explanation is that 5%. effect_size is the change in units of "
        f"that corner's own lap-to-lap spread."
        if compared else
        "no corners were matched between these two runs, so there is "
        "nothing to look at corner by corner")

    return {
        "baseline_laps": len(baseline),
        "candidate_laps": len(candidate),
        "metrics": metrics,
        "corners_compared": compared,
        "corner_leads": [
            {"apex_pos": round(c["apex_pos"], 4),
             **{t["channel"]: {k: v for k, v in t.items()
                               if k not in ("channel", _TEST)}
                for t in c["tests"]}}
            for c in shown],
        "corner_leads_note": leads_note,
        "corners_in_one_run_only": unmatched[:12],
        "multiple_comparisons": {
            "method": "holm-bonferroni",
            "family": "the run metrics; corner tests are exploratory",
            "tests_in_family": len(family),
            "exploratory_tests_not_in_family": len(leads),
            "family_confidence": f"{100 * (1 - FAMILY_ALPHA):.0f}%",
            "strictest_threshold": _sig(FAMILY_ALPHA / len(family))
            if family else None,
            "note": f"{len(family)} metrics were tested on this one pair of "
                    f"runs. Judged at 95% each and read as one answer, that "
                    f"flags something far more often than 5%, so they are "
                    f"corrected together: the chance of ANY false 'moved' "
                    f"among them is 5%. Read p_value_adjusted against 0.05. "
                    f"resolution comes from the same corrected level as the "
                    f"verdict rather than from an uncorrected 95%. A verdict "
                    f"of '{SUGGESTIVE}' means p cleared 0.05 alone but not "
                    f"the corrected level -- that is 'not enough laps yet', "
                    f"not 'no evidence'. The {len(leads)} corner tests are "
                    f"NOT in the family and assert nothing; see "
                    f"corner_leads_note.",
        },
        "resolution_note": "resolution is the smallest change these laps "
                           "could detect about half the time -- a real change "
                           "that size clears it on roughly one run in two, so "
                           "a change has to be comfortably larger to be seen "
                           "reliably. `power` is the chance of flagging a "
                           "change the size actually measured. Where a "
                           "resolution is a round number it is that metric's "
                           "floor for a physically meaningful change rather "
                           "than a statistic.",
        "summary": summary,
    }


# How much of a change is worth reporting at all on a corner channel, the
# corner-level sibling of the floors in RUN_METRICS.
CORNER_CHANNELS = [("slip_balance", 0.05), ("min_speed_kmh", 0.5)]


def _corner_clusters(laps: list[dict], tolerance: float) -> list[list[tuple]]:
    """Group one run's corner observations into pieces of road.

    Bucketing positions with round(pos / tol) * tol fails three ways at
    once, all of them measured: apexes at 0.0299 and 0.0301 -- a meter apart
    -- land in different buckets and each corner comes back n=2 from 3 laps; a
    bucket 105m wide swallows two genuine corners, so n=6 from 3 laps
    inflates df from 4 to 10 on samples that are not independent and the
    "corner" is the mean of a hairpin and a kink; and the reported position
    is the bucket center, up to half a bucket from any real apex.

    So: group by proximity, then split any group holding two corners from
    the same lap at its widest internal gap. A car passes an apex once a
    lap, so a duplicate is proof two corners were pooled -- which caps every
    cluster at one observation per lap and keeps df honest by construction.
    """
    obs = []
    for i, lap in enumerate(laps):
        for c in lap.get("corners") or []:
            pos = c.get("apex_pos")
            if isinstance(pos, (int, float)):
                obs.append((float(pos), i, c))
    obs.sort(key=lambda o: o[0])

    groups, cur = [], []
    for o in obs:
        if cur and o[0] - cur[-1][0] > tolerance:
            groups.append(cur)
            cur = []
        cur.append(o)
    if cur:
        groups.append(cur)

    out, queue = [], groups
    while queue:
        g = queue.pop()
        seen = [o[1] for o in g]
        if len(g) < 2 or len(set(seen)) == len(seen):
            out.append(g)
            continue
        _, k = max((g[i + 1][0] - g[i][0], i) for i in range(len(g) - 1))
        queue += [g[:k + 1], g[k + 1:]]
    return sorted(out, key=lambda g: g[0][0])


def _compare_corners(baseline, candidate, tolerance):
    """Pair each baseline corner with one candidate corner, and test both."""
    b_groups = _corner_clusters(baseline, tolerance)
    c_groups = _corner_clusters(candidate, tolerance)
    b_pos = [mean(o[0] for o in g) for g in b_groups]
    c_pos = [mean(o[0] for o in g) for g in c_groups]

    # Nearest first, one-to-one. Taking each baseline corner's nearest
    # candidate independently lets two baseline corners claim the same one.
    taken_b, taken_c, pairs = set(), set(), []
    for _, i, j in sorted((abs(x - y), i, j)
                          for i, x in enumerate(b_pos)
                          for j, y in enumerate(c_pos)
                          if abs(x - y) <= tolerance):
        if i in taken_b or j in taken_c:
            continue
        taken_b.add(i)
        taken_c.add(j)
        pairs.append((i, j))

    corners = []
    for i, j in sorted(pairs, key=lambda p: b_pos[p[0]]):
        tests = []
        for field, floor in CORNER_CHANNELS:
            b = [o[2][field] for o in b_groups[i]
                 if isinstance(o[2].get(field), (int, float))]
            c = [o[2][field] for o in c_groups[j]
                 if isinstance(o[2].get(field), (int, float))]
            # A corner is comparable on whichever channels it has. Requiring
            # slip balance -- which detect_corners drops whenever the slip
            # samples look like glitches -- threw away corners whose minimum
            # speed had moved 15 km/h on every lap.
            if not b or not c:
                continue
            entry = _measure(b, c, floor)
            entry["channel"] = field
            tests.append(entry)
        if tests:
            corners.append({"apex_pos": (b_pos[i] + c_pos[j]) / 2,
                            "tests": tests})

    # A corner the detector found on only one side is not evidence of
    # nothing; it used to be dropped without a word.
    unmatched = ([{"side": "baseline", "apex_pos": round(p, 4)}
                  for k, p in enumerate(b_pos) if k not in taken_b]
                 + [{"side": "candidate", "apex_pos": round(p, 4)}
                    for k, p in enumerate(c_pos) if k not in taken_c])
    return corners, unmatched, len(corners)


LINE_MAX_POINTS = 200
LINE_DEFAULT_POINTS = 100


def _binned_line(samples: list[dict], points: int) -> list[dict | None]:
    """Average each channel within equal slices of track position.

    Binned by norm_pos rather than by time, so two laps driven at different
    speeds line up slice for slice and can be subtracted. A slice the car
    never sampled is None rather than interpolated -- a gap in a driving
    line is worth seeing, and inventing a point there would draw the car
    through somewhere it never went.
    """
    bins: list[list[dict]] = [[] for _ in range(points)]
    for s in samples:
        pos = s.get("norm_pos")
        if pos is None:
            continue
        i = min(points - 1, max(0, int(pos * points)))
        bins[i].append(s)

    out: list[dict | None] = []
    for i, group in enumerate(bins):
        placed = [s for s in group if s.get("pos_x") is not None]
        if not placed:
            out.append(None)
            continue
        rides = [s["ride_f"] for s in group if s.get("ride_f") is not None]
        entry = {
            "pos": round((i + 0.5) / points, 4),
            "x": round(mean(s["pos_x"] for s in placed), 2),
            "z": round(mean(s["pos_z"] for s in placed), 2),
            "speed_kmh": round(mean(s["speed_kmh"] for s in group), 1),
        }
        if rides:
            # Ride height in mm, and how much it moved within this slice.
            # The spread is the bump proxy: a smooth stretch holds the car
            # at a near-constant height, a broken one does not.
            entry["ride_f_mm"] = round(1000 * mean(rides), 1)
            entry["ride_f_range_mm"] = round(1000 * (max(rides)
                                                     - min(rides)), 1)
        out.append(entry)
    return out


def driving_line(lap: dict, samples: list[dict], points: int = 0,
                 other_lap: dict | None = None,
                 other_samples: list[dict] | None = None) -> dict:
    """Where the car actually went, slice by slice around the lap.

    norm_pos has always said where the car was ALONG the lap. Position says
    where it was across it, which is the whole of what a line is -- so until
    the samples carried it, "I took a wider entry" was a claim nothing here
    could check.

    Pass a second lap to get `separation_m`: the straight-line distance
    between where the two cars were at the same point of the circuit. That
    is the number that answers whether two laps were driven on different
    lines, and how much.

    Laps recorded before the position columns existed report
    has_position: false and nothing else. There is no backfill -- the data
    was never captured.
    """
    points = points or LINE_DEFAULT_POINTS
    points = max(10, min(LINE_MAX_POINTS, int(points)))

    if not samples:
        return {"error": "no samples for this lap"}
    if all(s.get("pos_x") is None for s in samples):
        return {
            "lap_id": lap["id"],
            "has_position": False,
            "error": "this lap has no position data. Position recording "
                     "was added in schema v8; laps recorded before it "
                     "cannot be backfilled because the coordinates were "
                     "never captured.",
        }

    line = _binned_line(samples, points)
    out = {
        "lap_id": lap["id"],
        "lap_time": _fmt_time(lap["lap_time_ms"]),
        "has_position": True,
        "points": points,
        "note": "x/z are world metres from the track's own origin; the "
                "numbers only mean anything relative to each other. "
                "ride_f_range_mm is how much the front ride height moved "
                "within the slice -- the higher it is, the rougher the "
                "surface there.",
    }

    # `is not None` on the samples too, not truthiness. A caller that asked
    # for a comparison and passed a lap with no stored samples would
    # otherwise fall through the whole branch silently, and a payload with
    # no comparison in it and no comparison_error is indistinguishable from
    # one where no comparison was asked for. Skipping is only correct when
    # nothing was requested.
    if other_lap is not None and other_samples is not None:
        if not other_samples:
            out["comparison_error"] = (
                f"lap {other_lap['id']} has no telemetry samples stored, so "
                f"there is no line to compare against")
        elif all(s.get("pos_x") is None for s in other_samples):
            out["comparison_error"] = (
                f"lap {other_lap['id']} has no position data")
        else:
            theirs = _binned_line(other_samples, points)
            gaps = []
            for mine, yours in zip(line, theirs):
                if mine is None or yours is None:
                    continue
                d = math.hypot(mine["x"] - yours["x"], mine["z"] - yours["z"])
                mine["separation_m"] = round(d, 2)
                gaps.append((d, mine["pos"]))
            if gaps:
                gaps.sort(reverse=True)
                out["compared_with"] = {
                    "lap_id": other_lap["id"],
                    "lap_time": _fmt_time(other_lap["lap_time_ms"]),
                    "mean_separation_m": round(
                        mean(d for d, _ in gaps), 2),
                    "max_separation_m": round(gaps[0][0], 2),
                    "most_different_at": [
                        {"pos": p, "separation_m": round(d, 2)}
                        for d, p in gaps[:5]],
                }
            else:
                # Both laps carry position and still no slice holds both:
                # two partial laps that stopped in different places, or two
                # sets of gaps that happen not to overlap. Rare, and it
                # leaves the payload in exactly the state the checks above
                # exist to prevent -- no comparison, and nothing saying why.
                out["comparison_error"] = (
                    f"lap {other_lap['id']} and this one have no slice of "
                    f"track in common, so there is nothing to subtract. "
                    f"Both carry position; they just never cover the same "
                    f"part of the lap.")

    measured = [p for p in line if p is not None]
    rough = [p for p in measured if "ride_f_range_mm" in p]
    if rough:
        rough.sort(key=lambda p: -p["ride_f_range_mm"])
        out["roughest_sections"] = [
            {"pos": p["pos"], "ride_f_range_mm": p["ride_f_range_mm"],
             "speed_kmh": p["speed_kmh"]}
            for p in rough[:6]]

    out["slices_measured"] = len(measured)
    out["slices_empty"] = points - len(measured)
    out["line"] = line
    return out


def compare_laps(lap_a: dict, samples_a: list[dict],
                 lap_b: dict, samples_b: list[dict]) -> dict:
    """Corner-by-corner comparison of two laps, matched by track position."""
    # One bar for both laps. Detecting each against its own peak meant the
    # harder-driven lap dropped its marginal corners, and a corner missing
    # from one side simply does not appear in the comparison -- so the two
    # laps were being compared on whichever corners they happened to agree
    # existed.
    ref = lat_g_reference([samples_a, samples_b])
    ca = detect_corners(samples_a, ref)
    cb = detect_corners(samples_b, ref)

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
