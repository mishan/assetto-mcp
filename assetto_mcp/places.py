"""Say where something happened in meters and turns, not lap fractions.

The game places everything by `normalizedCarPosition`, a fraction of the
lap from 0 at the start/finish line to 1, and every analysis keeps that
because it is what lines laps up. A driver cannot use it. Told "6.8 g at
0.065", the driver at Interlagos had no idea where that was; told "T1, 70 m
after turn-in", they did. Drivers navigate by corners and by the 50 m and
100 m boards before them, so that is what a position is turned into here.

This runs over a finished payload rather than inside every analysis, so
each tool gets the same wording and the analyses keep speaking in the
fraction they compute with. The fraction stays in the payload beside the
meters, because machines still need it.
"""

from __future__ import annotations

from statistics import median

from . import db

# Keys holding a lap fraction, and the key the same place in meters past the
# start/finish line goes under. A whitelist rather than "anything ending in
# pos": the names are few and stable, and a guess at a key that only looks
# like a position would put a confident number on something that is not one.
POSITION_KEYS = {
    "pos": "at_m",
    "spline": "at_m",
    "apex_pos": "apex_m",
    "entry_pos": "entry_m",
    "exit_pos": "exit_m",
    "brake_point_pos": "brake_point_m",
    "throttle_on_pos": "throttle_on_m",
    "from_pos": "from_m",
    "to_pos": "to_m",
    "from": "from_m",
    "to": "to_m",
    "measured_from": "measured_from_m",
    "measured_to": "measured_to_m",
}

# Differences between two positions, and the key the same difference in
# meters goes under. Signed, and not wrapped: a brake point 0.036 of a lap
# later is 210 m later at Suzuka, which is the number anyone can picture.
DELTA_KEYS = {
    "brake_point_delta": "brake_point_delta_m",
}

# Which of a dict's positions `where` describes, first match wins. A span
# (from/to) is described by both ends.
WHERE_FROM = ("pos", "spline", "from_pos", "brake_point_pos", "apex_pos")
SPANS = (("from", "to"), ("from_pos", "to_pos"))

# Within this of the apex, a place is "the apex" rather than a few meters
# either side of it. About a car length and a half; closer than the 25 Hz
# samples place anything at racing speed.
APEX_M = 15

# How many recent clean laps a missing track length is estimated from.
ESTIMATE_LAPS = 3


def lap_distance_m(samples: list[dict]) -> float | None:
    """How far the car travelled over a lap, from speed and the clock.

    Checked against sessions whose length the game reported: within 0.4%
    on clean laps at Sebring, Kyalami and Interlagos, a few meters long
    because the car takes a wider line than the spline. A spin makes it
    meaningless, which is why only clean laps are fed to it.
    """
    total, last = 0.0, None
    for s in samples:
        t, v = s.get("t_ms"), s.get("speed_kmh")
        if t is None or v is None:
            continue
        if last is not None and t > last[0]:
            total += (t - last[0]) / 1000 * (v + last[1]) / 2 / 3.6
        last = (t, v)
    return total or None


def session_length(conn, session_id: int) -> tuple[float | None, str | None]:
    """The session's track length in meters, and where it came from.

    `game` is the length AC reports, stored for every session since the
    collector started recording it and for earlier ones where the in-game
    app sent it. `estimated` is integrated from the newest clean laps'
    speed, for the sessions that have neither.
    """
    session = db.get_session(conn, session_id)
    if not session:
        return None, None
    if session.get("track_length_m"):
        return float(session["track_length_m"]), "game"
    laps = [l for l in db.list_laps(conn, session_id, limit=32)
            if db.lap_usability(l)[0] and not l.get("invalid")]
    dists = []
    for lap in laps[:ESTIMATE_LAPS]:
        d = lap_distance_m(db.get_samples(conn, lap["id"]))
        if d:
            dists.append(d)
    if not dists:
        return None, None
    return round(median(dists), 1), "estimated"


def _fwd(a: float, b: float) -> float:
    """How far forward from a to b as a fraction of the lap, across the line."""
    return (b - a) % 1.0


def _signed(a: float, b: float) -> float:
    """b minus a as a fraction of the lap, the short way round the line."""
    return (b - a + 0.5) % 1.0 - 0.5


def _entry(t: dict) -> float:
    return t["entry_pos"] if t.get("entry_pos") is not None else t["apex_pos"]


def _exit(t: dict) -> float:
    return t["exit_pos"] if t.get("exit_pos") is not None else t["apex_pos"]


def where(pos: float, length_m: float, turns: list[dict]) -> str:
    """A place on the lap in the words a driver would use.

    Inside a turn it is measured from turn-in or the apex; in a braking zone
    it is the distance still to go to turn-in, which is what the boards
    count down; on a straight it is the distance to the next turn or from
    the last one, whichever is nearer.
    """
    def m(frac):
        return round(frac * length_m)

    usable = [t for t in turns
              if t.get("turn") and _is_fraction(t.get("apex_pos"))]
    if not usable:
        return f"{m(pos)} m past the start/finish line"

    # A turn list carrying apexes only -- the one compare_runs and
    # compare_laps put in their payloads -- has no turn-in to count from,
    # so the nearest apex is the reference instead.
    if not all(t.get("entry_pos") is not None
               and t.get("exit_pos") is not None for t in usable):
        ahead = min(usable, key=lambda t: _fwd(pos, t["apex_pos"]))
        behind = min(usable, key=lambda t: _fwd(t["apex_pos"], pos))
        to_go, since = (m(_fwd(pos, ahead["apex_pos"])),
                        m(_fwd(behind["apex_pos"], pos)))
        if min(to_go, since) <= APEX_M:
            return f"{(ahead if to_go <= since else behind)['turn']} apex"
        if to_go <= since:
            return f"{to_go} m before the {ahead['turn']} apex"
        return f"{since} m after the {behind['turn']} apex"

    for t in usable:
        into = _fwd(_entry(t), pos)
        if into <= _fwd(_entry(t), _exit(t)):
            past_apex = m(into - _fwd(_entry(t), t["apex_pos"]))
            if abs(past_apex) <= APEX_M:
                return f"{t['turn']} apex"
            if past_apex < 0:
                return f"{t['turn']}, {m(into)} m after turn-in"
            return f"{t['turn']}, {past_apex} m after the apex"

    # A brake point at or after turn-in -- a corner braked into rather than
    # before -- has no braking zone ahead of it to be in.
    for t in usable:
        brake = t.get("brake_point_pos")
        if (brake is not None and _signed(brake, _entry(t)) > 0
                and _fwd(brake, pos) < _fwd(brake, _entry(t))):
            return (f"{t['turn']} braking zone, "
                    f"{m(_fwd(pos, _entry(t)))} m before turn-in")

    # On a straight: the distance to the next turn-in or from the last
    # exit, whichever is shorter.
    ahead = min(usable, key=lambda t: _fwd(pos, _entry(t)))
    behind = min(usable, key=lambda t: _fwd(_exit(t), pos))
    to_go, since = _fwd(pos, _entry(ahead)), _fwd(_exit(behind), pos)
    if to_go <= since:
        return f"{m(to_go)} m before {ahead['turn']} turn-in"
    return f"{m(since)} m after {behind['turn']} exit"


def _is_fraction(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and -0.1 <= v <= 1.1)


def _place_dict(d: dict, length_m: float, turns: list[dict]) -> dict:
    out = {}
    for key, value in d.items():
        out[key] = value
        m_key = POSITION_KEYS.get(key)
        # `from` and `to` are also words other payloads use for lap
        # numbers and labels. Only a float is a position.
        if m_key and m_key not in d and _is_fraction(value) and (
                key not in ("from", "to") or isinstance(value, float)):
            out[m_key] = round((value % 1.0 if value != 1.0 else 1.0)
                               * length_m)
        d_key = DELTA_KEYS.get(key)
        if (d_key and d_key not in d and isinstance(value, (int, float))
                and not isinstance(value, bool) and abs(value) <= 0.5):
            out[d_key] = round(value * length_m)

    # A labelled turn already says where it is. What a driver also wants of
    # one is how far before turn-in the braking starts -- the board to brake
    # at. Negative when the braking starts after turn-in, which a short
    # braking zone into a second apex does.
    if d.get("turn"):
        brake, entry = d.get("brake_point_pos"), d.get("entry_pos")
        if _is_fraction(brake) and _is_fraction(entry):
            out["brake_before_turn_in_m"] = round(
                _signed(brake, entry) * length_m)
        return out

    if "where" in d:
        return out
    for a, b in SPANS:
        if isinstance(d.get(a), float) and isinstance(d.get(b), float):
            out["where"] = (f"from {where(d[a] % 1.0, length_m, turns)} "
                            f"to {where(d[b] % 1.0, length_m, turns)}")
            return out
    pos = next((d[k] for k in WHERE_FROM if _is_fraction(d.get(k))), None)
    if pos is not None:
        out["where"] = where(pos % 1.0, length_m, turns)
    return out


def place(payload, length_m: float | None, turns: list[dict] | None):
    """Add meters and a `where` beside every lap-fraction position.

    Returns the payload unchanged when there is no track length at all.
    `turns` is the numbering the payload's own labels came from, so a
    `where` never names a turn the rest of the payload calls something else.
    """
    if not length_m:
        return payload
    turns = turns or []

    def walk(node):
        if isinstance(node, dict):
            placed = _place_dict(node, length_m, turns)
            return {k: (walk(v) if k not in POSITION_KEYS else v)
                    for k, v in placed.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(payload)
