"""The circuit's own corner names, read from the track's sections.ini.

Turn numbers built from the laps (turns.py) are the run's, not the
circuit's: a kink taken flat is never detected, so every number after it is
one short, and a circuit that calls a corner 10A is numbered straight
through. At Road Atlanta the session's T12 was the circuit's Turn 10A, and
a driver told "brake later into T12" has to guess which corner that is.

Many tracks ship `data/sections.ini` for exactly this -- the game's own
track-description overlay reads it:

    [SECTION_10]
    IN=0.845
    OUT=0.863
    TEXT=Turn 10A

IN and OUT are lap fractions on AC's own spline, the same coordinate as
`normalizedCarPosition`, so a detected corner is named by which section its
apex falls in. No alignment, no other sim's spline. Where the file is
missing the turns keep their session numbers and nothing else changes.
"""

from __future__ import annotations

import configparser
import os
import re
from functools import lru_cache
from pathlib import Path

from .config import env

# How far outside a section an apex may fall and still take its name, as a
# fraction of the lap: about 25 m on a 4 km circuit. Section boundaries are
# drawn by hand for an overlay, and the detector's apex is the lateral-g
# peak, which on a long corner sits near an end. Road Atlanta's Turn 2 apex
# is 20 m past the OUT its sections.ini gives it.
NAME_SLACK = 0.006

ORDINALS = ("1st", "2nd", "3rd", "4th", "5th", "6th")


def _steam_roots() -> list[Path]:
    """Where Steam itself might be installed, most likely first."""
    roots = []
    for var in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        base = os.environ.get(var)
        if base:
            roots.append(Path(base) / "Steam")
    roots.append(Path.home() / ".steam" / "steam")
    roots.append(Path.home() / ".local" / "share" / "Steam")
    return roots


def _steam_libraries() -> list[Path]:
    """Every Steam library folder, from each Steam install's own list.

    libraryfolders.vdf names the libraries on other drives; AC is as likely
    to be on D: as under Program Files.
    """
    libs = []
    for root in _steam_roots():
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if root.is_dir():
            libs.append(root)
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in re.findall(r'"path"\s+"([^"]+)"', text):
            libs.append(Path(raw.replace("\\\\", "\\")))
    seen, out = set(), []
    for lib in libs:
        key = str(lib).lower()
        if key not in seen:
            seen.add(key)
            out.append(lib)
    return out


def ac_root() -> Path | None:
    """The Assetto Corsa install folder, or None if it cannot be found.

    ASSETTO_MCP_AC_ROOT wins when set. Otherwise every Steam library is
    searched for steamapps/common/assettocorsa.
    """
    explicit = env("AC_ROOT")
    if explicit:
        path = Path(explicit)
        return path if path.is_dir() else None
    for lib in _steam_libraries():
        path = lib / "steamapps" / "common" / "assettocorsa"
        if (path / "content" / "tracks").is_dir():
            return path
    return None


def sections_path(track: str | None, layout: str | None) -> Path | None:
    """The track's sections.ini, for its layout when it has several."""
    root = ac_root()
    if root is None or not track:
        return None
    base = root / "content" / "tracks" / track
    candidates = ([base / layout / "data" / "sections.ini"] if layout
                  else []) + [base / "data" / "sections.ini"]
    return next((p for p in candidates if p.is_file()), None)


def read_sections(path: Path) -> list[dict]:
    """The named sections of one sections.ini, in lap order.

    A section missing IN, OUT or TEXT, or with positions outside the lap,
    is skipped rather than guessed at.
    """
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(path.read_text(encoding="utf-8",
                                          errors="replace"))
    except (OSError, configparser.Error):
        return []
    out = []
    for name in parser.sections():
        sec = parser[name]
        try:
            lo, hi = float(sec["IN"]), float(sec["OUT"])
        except (KeyError, ValueError):
            continue
        text = (sec.get("TEXT") or "").strip()
        if text and 0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0:
            out.append({"name": text, "in_pos": lo, "out_pos": hi})
    return sorted(out, key=lambda s: s["in_pos"])


@lru_cache(maxsize=32)
def _cached_sections(path: str, mtime: float) -> tuple[dict, ...]:
    return tuple(read_sections(Path(path)))


def sections_for(track: str | None,
                 layout: str | None) -> tuple[list[dict], str | None]:
    """The circuit's named sections and the file they came from.

    Cached by file and modification time, so a pack installed while the
    server runs is picked up on the next call.
    """
    path = sections_path(track, layout)
    if path is None:
        return [], None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return [], None
    return [dict(s) for s in _cached_sections(str(path), mtime)], str(path)


def _distance(pos: float, section: dict) -> float:
    """How far outside a section a position is, 0 inside it.

    A section the start/finish line runs through has OUT below IN.
    """
    lo, hi = section["in_pos"], section["out_pos"]
    inside = lo <= pos <= hi if lo <= hi else (pos >= lo or pos <= hi)
    if inside:
        return 0.0
    return min(abs((pos - lo + 0.5) % 1.0 - 0.5),
               abs((pos - hi + 0.5) % 1.0 - 0.5))


def _span(lo: float, hi: float) -> list[tuple[float, float]]:
    """A stretch of lap as plain intervals, split where it crosses the line."""
    return [(lo, hi)] if lo <= hi else [(lo, 1.0), (0.0, hi)]


def _overlap(t: dict, section: dict) -> float:
    """How much of a turn, turn-in to exit, lies inside a section."""
    a = _span(t["entry_pos"] % 1.0, t["exit_pos"] % 1.0)
    b = _span(section["in_pos"], section["out_pos"])
    return sum(max(0.0, min(h1, h2) - max(l1, l2))
               for l1, h1 in a for l2, h2 in b)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _section_for(t: dict, sections: list[dict]) -> int | None:
    """Which section names a turn, or None.

    By overlap of the turn's own stretch of road, turn-in to exit, when the
    turn has one: the apex alone is the lateral-g peak, which on a corner
    run wide or taken in two bites can sit in the next section along.
    Road Atlanta's 10A read as 10B by its median apex, because two laps of
    the five went straight on there. A turn with no span -- the apex-only
    lists compare_laps and compare_runs carry -- falls back to the section
    its apex is in or nearest to, within NAME_SLACK.
    """
    if _is_num(t.get("entry_pos")) and _is_num(t.get("exit_pos")):
        scores = [_overlap(t, s) for s in sections]
        best = max(range(len(sections)), key=lambda j: scores[j])
        if scores[best] > 0:
            return best
    apex = t.get("apex_pos")
    if not _is_num(apex):
        return None
    best = min(range(len(sections)),
               key=lambda j: _distance(apex % 1.0, sections[j]))
    return best if _distance(apex % 1.0, sections[best]) <= NAME_SLACK else None


def name_turns(turns: list[dict], sections: list[dict]) -> list[dict]:
    """The turns, each copied with `name` from the section it lies in.

    A turn no section claims keeps no name. Where the detector found more
    than one turn inside one named section -- the Esses, a double-apex
    corner -- each gets the name with its order, "Turn 5 (2nd)", because a
    driver has to be able to tell the two apart and "Turn 5" twice would
    not.
    """
    out = [dict(t) for t in turns]
    if not sections:
        return out
    claimed: dict[int, list[int]] = {}
    for i, t in enumerate(out):
        j = _section_for(t, sections)
        if j is not None:
            claimed.setdefault(j, []).append(i)
    for j, idxs in claimed.items():
        idxs.sort(key=lambda i: out[i]["apex_pos"])
        for k, i in enumerate(idxs):
            name = sections[j]["name"]
            if len(idxs) > 1:
                name += f" ({ORDINALS[k] if k < len(ORDINALS) else k + 1})"
            out[i]["name"] = name
    return out
