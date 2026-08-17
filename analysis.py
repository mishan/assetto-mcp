"""Lap analysis: turn raw samples into compact, LLM-friendly summaries.

Design rule: the model never sees raw 25Hz telemetry. Everything here reduces
a lap to numbers a race engineer would actually reason about.

Pure Python on purpose - no numpy dependency to install on the gaming PC.
"""

from statistics import mean

WHEELS = ("fl", "fr", "rl", "rr")


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
    """Find corners as local minima of smoothed speed.

    Returns one dict per corner with apex position, min speed, brake point,
    peak steering, and a front-vs-rear slip balance metric around the apex
    (positive = front sliding more = understeer tendency).
    """
    if len(samples) < 50:
        return []

    speed = _smooth([s["speed_kmh"] for s in samples], 11)
    vmax = max(speed)
    n = len(speed)
    win = max(10, n // 20)  # ~5% of the lap on each side

    corners = []
    i = win
    while i < n - win:
        seg_min = min(speed[i - win:i + win + 1])
        if speed[i] != seg_min or speed[i] >= 0.92 * vmax:
            i += 1
            continue
        # Prominence vs the fastest points within the window on each side.
        left_max = max(speed[max(0, i - win):i + 1])
        right_max = max(speed[i:min(n, i + win + 1)])
        if min(left_max, right_max) - speed[i] > 0.08 * vmax:
            corners.append(_corner_stats(
                samples, max(0, i - win), i, min(n - 1, i + win)))
            i += win  # skip past this corner
        else:
            i += 1

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
    front_slip = mean(mean((s["slip_fl"], s["slip_fr"])) for s in seg)
    rear_slip = mean(mean((s["slip_rl"], s["slip_rr"])) for s in seg)
    peak_steer = max(abs(s["steer"]) for s in seg)

    # Throttle-on point after apex. Search well past the exit window:
    # drivers often reach half throttle only after the corner "ends".
    throttle_pos = None
    search_end = min(len(samples), exit_idx + (exit_idx - apex_idx) + 1)
    for j in range(apex_idx, search_end):
        if samples[j]["gas"] > 0.5:
            throttle_pos = samples[j]["norm_pos"]
            break

    return {
        "apex_pos": round(apex["norm_pos"], 4),
        "min_speed_kmh": round(apex["speed_kmh"], 1),
        "gear": apex["gear"],
        "brake_point_pos": round(brake_pos, 4) if brake_pos is not None else None,
        "throttle_on_pos": round(throttle_pos, 4) if throttle_pos is not None else None,
        "peak_steer_deg": round(peak_steer, 1),
        "slip_balance": round(front_slip - rear_slip, 3),
        "front_slip": round(front_slip, 3),
        "rear_slip": round(rear_slip, 3),
    }


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
    slip_balances = [c["slip_balance"] for c in corners]

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
        "balance_note": ("positive slip_balance = front slides more "
                         "(understeer); negative = rear (oversteer)"),
        "corners": corners,
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
                "slip_balance_delta": round(
                    c["slip_balance"] - best["slip_balance"], 3),
            })

    return {
        "lap_a": {"id": lap_a["id"], "time": _fmt_time(lap_a["lap_time_ms"])},
        "lap_b": {"id": lap_b["id"], "time": _fmt_time(lap_b["lap_time_ms"])},
        "time_delta_ms": lap_a["lap_time_ms"] - lap_b["lap_time_ms"],
        "note": "deltas are lap_a minus lap_b; positive min_speed_delta means"
                " lap_a carried more speed",
        "corners": matched,
    }
