"""Suspension analysis: damper histograms, ride height, roll balance.

Three questions this answers, in the order an engineer asks them:

  1. Are the dampers doing the right thing?   -> damper_histogram()
  2. Is the car running low enough / too low?  -> ride_height_report()
  3. Which axle is taking the load transfer?   -> roll_balance()

Two things make this harder than it looks, and both are handled explicitly
rather than assumed away.

**Sign convention.** CSP documents neither the units nor the sign of
suspension travel. Whether a rising number means the wheel is compressing or
extending decides whether "add bump" or "add rebound" is the right advice --
getting it backwards is worse than saying nothing. So the convention is
*inferred from the data*: under heavy braking the front suspension
compresses, which is about as reliable as vehicle dynamics gets. If a lap
contains no usable braking, the direction is reported as unknown and the
bump/rebound split is withheld.

**Sample rate.** Damper velocity is a fast signal. Sampled at render rate
(60-144Hz) and differentiated, the high-speed band is aliased into nonsense.
Samples therefore carry the tier that produced them, and anything derived
from render-rate data is labelled as body motion rather than damper
behaviour. See SOURCE_* below.
"""

import math
from statistics import median

WHEELS = ("fl", "fr", "rl", "rr")
FRONT = ("fl", "fr")
REAR = ("rl", "rr")

# Where a batch of samples came from. The distinction is not cosmetic: it
# decides which analyses are honest to run.
SOURCE_APP = "app"          # render rate, ~60-144Hz, aliased above ~30Hz
SOURCE_WORKER = "worker"    # CSP physics worker, 333Hz, true damper rate

# Above this, a render-rate sample stream cannot describe damper behaviour.
# Nyquist for a 60Hz stream is 30Hz; real damper content runs well past that.
APP_TIER_MAX_USEFUL_HZ = 30.0

# Damper velocity bins in mm/s, the shape a motorsport damper histogram
# conventionally takes: fine near zero where the low-speed valving lives,
# coarse further out where it is all bumps and kerbs.
VELOCITY_BINS_MM_S = (5, 10, 25, 50, 75, 100, 150, 250)

# A lap with fewer than this many usable samples isn't a histogram, it's an
# anecdote.
MIN_HISTOGRAM_SAMPLES = 200

# Braking hard enough that front compression is not in doubt.
BRAKING_THRESHOLD = 0.6

# The sign inference assumes brake pedal == weight transferring forward. A
# stationary car with the pedal held satisfies the first and none of the
# second, and an out-lap spent sitting in the garage on the brakes supplied
# thousands of such samples -- enough to outvote the real braking zones and
# invert the answer, at confidence 1.0. Both the speed floor and the cap on
# how much of a lap may be classified as braking exist to keep parked and
# crawling samples out of it.
SIGN_MIN_SPEED_KMH = 50.0
SIGN_MAX_BRAKING_SHARE = 0.5


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v)


def _rates(samples: list[dict], key: str) -> list[tuple[float, dict]]:
    """(d<key>/dt in m/s, sample) pairs, using each sample's own timestamp.

    Timestamps come from AC's physics clock, not wall time, so a dropped
    frame produces a longer dt rather than a phantom spike. Pairs where the
    clock did not advance are dropped: they are the same physics step
    observed twice, and dividing by that dt invents a velocity.
    """
    out = []
    for prev, cur in zip(samples, samples[1:]):
        dt_ms = cur.get("t_ms", 0) - prev.get("t_ms", 0)
        if dt_ms <= 0 or dt_ms > 250:      # 0 = duplicate frame, 250 = a stall
            continue
        a, b = prev.get(key), cur.get(key)
        if not _finite(a) or not _finite(b):
            continue
        out.append(((b - a) / (dt_ms / 1000.0), cur))
    return out


# --- sign convention ----------------------------------------------------


def infer_compression_sign(samples: list[dict]) -> dict:
    """Work out which direction of travel change means compression.

    Under heavy braking the front suspension compresses. That is the most
    dependable single fact in vehicle dynamics, and it lets the data tell us
    its own convention instead of hard-coding a guess about CSP's.

    Returns {"sign": -1|1|None, "confidence": 0..1, "basis": str}. sign is
    the multiplier that makes compression positive. None means we could not
    tell, and callers must then withhold anything bump/rebound flavoured.
    """
    # Compare where the front axle *sits* under braking against where it
    # sits at rest, not how fast it is moving. Under sustained braking the
    # nose is down but barely moving, so the rate is near zero for most of
    # the zone and only the brief onset carries the signal -- easily lost in
    # noise. The displacement holds for the whole zone.
    braking, free, slow = [], [], 0
    for s in samples:
        fl, fr = s.get("travel_fl"), s.get("travel_fr")
        brake = s.get("brake")
        if not (_finite(fl) and _finite(fr)) or not _finite(brake):
            continue
        # A car below this isn't transferring meaningful load, whatever the
        # pedal says. Missing speed is treated as too slow rather than
        # assumed fast: guessing here is what produced the inverted answer.
        speed = s.get("speed_kmh")
        if not _finite(speed) or speed < SIGN_MIN_SPEED_KMH:
            slow += 1
            continue
        front = (fl + fr) / 2
        if brake >= BRAKING_THRESHOLD:
            braking.append(front)
        elif brake < 0.05:
            free.append(front)

    if len(braking) < 20 or len(free) < 20:
        return {"sign": None, "confidence": 0.0,
                "basis": f"need 20 samples each side above "
                         f"{SIGN_MIN_SPEED_KMH:.0f}km/h; got {len(braking)} "
                         f"braking and {len(free)} off the brakes"
                         + (f" ({slow} discarded as too slow)" if slow else "")}

    # A real lap brakes for well under half its duration. A larger share
    # means these are not braking zones -- a stop-start out-lap, or a
    # session spent shuffling in the pits -- and the comparison against
    # "resting" travel is not measuring what it thinks it is.
    share = len(braking) / (len(braking) + len(free))
    if share > SIGN_MAX_BRAKING_SHARE:
        return {"sign": None, "confidence": 0.0,
                "basis": f"{share:.0%} of usable samples are under "
                         f"{BRAKING_THRESHOLD:.0%} brake, too many for these "
                         f"to be braking zones; refusing to infer a sign "
                         f"from what looks like a stop-start lap"}

    med_b, med_f = median(braking), median(free)
    delta = med_b - med_f
    if abs(delta) < 1e-5:      # 0.01mm: the nose did not measurably move
        return {"sign": None, "confidence": 0.0,
                "basis": "front travel is indistinguishable on and off the "
                         "brakes; cannot tell which way is compression"}

    # The direction the front moves under braking IS compression, so that
    # direction has to come out positive. Inverting this swaps bump and
    # rebound in every histogram and reverses the advice that follows it.
    sign = 1 if delta > 0 else -1

    # Confidence from separation: how many braking samples are clear of the
    # resting median in the expected direction. Two overlapping clouds mean
    # we are reading noise.
    clear = sum(1 for v in braking if (v - med_f > 0) == (delta > 0))
    agreement = clear / len(braking)
    return {
        "sign": sign,
        "confidence": round(max(0.0, (agreement - 0.5) * 2), 3),
        "basis": (f"front axle sits {abs(delta) * 1000:.1f}mm "
                  f"{'higher' if delta > 0 else 'lower'} under "
                  f">{BRAKING_THRESHOLD:.0%} brake than off it "
                  f"({len(braking)} braking vs {len(free)} free samples above "
                  f"{SIGN_MIN_SPEED_KMH:.0f}km/h, {agreement:.0%} "
                  f"separated), so {'increasing' if sign > 0 else 'decreasing'}"
                  f" travel = compression"),
    }


# --- damper histogram ---------------------------------------------------


def _bin_index(speed_mm_s: float) -> int:
    for i, edge in enumerate(VELOCITY_BINS_MM_S):
        if speed_mm_s < edge:
            return i
    return len(VELOCITY_BINS_MM_S)


def _bin_labels() -> list[str]:
    labels = []
    lo = 0
    for edge in VELOCITY_BINS_MM_S:
        labels.append(f"{lo}-{edge}")
        lo = edge
    labels.append(f"{lo}+")
    return labels


def _rate_matches_tier(samples: list[dict], source: str) -> str | None:
    """Does the measured rate support the tier the rows claim to be?

    The tier label comes from the client, and a client that starts a physics
    worker which then never produces will happily go on labelling its
    render-rate fallback as 333Hz data. That is the one failure mode this
    module exists to prevent, so the label is checked against the evidence
    rather than believed. Returns a warning, or None if the claim holds.
    """
    measured = _effective_rate_hz(samples)
    if source == SOURCE_WORKER and measured < 150.0:
        return (f"These rows claim to come from the 333Hz physics worker, "
                f"but they arrived at ~{measured:.0f}Hz. Something upstream "
                f"is mislabelling them, so treat the damper numbers with "
                f"the same suspicion as render-rate data.")
    return None


def damper_histogram(samples: list[dict], source: str = SOURCE_APP) -> dict:
    """Distribution of damper velocity, split bump vs rebound per axle.

    The classic tool for choosing bump/rebound clicks: most of a lap should
    sit in the low-speed bins (body control), with a thin high-speed tail
    (kerbs and bumps). A fat high-speed bump tail means the valving is
    packing down over kerbs; a lopsided bump/rebound ratio means the car
    settles asymmetrically.
    """
    if len(samples) < MIN_HISTOGRAM_SAMPLES:
        return {"available": False,
                "reason": f"only {len(samples)} samples; need "
                          f"{MIN_HISTOGRAM_SAMPLES} for a meaningful "
                          f"distribution"}

    conv = infer_compression_sign(samples)
    labels = _bin_labels()

    out = {
        "available": True,
        "source": source,
        "bins_mm_s": labels,
        "sign_convention": conv,
        "axles": {},
    }

    rate = _effective_rate_hz(samples)
    out["measured_rate_hz"] = round(rate, 1)
    if source == SOURCE_APP:
        out["caution"] = (
            f"Sampled at ~{rate:.0f}Hz on the render thread, so anything "
            f"above ~{APP_TIER_MAX_USEFUL_HZ:.0f}Hz is aliased. Read this as "
            f"body motion -- roll, pitch, dive -- not as damper valving. "
            f"The high-speed bins are not trustworthy at this rate; a CSP "
            f"physics worker (333Hz) is needed for real damper histograms.")
    else:
        mismatch = _rate_matches_tier(samples, source)
        if mismatch:
            out["caution"] = mismatch

    for axle, wheels in (("front", FRONT), ("rear", REAR)):
        counts_bump = [0] * (len(VELOCITY_BINS_MM_S) + 1)
        counts_reb = [0] * (len(VELOCITY_BINS_MM_S) + 1)
        peak_bump = peak_reb = 0.0
        total = 0

        for w in wheels:
            for rate, _ in _rates(samples, f"travel_{w}"):
                mm_s = rate * 1000.0
                total += 1
                if conv["sign"] is None:
                    # Direction unknown: still report magnitude, which is
                    # what tells you whether the dampers are working at all.
                    counts_bump[_bin_index(abs(mm_s))] += 1
                    peak_bump = max(peak_bump, abs(mm_s))
                    continue
                signed = mm_s * conv["sign"]
                if signed >= 0:
                    counts_bump[_bin_index(signed)] += 1
                    peak_bump = max(peak_bump, signed)
                else:
                    counts_reb[_bin_index(-signed)] += 1
                    peak_reb = max(peak_reb, -signed)

        if not total:
            continue

        axle_out = {
            "samples": total,
            "peak_mm_s": round(peak_bump if conv["sign"] is None
                               else max(peak_bump, peak_reb), 1),
        }
        if conv["sign"] is None:
            axle_out["magnitude_pct"] = _as_pct(counts_bump, total)
            axle_out["note"] = ("Direction of travel could not be "
                                "established, so bump and rebound are "
                                "combined. Magnitudes are still valid.")
        else:
            nb, nr = sum(counts_bump), sum(counts_reb)
            axle_out["bump_pct"] = _as_pct(counts_bump, nb)
            axle_out["rebound_pct"] = _as_pct(counts_reb, nr)
            axle_out["peak_bump_mm_s"] = round(peak_bump, 1)
            axle_out["peak_rebound_mm_s"] = round(peak_reb, 1)
            # Low-speed share is where the valving the driver feels lives.
            axle_out["low_speed_share_pct"] = round(
                100 * (sum(counts_bump[:3]) + sum(counts_reb[:3]))
                / max(1, nb + nr), 1)
        out["axles"][axle] = axle_out

    return out


def _as_pct(counts: list[int], total: int) -> dict:
    if not total:
        return {}
    return {label: round(100 * c / total, 1)
            for label, c in zip(_bin_labels(), counts) if c}


def _effective_rate_hz(samples: list[dict]) -> float:
    spans = [b.get("t_ms", 0) - a.get("t_ms", 0)
             for a, b in zip(samples, samples[1:])]
    spans = [s for s in spans if 0 < s <= 250]
    return 1000.0 / median(spans) if spans else 0.0


# --- ride height and bottoming -----------------------------------------


def ride_height_report(samples: list[dict], buckets: int = 20) -> dict:
    """Where the car runs low, and where it runs out of travel.

    Ride height is a slow signal, so unlike the damper histogram this is
    trustworthy at render rate.
    """
    usable = [s for s in samples
              if _finite(s.get("ride_f")) and _finite(s.get("ride_r"))]
    if not usable:
        return {"available": False, "reason": "no ride height samples"}

    front = [s["ride_f"] for s in usable]
    rear = [s["ride_r"] for s in usable]

    out = {
        "available": True,
        "front_mm": _spread(front),
        "rear_mm": _spread(rear),
        # Rake is what actually drives aero balance; the absolute heights
        # matter less than the difference between them.
        "rake_mm": _spread([s["ride_r"] - s["ride_f"] for s in usable]),
    }

    plank = [s["plank_wear"] for s in usable if _finite(s.get("plank_wear"))]
    if plank:
        worst = max(plank)
        out["plank_wear"] = {
            "max": round(worst, 4),
            "note": ("Plank wear is AC's own measure of the floor touching "
                     "down. Rising through a session means the car is "
                     "bottoming; at 1.0 it is fully worn."),
        }

    # Where on track the car is lowest -- that is the corner to look at.
    lows = _lowest_points(usable, buckets)
    if lows:
        out["lowest_points"] = lows
    return out


def _spread(values: list[float]) -> dict:
    """Summarise a distance channel in mm. Inputs are metres."""
    mm = [v * 1000.0 for v in values]
    mm.sort()
    return {
        "min": round(mm[0], 1),
        "median": round(mm[len(mm) // 2], 1),
        "max": round(mm[-1], 1),
        # 5th percentile: how low it gets routinely, as opposed to once.
        "p5": round(mm[max(0, int(0.05 * len(mm)))], 1),
    }


def _lowest_points(samples: list[dict], buckets: int) -> dict:
    """Where each end of the car runs lowest, reported separately.

    Ranking by whichever end is lower would hide the rear entirely on most
    cars: the front is nearly always the lower of the two, so a section
    where the rear squats onto the floor would never surface.
    """
    by_bucket: dict[int, list[dict]] = {}
    for s in samples:
        pos = s.get("spline")
        if not _finite(pos):
            continue
        by_bucket.setdefault(min(buckets - 1, int(pos * buckets)), []).append(s)

    rows = []
    for b, group in by_bucket.items():
        rows.append({
            "spline": round((b + 0.5) / buckets, 3),
            "front_mm": round(min(s["ride_f"] for s in group) * 1000, 1),
            "rear_mm": round(min(s["ride_r"] for s in group) * 1000, 1),
        })
    return {
        "front": sorted(rows, key=lambda r: r["front_mm"])[:3],
        "rear": sorted(rows, key=lambda r: r["rear_mm"])[:3],
    }


# --- roll and load transfer --------------------------------------------


def roll_balance(samples: list[dict]) -> dict:
    """Which axle takes the lateral load transfer.

    The number an engineer reaches for when deciding ARB and spring splits:
    the fraction of total lateral load transfer taken by the front axle.
    Above 50% the front is doing more of the work, which pushes the balance
    toward understeer -- and should agree with the slip_balance metric
    lap_summary already reports. When those two disagree, something else is
    going on (aero, differential, tyre temperatures) and that is worth
    knowing.
    """
    usable = [s for s in samples
              if all(_finite(s.get(f"load_{w}")) for w in WHEELS)]
    if len(usable) < 50:
        return {"available": False,
                "reason": f"only {len(usable)} samples with wheel loads"}

    fronts, rears, cornering = [], [], 0
    for s in usable:
        fl, fr = s["load_fl"], s["load_fr"]
        rl, rr = s["load_rl"], s["load_rr"]
        f_transfer = abs(fl - fr)
        r_transfer = abs(rl - rr)
        total = f_transfer + r_transfer
        # Only meaningful while the car is actually loaded up. In a straight
        # line the transfer is noise, and averaging it in drags every
        # session toward 50%.
        if total < 200:      # newtons
            continue
        cornering += 1
        fronts.append(f_transfer)
        rears.append(r_transfer)

    if cornering < 30:
        return {"available": False,
                "reason": "not enough cornering load transfer to measure"}

    f_mean = sum(fronts) / len(fronts)
    r_mean = sum(rears) / len(rears)
    tlltd = 100 * f_mean / (f_mean + r_mean)

    out = {
        "available": True,
        "front_load_transfer_pct": round(tlltd, 1),
        "cornering_samples": cornering,
        "reading": ("above 50 = front axle takes more of the lateral load "
                    "transfer, which biases toward understeer; below 50 "
                    "biases toward oversteer"),
    }

    travel = [s for s in usable
              if all(_finite(s.get(f"travel_{w}")) for w in WHEELS)]
    if len(travel) >= 50:
        f_roll = max(abs(s["travel_fl"] - s["travel_fr"]) for s in travel)
        r_roll = max(abs(s["travel_rl"] - s["travel_rr"]) for s in travel)
        out["peak_roll_travel_mm"] = {"front": round(f_roll * 1000, 1),
                                      "rear": round(r_roll * 1000, 1)}
    return out


# --- top level ----------------------------------------------------------


def summarise(samples: list[dict], source: str | None = None) -> dict:
    """Everything above, for one lap.

    Rows carry their own `source`, and the two tiers are not interchangeable
    -- they are different channels read from different APIs at different
    rates, with separate clocks. So the damper histogram is built from one
    tier only (the faster, if both are present), while ride height and roll
    balance are built from whichever rows actually carry those fields.
    Differentiating across a tier boundary would manufacture a high-speed
    tail out of nothing but the offset between two zero points -- which
    reads exactly like valving that packs down over kerbs.
    """
    if not samples:
        return {"available": False, "reason": "no suspension samples"}
    samples = sorted(samples, key=lambda s: s.get("t_ms", 0))

    by_source: dict[str, list[dict]] = {}
    for s in samples:
        by_source.setdefault(s.get("source") or source or SOURCE_APP,
                             []).append(s)

    # Travel: one tier only, the faster one if we have it.
    travel_source = (SOURCE_WORKER if by_source.get(SOURCE_WORKER)
                     else (source or next(iter(by_source))))
    travel_rows = by_source.get(travel_source, [])

    # Ride height, loads and plank wear live on ac.getCar() and so only
    # ever appear on app rows, whatever tier the travel came from.
    slow_rows = [s for s in samples
                 if _finite(s.get("ride_f")) or _finite(s.get("load_fl"))]

    out = {
        "available": True,
        "source": travel_source,
        "samples": len(samples),
        "sample_rate_hz": round(_effective_rate_hz(travel_rows), 1),
        "dampers": damper_histogram(travel_rows, travel_source),
        "ride_height": ride_height_report(slow_rows or samples),
        "roll": roll_balance(slow_rows or samples),
    }
    if len(by_source) > 1:
        out["tiers_present"] = {k: len(v) for k, v in by_source.items()}
        out["tier_note"] = (
            f"Both capture tiers are present. Damper analysis uses the "
            f"{travel_source} rows only; ride height and load transfer use "
            f"the render-rate rows, which are the only ones carrying them.")
    return out


def compact(summary: dict) -> dict | None:
    """The few lines worth folding into lap_summary.

    lap_summary has a ~1KB budget and suspension is not its main job, so
    this is a pointer to suspension_report rather than a replacement for it.
    """
    if not summary.get("available"):
        return None
    out = {"source": summary["source"],
           "sample_rate_hz": summary["sample_rate_hz"]}

    ride = summary.get("ride_height") or {}
    if ride.get("available"):
        out["min_ride_mm"] = {"front": ride["front_mm"]["min"],
                              "rear": ride["rear_mm"]["min"]}
    roll = summary.get("roll") or {}
    if roll.get("available"):
        out["front_load_transfer_pct"] = roll["front_load_transfer_pct"]

    dampers = summary.get("dampers") or {}
    if dampers.get("available"):
        front = (dampers.get("axles") or {}).get("front") or {}
        if "low_speed_share_pct" in front:
            out["front_low_speed_damper_pct"] = front["low_speed_share_pct"]
        if dampers.get("caution"):
            out["caution"] = "render-rate sampling; see suspension_report"
    return out
