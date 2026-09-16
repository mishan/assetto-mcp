"""Lap analysis: turn raw samples into compact, LLM-friendly summaries.

Design rule: the model never sees raw 25Hz telemetry. Everything here reduces
a lap to numbers a race engineer would actually reason about.

Pure Python on purpose - no numpy dependency to install on the gaming PC.
"""

import itertools
import math
import random
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


def _usable(lap: dict) -> tuple:
    """(usable, reason-if-not) for a lap's time.

    Imported lazily from db to keep the one definition of usability in one
    place without analysis and db importing each other at module scope.
    """
    from .db import lap_usability
    return lap_usability(lap)


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


def sample_slip_balance(s: dict) -> float | None:
    """Front slip minus rear slip at one sample; None where either axle glitched.

    The corner metric's quantity and sign -- positive is the front sliding
    more -- read at a single point instead of averaged round an apex, so a
    whole lap can be coloured by it.
    """
    f = _sane_slip(s.get("slip_fl"), s.get("slip_fr"))
    r = _sane_slip(s.get("slip_rl"), s.get("slip_rr"))
    return None if f is None or r is None else f - r


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

    For peak_lat_g that is worse than a wrong number on a summary, because
    it is one of RUN_METRICS. Measured on one Sebring run: a single ~10g
    lateral spike reported peak_lat_g as 6.32g averaged over two laps
    against a real 2.4, and inflated the metric's own noise estimate until
    compare_runs gave it a resolution of 15g -- a channel that could no
    longer detect a change of any size. A spike does not just misreport the
    lap it is on, it silently disables the comparison it takes part in.

    peak_braking_g is not in RUN_METRICS and carries none of that. It is
    filtered because it is the same signal on the other axis and a braking
    figure no car could produce is worth removing on its own account -- and
    because leaving one of a matched pair unfiltered is how the first one
    came to be missed.
    """
    return [s[field] for s in samples if _is_sane(s.get(field), limit)]


def _is_sane(v, limit: float) -> bool:
    """One acceleration reading that is a number, finite, and physical."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and abs(v) <= limit)


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


def lat_g_reference_detail(
        sample_sets: Iterable[list[dict]]) -> dict:
    """One cornering load for a set of laps, and how firm that number is.

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

    Pass every lap that is going to be compared. `reference` is None when
    none of them carries enough lateral load to have corners at all, which
    leaves detect_corners on its per-lap fallback. That is the real
    "nothing here corners" answer, and it should not be reachable by a
    typo as well.

    The median rather than the mean or the max: one scrappy lap with a big
    correction on it, or one lap driven far harder than the rest, should not
    move the bar for the whole run. The median of five laps is unmoved by
    either.

    `laps` is reported because that claim gets weaker as the count falls,
    and at two it is not true at all -- the median of two IS their mean, so
    a single scrappy lap drags the bar by half its own deviation. compare_laps
    passes exactly two, so this is not a hypothetical, and a caller that
    reports the reference should report how many laps produced it.
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
    return {"reference": median(peaks) if peaks else None,
            "laps": len(peaks),
            "spread_g": (round(max(peaks) - min(peaks), 3)
                         if len(peaks) > 1 else 0.0)}


def lat_g_reference(
        sample_sets: Iterable[list[dict]]) -> float | None:
    """The shared cornering load alone, for callers that only threshold."""
    return lat_g_reference_detail(sample_sets)["reference"]


def corner_threshold(peak_g: float) -> float:
    """The lateral g a region has to hold to be a corner, given a cornering load.

    One place for the rule, because two things state it: detect_corners
    applies it and corner_detection_note reports it, and a note that drifted
    from the detector would describe a bar nothing was held to.
    """
    return max(peak_g * CORNER_LAT_G_FRACTION, CORNER_MIN_LAT_G)


def corner_trace(samples: list[dict],
                 reference_peak_g: float | None = None) -> dict:
    """The lateral-g trace corner detection reads, and the bar it is held to.

    For anything that has to show the reasoning rather than the result. A
    turn split in two, or a kink that was never numbered, is explained by
    this trace against this bar -- the same smoothed trace detect_corners
    thresholds, not the raw channel, which would show corners crossing a
    line the detector never saw them cross.
    """
    lat, dropped = _lat_g_trace(samples)
    own = _lat_g_peak(lat)
    bar = own if reference_peak_g is None else reference_peak_g
    return {"lat": lat, "threshold_g": corner_threshold(bar),
            "own_peak_g": own, "dropped": dropped}


def corner_detection_note(reference: float | None, laps: int,
                          own_peak: float | None = None,
                          spread_g: float | None = None,
                          shared_basis: str | None = None) -> dict:
    """What bar the corners on this payload were found against.

    Reported everywhere corners are, because it decides which corners exist
    and it is not the same number in every tool. lap_summary read on its own
    uses the lap's own peak; the same lap inside compare_runs uses the bar
    shared across the comparison. Both are right, they disagree by design,
    and a driver who reads one and then the other was previously given two
    corner lists and no way to know why they differed.

    `reference is not None` throughout rather than truthiness: it is
    float | None, and 0.0 is a real reference meaning "nothing in this run
    cornered". Treating that as absent would report the per-lap basis for a
    payload whose corners were found against the shared one -- a note about
    provenance that is wrong about provenance is worse than no note.

    `laps` is how many contributed to the reference; 0 means the caller did
    not say. The count is reported only when it is known, and the caution
    is worded for the case it actually describes -- a warning about a
    two-lap median attached to a payload built from one lap, or from an
    unknown number, is metadata that misleads about metadata.
    """
    shared = reference is not None
    bar = reference if shared else (own_peak or 0.0)
    out = {
        # `shared_basis` names which laps shared it. It exists because the
        # answer is no longer always "the laps being compared": a single
        # lap read through lap_summary is now held to the bar its session's
        # turn numbering was built against, and a payload that called that
        # "the laps being compared" would be describing a comparison the
        # reader never asked for.
        "basis": (f"shared across "
                  f"{shared_basis or 'the laps being compared'}") if shared
                 else "this lap's own cornering load",
        "lat_g_reference": round(bar, 3),
        "threshold_g": round(corner_threshold(bar), 3),
    }
    if not shared:
        return out
    if laps > 0:
        out["laps_in_reference"] = laps
    if spread_g is not None:
        out["peak_spread_g"] = spread_g
    if laps == 1:
        # Not a hypothetical: lat_g_reference only counts laps carrying
        # enough lateral load to have corners at all, so a run where the
        # rest were in-laps contributes exactly one peak. There is no
        # median to be careful about -- the bar simply IS that lap's peak,
        # and every other lap is judged against it.
        out["caution"] = (
            "only one lap carried enough cornering load to contribute, so "
            "this bar is that lap's own peak rather than a median over the "
            "run. Every other lap is being judged against it.")
    elif laps == 2:
        out["caution"] = (
            "a median over two laps is their mean, so one scrappy lap "
            "moves this bar by half its own deviation. Corner membership "
            "here is less settled than in a comparison with more laps a "
            "side.")
    return out


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
    #
    # `is not None`, not truthiness. These are float | None, and a 0.0
    # reference is a real answer -- "nothing in this run cornered" -- that
    # must not silently become "use your own peak instead". Falling back on
    # a falsy float is how one lap ends up measured against a different bar
    # from the rest without anything saying so, which is the whole failure
    # this parameter exists to end.
    peak = own_peak if reference_peak_g is None else reference_peak_g
    thresh = corner_threshold(peak)

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
        stats = _corner_stats(samples, apex, exit_, prev_exit, entry)
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


# How far an apex may wander between laps and still be the same piece of
# road. 1% of a lap, about 50m at Mugello. compare_runs has taken this as
# its corner_tolerance since it stopped bucketing, and the turn numbers use
# the same figure deliberately: "T7" and the corner comparison at that apex
# have to be the same piece of road by construction, not by coincidence.
CORNER_TOLERANCE = 0.01

# How much road two laps' corners have to share to be the same corner: half
# the shorter of the two, turning the same way. Apex distance alone cannot
# hold a long corner together -- its slowest point wanders by more than
# CORNER_TOLERANCE from lap to lap -- and it was numbering Sunset Bend four
# times. Half, not any overlap: an esse's two halves touch across laps, and
# touching is not being the same corner.
CORNER_SPAN_OVERLAP = 0.5

# How many corners on each side have to share road before two groups are
# joined. One is not enough: at Kyalami one lap's corner began early enough
# to swallow the kink before it, and that single lap was pulling a kink
# three other laps had found into the next turn.
CORNER_SPAN_SUPPORT = 2

# How many laps have to have cornered on a piece of road before it is given
# a turn number. Repeatability, not a majority: an event invents a corner on
# one lap, driving does it on every lap it happens on.
#
# A majority rule was the first attempt and it refuses a real turn. Pool a
# two-lap baseline with a two-lap candidate, as compare_runs does, and a
# corner the candidate drove on both its laps and the baseline's detector
# never found sits at exactly half -- so the one corner the comparison most
# wants to name is the one it would have left unnamed.
CORNER_MAP_MIN_LAPS_SEEN = 2

# Below this many laps even that is too strong: of two laps, a corner found
# once is half the evidence there is rather than an outlier, and refusing it
# leaves a real turn unnumbered on the lap that drove it.
CORNER_MAP_MIN_LAPS = 3


def _median_field(obs: list[dict], field: str) -> float | None:
    """The median of one corner field over a cluster's observations.

    Median rather than mean because a brake point is the field most likely
    to be missing on some laps and wildly out on one: a lift-and-coast lap
    contributes a brake_point_pos half a corner early, and the mean would
    carry a quarter of that into a number the driver reads as "where this
    turn is braked for".
    """
    vals = [c[field] for c in obs
            if isinstance(c.get(field), (int, float))]
    return round(median(vals), 4) if vals else None


def corner_map(lap_corners: list[list[dict]],
               tolerance: float = CORNER_TOLERANCE) -> dict:
    """Number the pieces of road a run corners on: T1, T2, T3...

    detect_corners numbers its output 1..n in the order it found corners on
    ONE lap, which is an index into that lap's list and not an identity. A
    light corner that falls under the bar on lap 4 shifts every number after
    it, so corner 5 on one lap and corner 5 on the next need not be the same
    piece of road -- the same failure that made compare_runs match corners
    by position instead of by index. A number that cannot be quoted twice
    cannot be quoted to a driver at all.

    Pooling every lap's corners first is what turns the number into an
    identity. The grouping is _corner_clusters, the one compare_runs already
    pairs corners with, so a turn number means the same road as a corner
    lead at the same apex.

    A cluster is numbered only where more than one lap cornered on it.
    One-lap clusters are returned under `unnumbered` rather than dropped --
    they are real observations and worth seeing -- but they must not take a
    number: a spin violent enough for the detector to carve out as its own
    corner (which is how one at Sebring was eventually spotted) would
    otherwise renumber every turn after it on the strength of one lap.
    Below CORNER_MAP_MIN_LAPS that bar is dropped too; see the constants.

    Two limits worth knowing, both shared with compare_runs and neither
    hidden by the payload. A corner the start/finish line runs through is
    two pieces of road here, because positions do not wrap. And a turn the
    detector never sees -- a kink taken flat, below the lateral-g bar -- is
    not in the map, so these numbers are the corners this run *drove*, which
    is not always the circuit's own numbering.
    """
    laps = [{"corners": cs or []} for cs in lap_corners]
    total = len(laps)
    kept, minor = [], []
    for g in _corner_clusters(laps, tolerance):
        obs = [o[2] for o in g]
        # Distinct laps, not observations. _corner_clusters caps a cluster
        # at one corner per lap, so these agree today -- counting laps says
        # what the rule below actually needs, and keeps it honest if that
        # ever changes.
        seen = len({o[1] for o in g})
        signs = [c["turn_sign"] for c in obs if c.get("turn_sign") in (1, -1)]
        entry = {
            "apex_pos": round(median(o[0] for o in g), 4),
            "entry_pos": _median_field(obs, "entry_pos"),
            "exit_pos": _median_field(obs, "exit_pos"),
            "brake_point_pos": _median_field(obs, "brake_point_pos"),
            # None on a tie rather than a coin toss: a cluster holding as
            # many left-handers as right ones has pooled two corners, and
            # saying so is more use than picking one of them.
            "turn_sign": (None if not signs or sum(signs) == 0
                          else (1 if sum(signs) > 0 else -1)),
            "laps_seen": seen,
            "laps_total": total,
        }
        if total < CORNER_MAP_MIN_LAPS or seen >= CORNER_MAP_MIN_LAPS_SEEN:
            kept.append(entry)
        else:
            minor.append(entry)

    turns = [{"turn": f"T{n}", "number": n, **e}
             for n, e in enumerate(kept, 1)]
    return {
        "turns": turns,
        "laps": total,
        "tolerance": tolerance,
        "unnumbered": minor,
        "note": "turn numbers come from the corners these laps drove, in "
                "track order from the start/finish line. They are this "
                "run's numbering, not the circuit's official one -- a kink "
                "the detector never sees is not numbered here, and a "
                "circuit that numbers one of its corners 3A is numbered "
                "straight through.",
    }


def label_corners(corners: list[dict], turns: list[dict],
                  tolerance: float = CORNER_TOLERANCE) -> int:
    """Stamp `turn` on each corner of one lap from a corner map.

    Nearest first and one-to-one, the pairing _compare_corners uses. Taking
    each corner's nearest turn independently lets two corners of the same
    lap both claim T4, and a payload naming the same turn twice is worse
    than one that says it does not know.

    `turn` is set on every corner, None where nothing matched, so a reader
    never has to tell "this corner has no number" from "this payload was
    never labelled". Returns how many were labelled.
    """
    for c in corners:
        c["turn"] = None
    pairs = sorted(
        (abs(c["apex_pos"] - t["apex_pos"]), i, j)
        for i, c in enumerate(corners)
        if isinstance(c.get("apex_pos"), (int, float))
        for j, t in enumerate(turns)
        if abs(c["apex_pos"] - t["apex_pos"]) <= tolerance)
    taken_c, taken_t, labelled = set(), set(), 0
    for _, i, j in pairs:
        if i in taken_c or j in taken_t:
            continue
        taken_c.add(i)
        taken_t.add(j)
        corners[i]["turn"] = turns[j]["turn"]
        labelled += 1
    return labelled


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


# Fewest samples an entry phase needs before its figures mean anything.
ENTRY_MIN_SAMPLES = 3

# Yaw rate beyond this is not rotation, it is a reset or a teleport: a car
# in a full spin turns at well under half of it.
YAW_RATE_SANE_MAX_DEG_S = 360.0


def _heading_steps(samples: list[dict]) -> list[tuple[float, float]]:
    """(radians turned, seconds taken) between consecutive samples.

    AC reports heading in radians wrapped to -pi..pi, so a car pointing
    along the wrap turns from +3.1 to -3.1 in one tick, which is a step of
    0.08 rad and not of 6.2. Each step is unwrapped on its own for that
    reason, rather than differencing the raw column.

    Signed as AC reports it, and which sign is left has never been checked
    here -- so everything built from this reports magnitudes, which do not
    need the convention. A step implying more than YAW_RATE_SANE_MAX_DEG_S
    is dropped rather than clamped: it is a reset, and clamping would
    report a spin that never happened.
    """
    steps = []
    for a, b in zip(samples, samples[1:]):
        vals = (a.get("heading"), b.get("heading"),
                a.get("t_ms"), b.get("t_ms"))
        if not all(isinstance(v, (int, float)) and math.isfinite(v)
                   for v in vals):
            continue
        ha, hb, ta, tb = vals
        if tb <= ta:
            continue
        d = (hb - ha + math.pi) % (2.0 * math.pi) - math.pi
        dt = (tb - ta) / 1000.0
        if abs(math.degrees(d) / dt) > YAW_RATE_SANE_MAX_DEG_S:
            continue
        steps.append((d, dt))
    return steps


def _entry_phase(samples, brake_idx, entry_idx, apex_idx) -> dict | None:
    """What the car did between the brake point and the apex.

    Everything else about a corner is measured at the apex, and the apex is
    where the car has already been rotated. Trail braking, entry rotation
    and most spins happen before it: claude_sebring_v6 moved the coast
    differential from 40% to 60% purely for entry stability at Sunset Bend,
    every metric read "within noise", and no corner produced a lead,
    because nothing was looking at the part of the corner the change was
    for.

    From the brake point where there is one; from turn-in -- the start of
    the lateral load -- on a corner taken without braking, since a sweeper
    has an entry too. `from` says which.

    - `slip_balance` is the apex figure's definition, front minus rear,
      averaged over the phase. Negative here and positive at the apex is
      the car that is loose on entry and pushes in the middle.
    - `steer_norm` is mean steering over the phase, a fraction of lock.
      A mean rather than a sum: a sum grows with how long the phase is,
      which is a property of the brake point rather than of the steering.
    - `rotation_deg` is how far the car turned by the apex, and
      `yaw_rate_peak_deg_s` how fast it did at its quickest. Both from
      heading, so both null on laps recorded before it was.
    """
    if brake_idx is not None and brake_idx < apex_idx:
        start, source = brake_idx, "brake point"
    elif entry_idx is not None and entry_idx < apex_idx:
        start, source = entry_idx, "turn-in"
    else:
        return None
    phase = samples[start:apex_idx + 1]
    if len(phase) < ENTRY_MIN_SAMPLES:
        return None

    # Slip filtered exactly as the apex figure is, and steering taken only
    # from the ticks whose slip survived: a tick emitting a wheelSlip of
    # 30000 is not one to trust for anything else either.
    balances, clean = [], []
    for s in phase:
        f = _sane_slip(s["slip_fl"], s["slip_fr"])
        r = _sane_slip(s["slip_rl"], s["slip_rr"])
        if f is not None and r is not None:
            balances.append(f - r)
            clean.append(s)
    steer = [abs(s["steer"]) for s in (clean or phase)
             if isinstance(s.get("steer"), (int, float))
             and math.isfinite(s["steer"])]

    steps = _heading_steps(phase)
    rates = [math.degrees(d) / dt for d, dt in steps]
    # One tick either side of each -- a three-sample window -- so one noisy
    # heading reading does not decide the peak. Signed before the magnitude
    # is taken, so a jitter back and forth averages out instead of adding
    # up.
    smoothed = [mean(rates[max(0, i - 1):i + 2]) for i in range(len(rates))]
    return {
        "from": source,
        "from_pos": round(phase[0]["norm_pos"], 4),
        "slip_balance": round(mean(balances), 3) if balances else None,
        "steer_norm": round(mean(steer), 2) if steer else None,
        "yaw_rate_peak_deg_s": (round(max(abs(v) for v in smoothed), 1)
                                if smoothed else None),
        "rotation_deg": (round(abs(math.degrees(sum(d for d, _ in steps))), 1)
                         if steps else None),
    }


def _corner_stats(samples, apex_idx, exit_idx, brake_floor_idx=0,
                  entry_idx=None) -> dict:
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
    # Null when there is no phase to measure: no braking and no turn-in
    # before the apex, which a caller that did not pass entry_idx gets on
    # every unbraked corner.
    out["entry_phase"] = _entry_phase(samples, brake_idx, entry_idx,
                                      apex_idx)
    if dropped:
        out["slip_samples_dropped"] = dropped
        # Magnitude, not just a count: "3 dropped" reads the same whether
        # the filter caught three 30007s or three 51s, and only one of those
        # says the ceiling is in the right place.
        out["slip_dropped_peak"] = round(worst_dropped, 1)
        out["slip_coverage_pct"] = round(100 * len(slip_pairs) / len(seg), 1)
    return out


# Rises in the damage sum closer together than this are one contact: a car
# scraping along a wall adds damage on several consecutive ticks.
CONTACT_GAP_MS = 1000


def _contacts(samples: list[dict]) -> list[dict] | None:
    """Where on the lap bodywork damage went up, one entry per contact.

    None when the lap has no damage column at all (recorded before v9), an
    empty list when it has one and it never rose. The empty list cannot say
    whether there was no contact or the server had damage switched off --
    both read zero all lap -- so nothing here claims a clean lap from it.

    Only rises count. Damage falls when the car is repaired in the pits,
    and a repair is not a negative contact.
    """
    vals = [(s.get("t_ms"), s.get("norm_pos"), s.get("damage"))
            for s in samples]
    if not any(isinstance(d, (int, float)) for _, _, d in vals):
        return None
    out, prev, last_t = [], None, None
    for t, pos, d in vals:
        if not isinstance(d, (int, float)) or not math.isfinite(d):
            continue
        if prev is not None and d > prev + 1e-6:
            if (out and last_t is not None and t is not None
                    and t - last_t <= CONTACT_GAP_MS):
                out[-1]["damage_added"] += d - prev
            else:
                out.append({"pos": round(pos or 0.0, 4),
                            "damage_added": d - prev})
            last_t = t
        prev = d
    for c in out:
        c["damage_added"] = round(c["damage_added"], 3)
    return out


def lap_summary(lap: dict, samples: list[dict],
                reference_peak_g: float | None = None,
                reference_laps: int = 0,
                reference_spread_g: float | None = None,
                turns: list[dict] | None = None,
                reference_basis: str | None = None) -> dict:
    """Everything an engineer needs to know about one lap, in ~1KB.

    reference_peak_g comes from lat_g_reference over every lap being looked
    at together. Omit it for a lap read on its own; pass it whenever two
    summaries are going to be compared, or the two corner lists are drawn
    against different bars and need not contain the same corners.

    Which bar was used comes back in `corner_detection`, because that
    difference is otherwise invisible: the same lap read here and read
    inside compare_runs can carry different corners, and a driver comparing
    the two payloads has no way to see why.

    `turns` is a corner_map's turn list, and it is what puts T-numbers on
    this lap's corners. Pass the map built from the same laps the reference
    came from: a map built against a different bar holds corners this lap
    was never judged for, and the labelling would then be matching apexes
    across two different corner lists.
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
    # Distinct samples, not the two channels' drops added together. These
    # spikes come from a reset or a teleport and hit both axes on the same
    # tick, so summing double-counted the usual case -- and a lap where
    # every sample was glitched reported 800 dropped out of 400, a figure
    # the driver is meant to read against the lap's own sample count.
    accel_dropped = sum(
        1 for s in samples
        if not _is_sane(s.get("acc_lat"), LAT_G_SANE_MAX)
        or not _is_sane(s.get("acc_lon"), LON_G_SANE_MAX))

    corners = detect_corners(samples, reference_peak_g)
    # `corner` on each of these is this lap's ordinal and moves with what
    # the detector found; `turn` is the same piece of road on every lap of
    # the run. `is not None` so an empty map still stamps turn: None on
    # every corner -- "the map has no number for this" is an answer, and it
    # should not arrive as a missing key.
    if turns is not None:
        label_corners(corners, turns)
    # own_peak only when there is no shared reference, because that is the
    # only case corner_detection_note reports it in. Computing it always
    # meant smoothing and median-filtering the lateral-g trace a second
    # time -- detect_corners has already done it -- for every lap of every
    # comparison, to fill a field that would then be thrown away.
    detection = corner_detection_note(
        reference_peak_g, reference_laps,
        own_peak=(None if reference_peak_g is not None
                  else _lat_g_peak(_lat_g_trace(samples)[0])),
        spread_g=reference_spread_g, shared_basis=reference_basis)
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

    # One call, two fields: the pair has to agree, and it costs a lazy
    # import of db each time it is asked.
    usable, not_usable_because = _usable(lap)
    contacts = _contacts(samples)

    return {
        "lap_id": lap["id"],
        "car": lap["car"],
        "track": lap["track"] + (f"/{lap['track_config']}"
                                 if lap.get("track_config") else ""),
        "lap_time": _fmt_time(lap["lap_time_ms"]),
        "lap_time_ms": lap["lap_time_ms"],
        # Two separate facts, deliberately. `ran_wide` is about track
        # limits; `usable_for_timing` is about whether the lap time means
        # anything. A lap can run wide and still be the most informative one
        # of the session, so both are reported rather than a single verdict
        # that used to decide, silently, that the lap did not exist.
        # None, not False, when the lap was never scored. A lap stored with
        # no samples has nothing to say about track limits, and "did not run
        # wide" would be a claim there is no evidence for.
        "ran_wide": (bool(lap.get("invalid"))
                     if lap.get("excursions") is not None else None),
        "track_limits": {
            "max_tyres_out": lap.get("max_tyres_out"),
            "excursions": lap.get("excursions"),
            "off_track_ms": lap.get("off_track_ms"),
            "source": lap.get("invalid_source") or "inferred",
        },
        # A bool plus a separate reason, not a bool-or-a-sentence: both
        # halves of the latter are truthy, so `if lap["usable_for_timing"]`
        # was always true and read as though it had been checked.
        "usable_for_timing": usable,
        "not_usable_because": not_usable_because,
        # Above 1, this lap's trace has been decimated to keep the database
        # within its size budget: 2 means every second sample survives. The
        # lap and its numbers are intact; the resolution is not.
        "sample_stride": lap.get("sample_stride") or 1,
        # How many telemetry samples this lap is made of. Reported so
        # accel_samples_dropped below has something to be read against: "12
        # dropped" means nothing without it, and it is the denominator for
        # judging whether a lap's peaks are trustworthy.
        "samples": total,
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
        # Counts distinct samples, so it is directly comparable to `samples`
        # above -- a spike usually hits both axes on the same tick, and
        # adding the two channels' drops made this exceed the lap's own
        # sample count.
        "accel_samples_dropped": accel_dropped or None,
        # Where damage went up, when it did. Null for a lap with no contact
        # and for one where damage was off, which read the same -- so a
        # null here is not a clean bill.
        "contacts": contacts or None,
        "avg_ride_height_f": round(mean(s["ride_f"] for s in samples), 4),
        "avg_ride_height_r": round(mean(s["ride_r"] for s in samples), 4),
        "tyres": tyres,
        "overall_slip_balance": round(mean(slip_balances), 3)
        if slip_balances else None,
        "slip_quality": slip_quality,
        "balance_note": ("positive slip_balance = front slides more "
                         "(understeer); negative = rear (oversteer)"),
        "setup": lap.get("setup_name") or None,
        "corner_detection": detection,
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


# A change in lap-time spread not worth calling a change whatever the
# statistics say: 50ms of standard deviation is below anything a driver can
# feel or a stopwatch argue about.
CONSISTENCY_FLOOR_MS = 50.0

# Relabellings enumerated exactly up to this many, drawn at random past it.
# Eight laps a side is 12870 and exact; ten a side is 184756 and not worth
# the wait for a figure the draws get to within a fraction of a percent.
PERMUTATION_EXACT_MAX = 20000
PERMUTATION_DRAWS = 20000

# _TEST["kind"] for a test with no t distribution behind it, so _report
# does not go looking for degrees of freedom.
PERMUTATION = "permutation"


def _smallest_permutation_p(n1: int, n2: int) -> float:
    """The smallest p the spread test can return with this many laps.

    Every relabelling of the laps is equally likely under no change, so no
    result can be rarer than one relabelling in all of them -- two when the
    sides are the same size, because swapping the sides gives the same
    ratio turned over.

    Past PERMUTATION_EXACT_MAX the relabellings are drawn rather than
    enumerated, and a drawn estimate is (hits + 1) / (draws + 1): never
    below one in the draws, however lopsided the laps. That floor sits
    above the theoretical one, and it is the one this code can actually
    return, so the larger of the two is what is reported. Quoting the
    theoretical minimum there would promise a p the estimate cannot reach.
    """
    total = math.comb(n1 + n2, n1)
    theoretical = (2.0 if n1 == n2 else 1.0) / total
    if total > PERMUTATION_EXACT_MAX:
        return max(theoretical, 1.0 / (PERMUTATION_DRAWS + 1))
    return theoretical


def _permutation_spread_p(base: list[float], cand: list[float]) -> float:
    """Two-sided p for a change in spread, by relabelling the laps.

    The statistic is |log| of the ratio of the two variances, so tighter
    and looser are judged alike. Under no change every split of these laps
    into two runs of these sizes was as likely as the one that happened; p
    is the share of splits at least as lopsided. Exact whatever shape the
    lap times have, which is the point -- see _measure_consistency.
    """
    n1, n = len(base), len(base) + len(cand)
    # Centred first: lap times are ~1e5 ms and their squares ~1e10, and the
    # variances are differences of those.
    centre = (sum(base) + sum(cand)) / n
    xs = [v - centre for v in list(base) + list(cand)]
    tot, tot_sq = sum(xs), sum(v * v for v in xs)
    nb = n - n1

    def stat(idx) -> float:
        sa = sum(xs[i] for i in idx)
        qa = sum(xs[i] * xs[i] for i in idx)
        va = (qa - sa * sa / n1) / (n1 - 1)
        sb, qb = tot - sa, tot_sq - qa
        vb = (qb - sb * sb / nb) / (nb - 1)
        if va <= 0 or vb <= 0:
            return 0.0 if va <= 0 and vb <= 0 else math.inf
        return abs(math.log(va / vb))

    observed = stat(range(n1))
    # Ties are "at least as lopsided", and two splits that tie exactly can
    # differ in the last bit after this arithmetic.
    edge = (observed - 1e-9 * max(1.0, observed)
            if math.isfinite(observed) else observed)
    total = math.comb(n, n1)
    if total <= PERMUTATION_EXACT_MAX:
        hits = sum(1 for idx in itertools.combinations(range(n), n1)
                   if stat(idx) >= edge)
        return hits / total
    # Seeded, so the same laps always give the same answer.
    rng = random.Random(0)
    hits = sum(1 for _ in range(PERMUTATION_DRAWS)
               if stat(rng.sample(range(n), n1)) >= edge)
    return (hits + 1) / (PERMUTATION_DRAWS + 1)


def _measure_consistency(base: list[float], cand: list[float],
                         family_size: int) -> dict:
    """Did the laps get more or less repeatable, as opposed to faster?

    Every other metric compares means. A change that makes the driver more
    consistent can leave the mean where it was while the spread collapses,
    and a comparison of means is blind to that by construction.

    The test is a permutation test on the ratio of the variances, not the
    variance-ratio F test. The F test assumes normal lap times, and a run
    in which a spin now and then costs five seconds is nothing like
    normal. Simulated with a 0.3s spread and a 15% chance of a 3-8s spin
    on any lap, identical on both sides: at six laps a side the F test
    called the spread changed in 47% of runs at 95%, and 44% even at the
    corrected 0.05/9. Relabelling the laps assumes nothing about their
    shape and held 0.3% at 0.05/9 on the same runs.

    What that costs is laps. The smallest p a relabelling can produce is
    set by how many ways there are to relabel, so with few laps no result
    can clear the bar however lopsided it is. Such a test is left out of
    the family entirely rather than counted in it -- a test that cannot
    reject cannot reject falsely, and counting it would only raise every
    other metric's threshold for nothing -- and the entry says how many
    laps would make it testable. Whether it is in is decided by the lap
    counts alone, never by the result.

    And the honest reading of the case that motivated it. Sebring v9: six
    laps inside 0.97s after a run where two of six ended in a spin. Taken
    as that shape, p = 0.41. If one lap in three spins by chance, a run of
    six has no spin 9% of the time, so six laps a side cannot tell "the
    setup stopped the spins" from "no spin happened this time" -- and a
    test that says otherwise is the F test above, wrong four times in ten.
    """
    n1, _, s1 = _stats(base)
    n2, _, s2 = _stats(cand)
    out = {"label": "lap-time consistency", "baseline_n": n1,
           "candidate_n": n2, "units": "ms"}
    if n1 == 0 or n2 == 0:
        return {**out, "verdict": "not measured"}
    if n1 < 2 or n2 < 2:
        return {**out, "verdict": "need at least 2 laps a side to see noise"}
    direction = ("tighter" if s2 < s1 else "looser" if s2 > s1
                 else "unchanged")
    out.update({
        "label": f"lap-time consistency ({direction})",
        "baseline_sd": round(s1, 1),
        "candidate_sd": round(s2, 1),
        "change_sd": round(s2 - s1, 1),
        "direction": direction,
        "note": "standard deviation of lap time on each side, judged by "
                "relabelling the laps rather than by an F test -- a spin "
                "now and then makes lap times far from normal, and an F "
                "test on them calls a change in spread in over 40% of runs "
                "where nothing changed. The price is that few laps cannot "
                "confirm anything: smallest_possible_p says how far these "
                "laps could have gone.",
    })
    smallest = _smallest_permutation_p(n1, n2)
    strictest = FAMILY_ALPHA / family_size
    if smallest > strictest:
        # More laps cannot take p below the drawn estimate's own floor, so a
        # threshold stricter than that has no lap count that meets it -- and
        # searching for one would never end.
        need = None
        if strictest >= 1.0 / (PERMUTATION_DRAWS + 1):
            need = 2
            while _smallest_permutation_p(need, need) > strictest:
                need += 1
        return {**out, "verdict": "too few laps to test",
                "smallest_possible_p": _sig(smallest),
                "laps_needed_a_side": need,
                "not_in_family": f"with {n1} and {n2} laps no result could "
                                 f"clear 0.05/{family_size}, so this was "
                                 f"not counted among the metrics judged "
                                 f"together and cost them nothing"}
    out[_TEST] = {"kind": PERMUTATION, "p": _permutation_spread_p(base, cand),
                  "diff": s2 - s1, "floor": CONSISTENCY_FLOOR_MS,
                  "min_p": smallest}
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
    a lead, and over 2500 null runs at fifteen corners -- two channels a
    corner, thirty tests -- 77.6% of payloads carried at least one. The
    entry channels make it up to five a corner and the rate climbs with
    the count, which is why the payload's note computes it for the run it
    describes instead of quoting this. A lead standing alone under eight
    metrics that all read "within noise" is most likely one of those, which
    is why the payload says so in the same breath as the lead.

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
    if t.get("kind") == PERMUTATION:
        # No resolution and no power: both are t-distribution figures, and
        # this test has no t distribution. What it does have is a floor on
        # p set by the lap count, which says the same kind of thing.
        entry["p_value"] = _sig(t["p"])
        entry["p_value_adjusted"] = _sig(t["p_adj"])
        entry["smallest_possible_p"] = _sig(t["min_p"])
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
                 corner_tolerance: float = CORNER_TOLERANCE) -> dict:
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

    `lap_time_consistency` asks the other question -- did the laps get more
    or less repeatable -- and joins the family only once there are enough
    laps for it to be able to answer; see _measure_consistency.
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

    # After the means, because whether it can join the family depends on
    # how big the family already is.
    metrics["lap_time_consistency"] = _measure_consistency(
        [v for v in (_dig(l, ("lap_time_ms",)) for l in baseline)
         if v is not None],
        [v for v in (_dig(l, ("lap_time_ms",)) for l in candidate)
         if v is not None],
        sum(1 for e in metrics.values() if _TEST in e) + 1)

    corners, unmatched, compared = _compare_corners(
        baseline, candidate, corner_tolerance)

    # One map over both sides, for the same reason there is one lateral-g
    # bar over both: numbered per side, the piece of road that is T7 in the
    # baseline could be T6 in the candidate, and a payload that names the
    # same corner two different things inside itself is worse than one that
    # only gives apex positions. The unmatched list is labelled too -- a
    # corner found on one side only is exactly the one a reader wants to
    # name, and it is the reason the two sides can disagree about numbering
    # in the first place.
    turn_map = corner_map(
        [l.get("corners") or [] for l in baseline + candidate],
        corner_tolerance)
    label_corners(corners, turn_map["turns"], corner_tolerance)
    label_corners(unmatched, turn_map["turns"], corner_tolerance)

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
        # The turn number where there is one, the apex position where there
        # is not. A lead the map has no number for is still a lead, and
        # dropping it from this sentence to keep the format tidy would hide
        # exactly the corners the detector is least sure about.
        where = ", ".join(
            c.get("turn") or f"{c['apex_pos']:.3f}" for c in
            [c for c in ranked
             if any(t.get("lead") == "worth a look"
                    for t in c["tests"])][:4])
        head += (f". Separately, {flagged} corner(s) stand out as "
                 f"exploratory leads ({where}) -- uncorrected, not "
                 f"findings, and about 5% of quiet corners do this")
    summary = head

    # The chance a run with nothing changed carries at least one lead,
    # counting this run's corner tests as independent. They are not quite
    # -- the channels of one corner move together -- so it runs a little
    # high: over 2500 null runs of thirty tests the measured rate was
    # 77.6% against 78.5% counted this way. Computed rather than quoted,
    # because the entry channels took a corner from two tests to five and
    # a fixed figure for thirty was understating it by the time they did.
    null_lead_pct = round(100 * (1 - (1 - FAMILY_ALPHA) ** len(leads)))
    leads_note = (
        f"EXPLORATORY, not findings. {compared} corner(s) were compared on "
        f"up to {len(CORNER_CHANNELS)} channels each, {len(leads)} tests in "
        f"all; the {len(shown)} with the largest effect "
        f"size are listed, largest first, and {flagged} of all those "
        f"compared cleared an uncorrected 95%. These p-values are NOT "
        f"corrected for how many corners were looked at, so roughly 5% of "
        f"unchanged corner tests come back 'worth a look', and with "
        f"{len(leads)} of them about {null_lead_pct}% of runs with nothing "
        f"changed at all would carry at least one. A lead says where to "
        f"look when a metric moved; standing "
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
            {"turn": c.get("turn"), "apex_pos": round(c["apex_pos"], 4),
             **{t["channel"]: {k: v for k, v in t.items()
                               if k not in ("channel", _TEST)}
                for t in c["tests"]}}
            for c in shown],
        "corner_leads_note": leads_note,
        "corners_in_one_run_only": unmatched[:12],
        # Where each label is, so a reader can place T7 without holding a
        # second payload beside this one. Apex only: the full map -- brake
        # points, extent, how many laps each turn was seen on -- is what
        # the track_corners tool is for, and repeating it here would cost
        # more than the labels are worth.
        "turns": [{"turn": t["turn"], "apex_pos": t["apex_pos"]}
                  for t in turn_map["turns"]],
        "turns_note": turn_map["note"],
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
# corner-level sibling of the floors in RUN_METRICS. (name, where in the
# corner dict, floor). The entry channels are what a change aimed at corner
# entry moves, and without them such a change had nowhere to show up.
CORNER_CHANNELS = [
    ("slip_balance", ("slip_balance",), 0.05),
    ("min_speed_kmh", ("min_speed_kmh",), 0.5),
    ("entry_slip_balance", ("entry_phase", "slip_balance"), 0.05),
    ("entry_yaw_rate_deg_s", ("entry_phase", "yaw_rate_peak_deg_s"), 2.0),
    ("entry_steer_norm", ("entry_phase", "steer_norm"), 0.02),
]


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

    Proximity alone split long corners. On a long constant-radius corner
    the slowest point wanders by more than `tolerance` between laps, and
    wherever the lateral g sags under the bar mid-corner the detector
    reports two pieces on that lap. Sunset Bend at Sebring came back as four
    turns seen on 2, 4, 2 and 2 laps of 8, and Interlagos, Suzuka and
    Kyalami did the same. So two groups are joined when their corners
    cover the same road, turning the same way (CORNER_SPAN_OVERLAP), on
    enough laps on both sides (CORNER_SPAN_SUPPORT).
    A joined group with duplicates is then read by what most laps did: if
    most drove it as one corner, it is one, and a lap the detector split
    keeps its slowest piece, where its apex really was. If most drove it as
    two, the widest-gap split below separates them as before -- which is
    what keeps a pair one lap ran together as two corners.

    Proximity only chains corners turning the same way. An esse's two
    halves can have apexes closer than `tolerance` -- Senna at Interlagos
    does, on every lap -- and chained together they were split wherever the
    widest gap happened to fall, which is not where the direction changes.
    Corners with no recorded direction chain with anything, as before.
    """
    obs = []
    for i, lap in enumerate(laps):
        for c in lap.get("corners") or []:
            pos = c.get("apex_pos")
            if isinstance(pos, (int, float)):
                obs.append((float(pos), i, c))
    obs.sort(key=lambda o: o[0])

    parent = list(range(len(obs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Chain each corner to the nearest earlier one it could be, within
    # tolerance -- the same chaining as before, skipping over corners that
    # turn the other way.
    for i in range(1, len(obs)):
        j = i - 1
        while j >= 0 and obs[i][0] - obs[j][0] <= tolerance:
            if _same_way(obs[i][2], obs[j][2]):
                parent[find(i)] = find(j)
                break
            j -= 1

    # Then join whole groups that share road, counting the LAPS on each side
    # that do. Laps, not corners: a lap that split one corner into two has
    # two corners in the group, and counting corners let that one lap meet a
    # rule written as "two laps on each side" by itself -- which is how a
    # one-lap artefact could pull a real turn into its neighbour. The count
    # is per side because a split piece seen on two laps is still two
    # pieces, however many whole laps it sits inside.
    #
    # Support is counted against the groups proximity left, and is not
    # recounted as those groups merge. Three pieces supported by one lap
    # each against the other two therefore stay three turns, even though
    # any two of them merged would clear the bar against the third. That is
    # the safe direction to be wrong in -- a turn split in two is visible
    # on the map and in the payload, where two turns silently welded into
    # one are not -- but it is a limit, not an accident.
    # Each corner's stretch and direction once, not once per pair: this loop
    # is quadratic in observations, and re-reading four dict keys through
    # four isinstance checks inside it was most of what it cost.
    roads = [_road(o[2]) for o in obs]

    bridges: dict[tuple, tuple[set, set]] = {}
    for i, a in enumerate(obs):
        if roads[i] is None:
            continue
        for j in range(i + 1, len(obs)):
            b = obs[j]
            if a[1] == b[1] or not _same_road(roads[i], roads[j]):
                continue
            ga, gb = find(i), find(j)
            if ga == gb:
                continue
            key, (x, y) = (((ga, gb), (a[1], b[1])) if ga < gb
                           else ((gb, ga), (b[1], a[1])))
            sides = bridges.setdefault(key, (set(), set()))
            sides[0].add(x)
            sides[1].add(y)
    for (ga, gb), (xs, ys) in bridges.items():
        if len(xs) >= CORNER_SPAN_SUPPORT and len(ys) >= CORNER_SPAN_SUPPORT:
            ra, rb = find(ga), find(gb)
            if ra != rb:
                parent[rb] = ra

    comps: dict[int, list] = {}
    for i, o in enumerate(obs):
        comps.setdefault(find(i), []).append(o)

    out, queue = [], []
    for g in comps.values():
        per_lap: dict[int, list] = {}
        for o in g:
            per_lap.setdefault(o[1], []).append(o)
        counts = sorted(len(v) for v in per_lap.values())
        if counts[-1] > 1 and counts[(len(counts) - 1) // 2] == 1:
            g = [min(v, key=_apex_piece) for v in per_lap.values()]
        queue.append(sorted(g, key=lambda o: o[0]))

    while queue:
        g = queue.pop()
        seen = [o[1] for o in g]
        if len(g) < 2 or len(set(seen)) == len(seen):
            out.append(g)
            continue
        _, k = max((g[i + 1][0] - g[i][0], i) for i in range(len(g) - 1))
        queue += [g[:k + 1], g[k + 1:]]
    return sorted(out, key=lambda g: g[0][0])


def _span(c: dict) -> tuple[float, float] | None:
    e, x = c.get("entry_pos"), c.get("exit_pos")
    if isinstance(e, (int, float)) and isinstance(x, (int, float)) and x > e:
        return float(e), float(x)
    return None


def _road(c: dict) -> tuple[tuple[float, float], object] | None:
    """A corner's stretch and the way it turns, or None if it has no span.

    A corner known only by its apex can still be grouped by proximity, but
    nothing here can say what road it covered, so it never bridges.
    """
    s = _span(c)
    return None if s is None else (s, c.get("turn_sign"))


def _same_road(ra, rb) -> bool:
    """Whether two _road() values cover the same stretch, turning the same way.

    Takes the pair already read rather than the corners, because the caller
    compares every observation with every other and would otherwise read
    each corner's span once per comparison instead of once.
    """
    if ra is None or rb is None or ra[1] != rb[1]:
        return False
    sa, sb = ra[0], rb[0]
    shared = min(sa[1], sb[1]) - max(sa[0], sb[0])
    return shared >= CORNER_SPAN_OVERLAP * min(sa[1] - sa[0], sb[1] - sb[0])


def _same_way(a: dict, b: dict) -> bool:
    """Whether two corners could turn the same way; unknown matches anything."""
    sa, sb = a.get("turn_sign"), b.get("turn_sign")
    return sa is None or sb is None or sa == sb


def _apex_piece(o: tuple) -> tuple:
    """Sort key for the piece of a split corner that holds its apex.

    The slowest piece, since the apex is the slowest point; the longest
    where speed was not recorded.
    """
    c = o[2]
    speed = c.get("min_speed_kmh")
    span = _span(c)
    return (speed if isinstance(speed, (int, float)) else math.inf,
            -(span[1] - span[0]) if span else 0.0)


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
        for field, path, floor in CORNER_CHANNELS:
            b = [v for v in (_dig(o[2], path) for o in b_groups[i])
                 if v is not None]
            c = [v for v in (_dig(o[2], path) for o in c_groups[j])
                 if v is not None]
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

# How much a slice's front ride height must move before it is worth calling
# a rough section. Below this the ranking is just sorting noise, and a
# glass-smooth lap was still reporting its six "roughest" places at 0.0mm --
# which reads as a finding rather than as the absence of one.
ROUGH_MIN_RANGE_MM = 1.0


def _has_position(samples: list[dict]) -> bool:
    """Whether any sample carries a usable world position.

    Both axes the line is drawn from, because a row with one and not the
    other is not a position -- see the note in _binned_line.
    """
    return any(s.get("pos_x") is not None and s.get("pos_z") is not None
               for s in samples)


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
        # Both axes the line is drawn from -- x and z. pos_y is the height
        # and nothing here reads it, so it is deliberately not required: a
        # row missing only the vertical still describes a line on the map.
        #
        # Checking pos_x alone was the bug. The three are written together
        # by the collector, so a row with one and not the others cannot come
        # from a live session -- but store_lap accepts short tuples from
        # earlier layouts, and one truncated a field past tyres_out produces
        # exactly this. It reached mean(s["pos_z"]) and raised a TypeError
        # from inside statistics, naming nothing the caller could act on.
        placed = [s for s in group
                  if s.get("pos_x") is not None and s.get("pos_z") is not None]
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
    if not _has_position(samples):
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
        elif not _has_position(other_samples):
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
    # A floor, not just a ranking. Sorting descending always yields six
    # entries, so a glass-smooth track reported six roughest sections at
    # 0.0mm -- a list that looks like a finding and is the absence of one.
    rough = [p for p in measured
             if p.get("ride_f_range_mm", 0.0) >= ROUGH_MIN_RANGE_MM]
    if rough:
        rough.sort(key=lambda p: -p["ride_f_range_mm"])
        out["roughest_sections"] = [
            {"pos": p["pos"], "ride_f_range_mm": p["ride_f_range_mm"],
             "speed_kmh": p["speed_kmh"]}
            for p in rough[:6]]
    elif any("ride_f_range_mm" in p for p in measured):
        out["roughest_sections"] = []
        out["surface_note"] = (
            f"no slice moved the front ride height by "
            f"{ROUGH_MIN_RANGE_MM}mm or more; nothing here reads as rough")

    out["slices_measured"] = len(measured)
    out["slices_empty"] = points - len(measured)
    out["line"] = line
    return out


# --- tyre wear ----------------------------------------------------------
#
# AC counts tyre wear DOWN from 100. Every reading here is converted to
# "used", counting up from zero, because a driver asking whether the tyres
# went off wants a number that grows as they do.

WEAR_CORNERS = ("fl", "fr", "rl", "rr")


def _wear_at(sample: dict) -> dict | None:
    """The four wear readings from one sample, or None if it carries none."""
    out = {}
    for w in WEAR_CORNERS:
        v = sample.get(f"wear_{w}")
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            return None
        out[w] = float(v)
    return out


# Remaining wear rising by more than this between one lap's end and the
# next lap's start means a fresh set went on. Tyres do not un-wear, so in
# principle any rise is a reset -- the margin is only there so a hundredth
# of a percent of jitter does not cut a stint in half. It is generously
# above any noise seen and far below a real change, which puts a worn set
# back to 100 and moves several percent at once.
WEAR_RESET_PCT = 0.5


def split_stints(laps: list[dict],
                 first_reason: str = "first lap of the session") -> dict:
    """Cut a session's laps into stints at the places the tyres changed.

    A session is not a stint. Sessions deliberately retain out-laps and pit
    laps, and one can span a tyre change, a setup change, or both. Running
    the whole thing through `stint_wear` takes the first set's starting
    wear and the last set's ending wear, subtracts them, and reports the
    difference as if one set of tyres had done all of it -- and then puts
    the two sets into a single early-against-late trend, which is where the
    "did they go off" answer comes from. Both numbers can come out
    confidently wrong.

    `laps` are entries as `stint_wear` takes them, oldest first. Returns
    {"stints": [{"laps": [...], "started_because": <reason>}, ...],
    "boundary_laps": [...]}.

    `first_reason` is what the first stint says about why it started. It
    is a parameter because only the caller knows whether it was handed the
    whole session or a slice of one -- saying "first lap of the session"
    over a windowed or explicitly ranged set of laps is a claim about data
    this function never saw.

    Four things end a stint:

    - an **out-lap**, which by definition begins a new run out of the pits;
    - a **pit lap**, whose wear delta may straddle a tyre change made
      halfway through it and so belongs to neither side. It is excluded
      from the stints and reported separately rather than silently dropped;
    - **wear rising within one lap** -- the set changed part-way through a
      lap that carries no pit flag. Its own delta is negative and would
      subtract from the stint total, so it is held out as a boundary too;
    - **remaining wear rising between laps**, which only happens when a
      fresh set went on. This catches a change the flags missed, and does
      not depend on them being right, which matters because `pitted` is
      *inferred* for laps migrated from before schema v10.
    """
    stints: list[dict] = []
    boundaries: list[dict] = []
    current: list[dict] = []
    reason = first_reason
    prev_end = None

    def flush():
        nonlocal current
        if current:
            stints.append({"laps": current, "started_because": reason})
            current = []

    for entry in laps:
        lap = entry["lap"]
        if lap.get("pitted"):
            flush()
            boundaries.append({
                "lap_id": lap["id"],
                "lap_number": lap.get("lap_number"),
                "excluded_because": "pit visit during this lap, so its wear "
                                    "delta may straddle a tyre change",
            })
            reason = "first lap after a pit visit"
            prev_end = None
            continue

        start = _wear_at(entry.get("first")) if entry.get("first") else None
        end = _wear_at(entry.get("last")) if entry.get("last") else None

        # Wear went UP inside this lap: a set changed part-way through one
        # that carries no pit flag. Its delta is negative, so leaving it in
        # would subtract from the stint's total and flatten its trend.
        if (start is not None and end is not None
                and any(end[w] - start[w] > WEAR_RESET_PCT
                        for w in WEAR_CORNERS)):
            flush()
            boundaries.append({
                "lap_id": lap["id"],
                "lap_number": lap.get("lap_number"),
                "excluded_because": "tyre wear rose during this lap, so a "
                                    "fresh set went on part-way through it "
                                    "and its own delta spans both sets",
            })
            reason = "first lap after a tyre change made mid-lap"
            prev_end = None
            continue

        if lap.get("out_lap"):
            flush()
            reason = "out-lap: this run began here"
            prev_end = None
        elif (start is not None and prev_end is not None
                and any(start[w] - prev_end[w] > WEAR_RESET_PCT
                        for w in WEAR_CORNERS)):
            flush()
            reason = ("tyre wear went back up, so a fresh set went on "
                      "before this lap")

        current.append(entry)
        # Unconditionally, including when this lap has no end reading. A
        # lap whose end wear is missing clears the baseline rather than
        # leaving the previous lap's behind: keeping a stale one made the
        # NEXT lap look like a jump back up, so a set changed on the very
        # lap that lost its reading came out as two one-lap stints instead
        # of one. Losing the baseline only costs a missed boundary, which
        # is the safe direction; a stale one invents boundaries.
        prev_end = end

    flush()
    return {"stints": stints, "boundary_laps": boundaries}


def stint_wear(laps: list[dict]) -> dict:
    """How much tyre each lap of a stint used, and whether it is accelerating.

    Each entry in `laps` is {"lap": <lap row>, "first": <sample>,
    "last": <sample>} -- only the two end samples are needed, so a caller
    should not read a whole trace per lap to build this.

    `laps` must be ONE stint. This function has no way to know it was
    handed two sets of tyres, and if it is, the totals span the change and
    the trend compares one set against the other. Callers holding a whole
    session run it through `split_stints` first.

    The question this exists for is "did the tyres go off", and the honest
    answer needs the wear itself rather than its shadow. Until now that was
    argued from hot pressure and core temperature, which are affected by
    wear and by half a dozen other things -- so a flat pressure trace was
    being read as evidence of no degradation when it is only evidence of
    stable pressure.

    AC counts wear down from 100. Reported as `used`, counting up, so more
    always means worse.
    """
    rows = []
    # The wear the stint STARTED on, which is not the same as the first
    # measured lap's remaining_pct -- that one is taken at the end of the
    # lap, so reporting it as "at start" quietly hid the first lap's wear
    # and claimed the driver began on a tyre already used.
    first_start = None
    for entry in laps:
        lap = entry["lap"]
        first, last = entry.get("first"), entry.get("last")
        start = _wear_at(first) if first else None
        end = _wear_at(last) if last else None
        if start is not None and first_start is None:
            first_start = start
        if start is None or end is None:
            rows.append({
                "lap_id": lap["id"],
                "lap_number": lap.get("lap_number"),
                "has_wear": False,
            })
            continue
        rows.append({
            "lap_id": lap["id"],
            "lap_number": lap.get("lap_number"),
            "lap_time": _fmt_time(lap["lap_time_ms"]),
            "has_wear": True,
            # An out-lap covers part of a lap's distance, so its wear is not
            # comparable with a flying lap's. It counts towards the total --
            # the rubber came off -- but not towards any per-lap RATE.
            "out_lap": bool(lap.get("out_lap")),
            # Remaining, as the game shows it, and used, counting up.
            "remaining_pct": {w: round(end[w], 2) for w in WEAR_CORNERS},
            "used_this_lap_pct": {
                w: round(start[w] - end[w], 3) for w in WEAR_CORNERS},
        })

    measured = [r for r in rows if r["has_wear"]]
    out = {"laps": rows, "laps_with_wear": len(measured),
           "laps_without_wear": len(rows) - len(measured)}
    if not measured:
        out["error"] = (
            "no lap in this set carries tyre wear. Wear recording arrived "
            "in schema v9; laps from before it cannot be backfilled, "
            "because the readings were never captured.")
        return out

    # Rates come from full laps only. An out-lap's shorter distance uses
    # less rubber, and averaging it in with flying laps drags the early
    # rate down -- which reads as a rate RISING later in the stint, the
    # exact signal this report is asked to detect. The bias only ever
    # points one way, because an out-lap is always at the start.
    full = [r for r in measured if not r["out_lap"]]
    out_laps = len(measured) - len(full)

    last_seen = measured[-1]
    total = {w: round(sum(r["used_this_lap_pct"][w] for r in measured), 2)
             for w in WEAR_CORNERS}
    worst = max(WEAR_CORNERS, key=lambda w: total[w])

    out["summary"] = {
        "laps_measured": len(measured),
        "full_laps_measured": len(full),
        "out_laps_excluded_from_rates": out_laps,
        "remaining_at_start_pct": {
            w: round(first_start[w], 2) for w in WEAR_CORNERS},
        "remaining_at_end_pct": last_seen["remaining_pct"],
        "used_total_pct": total,
        "used_per_lap_pct": (
            {w: round(sum(r["used_this_lap_pct"][w] for r in full)
                      / len(full), 3) for w in WEAR_CORNERS}
            if full else None),
        "worst_corner": worst,
        "note": "AC counts wear down from 100; `used` counts up, so higher "
                "always means more worn. These are the game's own wear "
                "figures, not an inference from pressure or temperature. "
                "`used_total_pct` covers every measured lap; the rate and "
                "the trend use full laps only, because an out-lap covers "
                "less distance and would drag the early rate down.",
    }
    if not full:
        out["summary"]["rate_note"] = (
            "every measured lap in this stint was an out-lap, so there is "
            "no full lap to state a rate from")

    # Is it getting worse, or is it linear? A tyre that is degrading
    # non-linearly uses more in the second half than the first, and that is
    # the shape a driver means by "they went off" -- as distinct from wear
    # that simply accumulates, which every tyre does and which costs
    # nothing on its own.
    if len(full) >= 4:
        half = len(full) // 2
        early = full[:half]
        late = full[-half:]
        trend = {}
        for w in WEAR_CORNERS:
            a = mean(r["used_this_lap_pct"][w] for r in early)
            b = mean(r["used_this_lap_pct"][w] for r in late)
            trend[w] = {"early_per_lap": round(a, 3),
                        "late_per_lap": round(b, 3),
                        "change": round(b - a, 3)}
        out["trend"] = trend
        out["trend_note"] = (
            f"first half of the stint against the last half, over "
            f"{len(full)} full laps. Wear rising late is degradation "
            f"accelerating; a flat rate is a tyre wearing normally, which "
            f"is not the same thing as one going off.")
    return out


# --- braking, and what the aid fields are not ---------------------------
#
# This started life as an ABS activity report, on the assumption that
# shared memory's `abs` and `tc` were the amount of intervention happening
# right now. The first real lap falsified it: both were constant to three
# decimal places across 3024 samples -- down every straight and through
# every braking zone alike. Nothing that measures moment-to-moment
# intervention behaves that way, so they are a setting or a threshold, and
# reporting them as activity was inventing a measurement.
#
# CSP does expose the real thing -- `absInAction` and
# `tractionControlInAction`, both booleans on ac.CarState -- but they are
# marked physics-only, which means a physics worker, which CSP forbids
# online. Same constraint the damper histograms already live under. Until
# that is wired up, ABS activity is unmeasured and this module says so.
#
# What IS measurable from data already stored is the thing the driver
# actually described: wheels locking under braking. A tyre at the limit
# under straight-line braking shows up as slip, and slip is recorded at
# 25 Hz on all four corners.

# Pedal travel that counts as leaning on the brakes. Below this the driver
# is trailing off, and what the tyres do there says nothing about whether
# ABS is set right.
HARD_BRAKE = 0.5
# Steering below this is straight enough that slip is longitudinal --
# i.e. the wheel is locking rather than cornering. Without this the report
# would flag every trail-braked corner entry as a lockup, because slip
# under combined braking and cornering is high by construction.
STRAIGHT_STEER = 0.15
# Front slip above this, while braking hard in a straight line, reads as a
# wheel getting away. PROVISIONAL: it has never been calibrated against a
# lockup someone confirmed from the cockpit, so it is reported alongside
# the raw distribution rather than instead of it, and the payload says so.
LOCKUP_SLIP = 3.0

AID_ACTIVE_EPS = 0.001
def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile. No interpolation, no numpy."""
    ordered = sorted(values)
    i = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[i]


def _aid_field_note(samples: list[dict], field: str) -> dict | None:
    """What a driver-aid field held, and whether it varied at all.

    Reported as an observation rather than as a measurement of anything.
    If `varies` is false the field cannot be describing intervention, and
    the payload should not let a reader believe otherwise.
    """
    vals = [s.get(field) for s in samples]
    vals = [v for v in vals
            if isinstance(v, (int, float)) and math.isfinite(v)]
    if not vals:
        return None
    lo, hi = min(vals), max(vals)
    return {
        "samples": len(vals),
        "min": round(lo, 4),
        "max": round(hi, 4),
        "varies": (hi - lo) > AID_ACTIVE_EPS,
    }


def braking_report(lap: dict, samples: list[dict],
                   points: int = 20) -> dict:
    """What the tyres did under braking, and whether anything locked.

    The question behind this is "is ABS aggressive enough", which cannot be
    answered from the setup value and -- for now -- cannot be answered from
    shared memory either, because the fields that look like ABS activity
    are constant across a lap. See the notes above LOCKUP_SLIP.

    What can be answered: under hard braking in a straight line, how much
    slip did each axle carry, and did the front ever run away. Slip while
    braking AND cornering is high by construction, so the steering filter
    is what makes the rest of the numbers mean anything.
    """
    if not samples:
        return {"error": "no samples for this lap"}

    def num(s, k):
        v = s.get(k)
        return v if isinstance(v, (int, float)) and math.isfinite(v) else None

    def _hard_straight(s) -> bool:
        """Hard on the brakes with the wheel near straight.

        One definition, used both for the axle statistics and for walking
        the lap looking for lockup runs, so the two cannot drift apart.
        """
        return ((num(s, "brake") or 0.0) >= HARD_BRAKE
                and abs(num(s, "steer") or 0.0) < STRAIGHT_STEER)

    braking = [s for s in samples
               if (num(s, "brake") or 0.0) >= HARD_BRAKE]
    straight = [s for s in braking if _hard_straight(s)]

    out = {
        "lap_id": lap["id"],
        "lap_time": _fmt_time(lap["lap_time_ms"]),
        "hard_braking_samples": len(braking),
        "straight_line_braking_samples": len(straight),
        "thresholds": {
            "hard_brake": HARD_BRAKE,
            "straight_steer": STRAIGHT_STEER,
            "lockup_slip": LOCKUP_SLIP,
            "lockup_slip_is_calibrated": False,
        },
    }

    if not straight:
        out["error"] = (
            f"no sample on this lap had brake >= {HARD_BRAKE} with steering "
            f"under {STRAIGHT_STEER}. Everything here needs straight-line "
            f"braking to separate a locking wheel from a cornering one.")
        return out

    axles = {}
    for axle, corners in (("front", ("fl", "fr")), ("rear", ("rl", "rr"))):
        vals = []
        for s in straight:
            got = [num(s, f"slip_{c}") for c in corners]
            got = [v for v in got if v is not None and abs(v) <= SLIP_SANE_MAX]
            if got:
                vals.append(max(got))          # the worst wheel on the axle
        if not vals:
            continue
        axles[axle] = {
            "mean_slip": round(mean(vals), 3),
            "p95_slip": round(_pct(vals, 0.95), 3),
            "max_slip": round(max(vals), 3),
            "samples_over_lockup_threshold": sum(
                1 for v in vals if v > LOCKUP_SLIP),
        }
    out["under_straight_line_braking"] = axles

    if "front" in axles and "rear" in axles:
        f, r = axles["front"]["mean_slip"], axles["rear"]["mean_slip"]
        out["axle_closer_to_locking"] = "front" if f > r else "rear"
        out["axle_note"] = (
            "the axle carrying more slip under straight-line braking is the "
            "one nearer its limit, which is what brake bias moves")

    # Lockup runs: consecutive samples *in the recorded order* that were
    # over the threshold under hard straight-line braking.
    #
    # This walks the whole lap rather than the filtered `straight` list, and
    # that is the entire point. `straight` has already dropped every
    # coasting, cornering and light-brake sample, so two one-tick spikes in
    # braking zones half a lap apart sit next to each other in it and would
    # be reported as a single two-sample run -- a lockup invented out of two
    # pieces of noise. Anything that is not hard, straight braking over the
    # threshold ends the run, including the samples that were filtered out.
    runs, current = [], []
    for s in samples:
        worst = None
        if _hard_straight(s):
            got = [num(s, f"slip_{c}") for c in ("fl", "fr")]
            got = [v for v in got if v is not None and abs(v) <= SLIP_SANE_MAX]
            worst = max(got) if got else None
        if worst is not None and worst > LOCKUP_SLIP:
            current.append(s)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    out["front_lockup_runs"] = [
        {"from_pos": round(r[0].get("norm_pos", 0.0), 4),
         "to_pos": round(r[-1].get("norm_pos", 0.0), 4),
         "samples": len(r)}
        for r in runs if len(r) >= 2][:8]
    out["front_lockup_run_count"] = len(out["front_lockup_runs"])

    # Where round the lap the fronts work hardest under braking.
    points = max(10, min(LINE_MAX_POINTS, int(points)))
    bins: list[list[dict]] = [[] for _ in range(points)]
    for s in straight:
        pos = s.get("norm_pos")
        if pos is None:
            continue
        bins[min(points - 1, max(0, int(pos * points)))].append(s)
    slices = []
    for i, group in enumerate(bins):
        vals = []
        for s in group:
            got = [num(s, f"slip_{c}") for c in ("fl", "fr")]
            got = [v for v in got if v is not None and abs(v) <= SLIP_SANE_MAX]
            if got:
                vals.append(max(got))
        if vals:
            slices.append({"pos": round((i + 0.5) / points, 4),
                           "max_front_slip": round(max(vals), 3),
                           "samples": len(vals)})
    slices.sort(key=lambda e: -e["max_front_slip"])
    out["hardest_braking_slices"] = slices[:6]

    # The aid fields, reported as what they are rather than as activity.
    abs_f = _aid_field_note(samples, "abs_active")
    tc_f = _aid_field_note(samples, "tc_active")
    if abs_f or tc_f:
        out["aid_fields"] = {"abs": abs_f, "tc": tc_f}
        constant = [n for n, f in (("abs", abs_f), ("tc", tc_f))
                    if f and not f["varies"]]
        if constant:
            out["aid_fields_note"] = (
                f"{', '.join(constant)} held one value for the whole lap, "
                f"including down the straights, so {'they are' if len(constant) > 1 else 'it is'} "
                f"not a measure of intervention -- more likely a setting or "
                f"a slip threshold. CSP exposes the real flags "
                f"(absInAction, tractionControlInAction) but only to a "
                f"physics worker, which is single-player only.")
    return out


# --- body attitude ------------------------------------------------------
#
# AC reports roll and pitch in radians. Everything here is converted to
# degrees, because that is the unit every suspension conversation uses and
# a payload in radians invites a factor-of-57 mistake nobody would notice.
#
# The attitude AC reports is ABSOLUTE -- it is the body's angle to the
# world, not to the road surface, so it carries banking, camber, road grade
# and whatever static tilt and rake the car sits at. None of that is
# suspension movement. The fits below keep the signs and carry a free
# intercept precisely so the static part lands in the intercept instead of
# being read as roll the springs allowed.

# A lap needs samples with real load on them before a slope means anything:
# these are the thresholds that count a sample as loaded. They no longer
# filter what is fitted -- the low-g samples are what anchor the intercept
# -- only whether there was enough load on the lap to fit at all.
ROLL_MIN_LAT_G = 0.5
BRAKE_MIN_LON_G = 0.5
# Enough loaded samples that a gradient means something. A handful of
# points through a fitted line is not a measurement.
GRADIENT_MIN_SAMPLES = 40


def _fit_line(pairs: list[tuple[float, float]]) -> dict | None:
    """Ordinary least squares with a free intercept: y = slope*x + offset.

    Both the sign of x and the sign of y are kept, and the intercept is
    free, and both of those are load-bearing.

    An earlier version fitted |roll| against |lateral g| through the
    origin, which does not measure suspension roll at all. The attitude AC
    reports is absolute, so it carries track banking, camber and whatever
    static tilt the car sits at -- a car with a fixed 0.4 degrees of lean
    and no suspension travel whatsoever still produced a confident positive
    gradient. Taking absolutes made it worse than that: a sample where the
    body leant *into* the corner, against the load, was folded onto the
    same side as one leaning out and counted as supporting the fit.

    With signs kept, roll opposing the load subtracts from the slope the
    way it should, and the intercept absorbs the static offset instead of
    the slope absorbing it. Returns the slope, the intercept, and r2 --
    r2 says whether a straight line described the data at all, which is
    what stops a meaningless slope being quoted as a measurement.
    """
    n = len(pairs)
    if n < 2:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    if sxx <= 1e-12:
        return None                      # no spread in load: nothing to fit
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    slope = sxy / sxx
    offset = my - slope * mx
    syy = sum((y - my) ** 2 for _, y in pairs)
    r2 = (sxy * sxy) / (sxx * syy) if syy > 1e-12 else None
    return {"slope": slope, "offset": offset,
            "r2": None if r2 is None else max(0.0, min(1.0, r2))}


def attitude_report(lap: dict, samples: list[dict]) -> dict:
    """Body roll and pitch, and how much of each the car gives up per g.

    Every anti-roll bar and spring argument this project has had was
    settled -- or more often not settled -- by reasoning from load transfer
    or from what the car felt like. Roll gradient is the direct
    measurement: degrees of roll per g of lateral acceleration is total
    roll stiffness, so a bar change either moves it or did not do what it
    was meant to.
    """
    if not samples:
        return {"error": "no samples for this lap"}

    def num(s, k):
        v = s.get(k)
        return v if isinstance(v, (int, float)) and math.isfinite(v) else None

    rolls = [(num(s, "roll"), num(s, "acc_lat")) for s in samples]
    rolls = [(math.degrees(r), g) for r, g in rolls
             if r is not None and g is not None and abs(g) <= LAT_G_SANE_MAX]
    pitches = [(num(s, "pitch"), num(s, "acc_lon")) for s in samples]
    pitches = [(math.degrees(p), g) for p, g in pitches
               if p is not None and g is not None and abs(g) <= LON_G_SANE_MAX]

    if not rolls and not pitches:
        return {
            "lap_id": lap["id"],
            "has_attitude": False,
            "error": "this lap has no roll or pitch recorded. Attitude "
                     "arrived in schema v8; earlier laps cannot be "
                     "backfilled because the readings were never captured.",
        }

    out = {
        "lap_id": lap["id"],
        "lap_time": _fmt_time(lap["lap_time_ms"]),
        "has_attitude": True,
        "units": "degrees; AC reports radians and these are converted",
    }

    if rolls:
        # Signed, and fitted over every sane sample rather than only the
        # loaded ones. The low-g samples are what pin the intercept -- the
        # attitude the car holds when nothing is loading it, which is
        # static tilt plus whatever the road is doing underneath it. Fitting
        # loaded samples alone leaves the intercept to be extrapolated from
        # points that are all far from zero.
        loaded = [(g, r) for r, g in rolls if abs(g) >= ROLL_MIN_LAT_G]
        entry = {
            "max_abs_deg": round(max(abs(r) for r, _ in rolls), 2),
            "loaded_samples": len(loaded),
            "loaded_both_directions": (
                any(g >= ROLL_MIN_LAT_G for g, _ in loaded)
                and any(g <= -ROLL_MIN_LAT_G for g, _ in loaded)),
        }
        fit = (_fit_line([(g, r) for r, g in rolls])
               if len(loaded) >= GRADIENT_MIN_SAMPLES else None)
        if fit is not None:
            entry["gradient_deg_per_g"] = round(abs(fit["slope"]), 3)
            entry["static_offset_deg"] = round(fit["offset"], 3)
            entry["fit_r2"] = (None if fit["r2"] is None
                               else round(fit["r2"], 3))
            entry["gradient_note"] = (
                f"degrees of body roll per g of lateral acceleration, from "
                f"a signed least-squares fit of roll against lateral g with "
                f"a free intercept, over {len(rolls)} samples of which "
                f"{len(loaded)} carried at least {ROLL_MIN_LAT_G}g of "
                f"lateral load either way. Lower is a "
                f"stiffer car in roll, and this is what an anti-roll bar "
                f"change should move. `static_offset_deg` is the roll the "
                f"car holds at zero lateral g -- static tilt, road camber "
                f"and banking -- which is NOT suspension roll and is why "
                f"the fit carries an intercept instead of being forced "
                f"through the origin. The intercept removes the CONSTANT "
                f"part of that only -- banking that rises with cornering "
                f"load is correlated with lateral g and still lands in the "
                f"slope, so this is a car-plus-track number and comparisons "
                f"belong within one track.")
            # The signed slope, kept because the magnitude alone cannot show
            # a lap where roll ran the wrong way against load. Which sign
            # means "leaning out of the corner" is AC's convention and this
            # project has never verified it, so no direction is claimed --
            # what is usable is that the sign should be the SAME on every
            # lap of a car, and a lap that disagrees with its neighbours is
            # the anomaly.
            entry["fitted_slope_deg_per_g"] = round(fit["slope"], 3)
            entry["gradient_caveats"] = caveats = []
            if not entry["loaded_both_directions"]:
                caveats.append(
                    "every loaded sample was a corner in the same "
                    "direction, so the intercept is extrapolated rather "
                    "than bracketed and the split between static offset "
                    "and roll is weaker than the numbers suggest")
            if entry["fit_r2"] is not None and entry["fit_r2"] < 0.5:
                caveats.append(
                    f"r2 {entry['fit_r2']} -- roll was not close to linear "
                    f"in lateral g on this lap, so the gradient is a poor "
                    f"summary of it. Banking, kerbs or a bottoming car all "
                    f"do this.")
            if not caveats:
                del entry["gradient_caveats"]
        else:
            entry["gradient_deg_per_g"] = None
            entry["gradient_note"] = (
                f"only {len(loaded)} samples at {ROLL_MIN_LAT_G}g or more of "
                f"lateral load, "
                f"under the {GRADIENT_MIN_SAMPLES} needed for a gradient "
                f"worth quoting"
                if len(loaded) < GRADIENT_MIN_SAMPLES else
                f"{len(loaded)} samples carried at least "
                f"{ROLL_MIN_LAT_G}g, but "
                f"lateral g barely varied across them, so there is no "
                f"spread to fit a slope through")
        out["roll"] = entry

    if pitches:
        # Same argument as roll, and the same trap: absolute pitch carries
        # static rake and the grade of the road.
        #
        # Fitted over braking and coasting only -- every sample at or below
        # zero longitudinal g. Squat under power is a different suspension
        # question, rear springs and anti-squat, and folding it in would
        # make a number labelled "dive" partly a measurement of the rear of
        # the car. The cut is at zero rather than at -BRAKE_MIN_LON_G
        # because the near-zero samples are what pin the intercept; without
        # them it would be extrapolated from braking points alone.
        braking = [(g, p) for p, g in pitches if g <= -BRAKE_MIN_LON_G]
        fitted = [(g, p) for p, g in pitches if g <= 0.0]
        # Samples near zero longitudinal load. Without them the intercept
        # is extrapolated from braking points alone -- the same weakness
        # roll has on a track that only turns one way.
        unloaded = [1 for g, _ in fitted if g > -BRAKE_MIN_LON_G]
        entry = {
            "max_abs_deg": round(max(abs(p) for p, _ in pitches), 2),
            "braking_samples": len(braking),
            "unloaded_samples": len(unloaded),
        }
        fit = (_fit_line(fitted)
               if len(braking) >= GRADIENT_MIN_SAMPLES else None)
        if fit is not None:
            entry["dive_deg_per_g"] = round(abs(fit["slope"]), 3)
            entry["static_offset_deg"] = round(fit["offset"], 3)
            entry["fit_r2"] = (None if fit["r2"] is None
                               else round(fit["r2"], 3))
            entry["dive_note"] = (
                f"degrees of nose-down pitch per g of braking, from a "
                f"signed fit with a free intercept over {len(fitted)} "
                f"samples at or below zero longitudinal g, {len(braking)} "
                f"of them braking harder than {BRAKE_MIN_LON_G}g. Every "
                f"sample under power is excluded: squat is a "
                f"rear-suspension question and does not belong in a dive "
                f"figure. Front spring and damper changes "
                f"should move this; it is also what puts the splitter on "
                f"the ground. `static_offset_deg` is the rake the car sits "
                f"at with no longitudinal load, road grade included, and is "
                f"not dive -- though grade that varies with where the "
                f"driver brakes is correlated with braking g and still "
                f"lands in the slope.")
            # Signed, for the same reason as roll: which sign is nose-down
            # is AC's convention and unverified here, so the useful check is
            # that it agrees across laps of the same car.
            entry["fitted_slope_deg_per_g"] = round(fit["slope"], 3)
            entry["dive_caveats"] = caveats = []
            if not unloaded:
                caveats.append(
                    "every fitted sample was under braking, so the "
                    "intercept is extrapolated rather than bracketed and "
                    "the split between static rake and dive is weaker than "
                    "the numbers suggest")
            if entry["fit_r2"] is not None and entry["fit_r2"] < 0.5:
                caveats.append(
                    f"r2 {entry['fit_r2']} -- pitch was not close to linear "
                    f"in longitudinal g on this lap, so the gradient is a "
                    f"poor summary of it")
            if not caveats:
                del entry["dive_caveats"]
        else:
            entry["dive_deg_per_g"] = None
            entry["dive_note"] = (
                f"only {len(braking)} samples braking above "
                f"{BRAKE_MIN_LON_G}g, under the {GRADIENT_MIN_SAMPLES} "
                f"needed for a dive figure worth quoting"
                if len(braking) < GRADIENT_MIN_SAMPLES else
                f"{len(braking)} braking samples, but longitudinal g barely "
                f"varied across them, so there is no spread to fit a slope "
                f"through")
        out["pitch"] = entry

    return out


def compare_laps(lap_a: dict, samples_a: list[dict],
                 lap_b: dict, samples_b: list[dict]) -> dict:
    """Corner-by-corner comparison of two laps, matched by track position."""
    # One bar for both laps. Detecting each against its own peak meant the
    # harder-driven lap dropped its marginal corners, and a corner missing
    # from one side simply does not appear in the comparison -- so the two
    # laps were being compared on whichever corners they happened to agree
    # existed.
    detail = lat_g_reference_detail([samples_a, samples_b])
    ref = detail["reference"]
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

    # Numbered across both laps at once, for the same reason there is one
    # detection bar across both: numbering each lap on its own would let the
    # same piece of road be T5 on one side of a delta and T4 on the other,
    # inside a payload whose whole job is to put the two side by side.
    turn_map = corner_map([ca, cb])
    label_corners(matched, turn_map["turns"])

    return {
        "lap_a": {"id": lap_a["id"], "time": _fmt_time(lap_a["lap_time_ms"])},
        "lap_b": {"id": lap_b["id"], "time": _fmt_time(lap_b["lap_time_ms"])},
        "time_delta_ms": lap_a["lap_time_ms"] - lap_b["lap_time_ms"],
        "note": "deltas are lap_a minus lap_b; positive min_speed_delta means"
                " lap_a carried more speed",
        # Which bar produced these corners, and how firm it is. Two laps is
        # the weakest case for a median, and this tool always passes two.
        "corner_detection": corner_detection_note(
            ref, detail["laps"], spread_g=detail["spread_g"]),
        "corners_found": {"lap_a": len(ca), "lap_b": len(cb),
                          "matched": len(matched)},
        # Where the labels on those corners are. Two laps is a thin basis
        # for a numbering and this tool always has exactly two, so the table
        # travels with the payload rather than being looked up elsewhere
        # against a different set of laps.
        "turns": [{"turn": t["turn"], "apex_pos": t["apex_pos"]}
                  for t in turn_map["turns"]],
        "turns_note": turn_map["note"],
        "corners": matched,
    }
