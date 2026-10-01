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
approach and apex lie in. No alignment, no other sim's spline. Where the
file is missing the turns keep their session numbers and nothing else
changes.
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
    to be on D: as under Program Files. Current Steam writes each library
    as a block with a "path" key; an older install's file can still hold
    the flat form, `"1" "D:\\SteamLibrary"`. The new form's "apps" block
    also pairs numeric keys with strings (app id to size), so a flat-form
    value only counts when it looks like a path.
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
        raws = re.findall(r'"path"\s+"([^"]+)"', text)
        raws += [v for v in re.findall(r'^\s*"\d+"\s+"([^"]+)"', text, re.M)
                 if "\\" in v or "/" in v]
        for raw in raws:
            libs.append(Path(raw.replace("\\\\", "\\")))
    seen, out = set(), []
    for lib in libs:
        key = str(lib).lower()
        if key not in seen:
            seen.add(key)
            out.append(lib)
    return out


@lru_cache(maxsize=1)
def _steam_ac_root() -> Path | None:
    """AC's folder in whichever Steam library holds it, searched once.

    Every tool that places a position asks for the track's sections, so the
    search -- several Steam installs, a vdf each, a stat per library -- would
    otherwise run on every call. An install moved while the server runs is
    found again after a restart, or straight away with ASSETTO_MCP_AC_ROOT.
    """
    for lib in _steam_libraries():
        path = lib / "steamapps" / "common" / "assettocorsa"
        if (path / "content" / "tracks").is_dir():
            return path
    return None


def ac_root() -> Path | None:
    """The Assetto Corsa install folder, or None if it cannot be found.

    ASSETTO_MCP_AC_ROOT wins when set, and is read on every call rather
    than cached, so changing it takes effect at once. Otherwise every Steam
    library is searched for steamapps/common/assettocorsa, once
    (`_steam_ac_root.cache_clear()` forgets the answer).
    """
    explicit = env("AC_ROOT")
    if explicit:
        path = Path(explicit)
        return path if path.is_dir() else None
    return _steam_ac_root()


def sections_path(track: str | None, layout: str | None) -> Path | None:
    """The track's sections.ini, for its layout when it has several.

    A layout without its own file has no names, even when the track folder
    holds one: a multi-layout track's root data/ belongs to no layout in
    particular, and its fractions laid over a different layout's spline
    would name corners confidently and wrongly. Only a track with no
    layouts reads its root data/.
    """
    root = ac_root()
    if root is None or not track:
        return None
    base = root / "content" / "tracks" / track
    path = (base / layout if layout else base) / "data" / "sections.ini"
    return path if path.is_file() else None


def read_sections(path: Path) -> list[dict]:
    """The named sections of one sections.ini, in lap order.

    A section missing IN, OUT or TEXT, or with positions outside the lap,
    is skipped rather than guessed at. The files are written by hand in
    Notepad as often as not, so a byte-order mark, an inline "; comment"
    or one stray line with no "=" is read past rather than costing every
    other name: configparser keeps the sections it did parse and only
    raises at the end.
    """
    parser = configparser.ConfigParser(strict=False, interpolation=None,
                                       inline_comment_prefixes=(";", "#"))
    try:
        parser.read_string(path.read_text(encoding="utf-8-sig",
                                          errors="replace"))
    except configparser.MissingSectionHeaderError:
        return []
    except configparser.ParsingError:
        pass
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


def _overlap(lo: float, hi: float, section: dict) -> float:
    """How much of the stretch lo to hi lies inside a section."""
    a = _span(lo % 1.0, hi % 1.0)
    b = _span(section["in_pos"], section["out_pos"])
    return sum(max(0.0, min(h1, h2) - max(l1, l2))
               for l1, h1 in a for l2, h2 in b)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _section_for(t: dict, sections: list[dict]) -> int | None:
    """Which section names a turn, or None.

    By overlap of the turn's approach -- turn-in to apex -- when the turn
    has a turn-in: that is the stretch a driver means by a corner's name.
    The apex alone is the lateral-g peak, which on a corner run wide or
    taken in two bites can sit in the next section along: Road Atlanta's
    10A read as 10B by its median apex, because two laps of the five went
    straight on there. The exit is left out because it runs on into
    whatever follows, and counting it named a turn whose apex sat in Turn 3
    after the Esses that its exit ran into, and named a flat kink "Turn 4"
    for the few meters its exit shared with Turn 4's entry.

    A section only names a turn whose apex is in it or within NAME_SLACK
    of it, or that holds at least half the approach, so a section the
    approach merely clips does not. Among those, the one holding the apex
    wins, then the one holding the most of the approach. A turn with no
    approach -- the apex-only lists compare_laps and compare_runs carry --
    falls back to the section its apex is in or nearest to, within
    NAME_SLACK.
    """
    apex = t.get("apex_pos")
    if not _is_num(apex):
        return None
    apex %= 1.0
    entry = t.get("entry_pos")
    if _is_num(entry):
        approach = (apex - entry) % 1.0
        best, best_key = None, None
        for j, s in enumerate(sections):
            ov = _overlap(entry, apex, s)
            if ov <= 0:
                continue
            d = _distance(apex, s)
            if d > NAME_SLACK and ov < approach / 2:
                continue
            key = (d == 0.0, ov)
            if best_key is None or key > best_key:
                best, best_key = j, key
        if best is not None:
            return best
    best = min(range(len(sections)),
               key=lambda j: _distance(apex, sections[j]))
    return best if _distance(apex, sections[best]) <= NAME_SLACK else None


def claim(turns: list[dict],
          sections: list[dict]) -> tuple[list[dict], list[int | None]]:
    """The turns named (as name_turns), and which section claimed each.

    The index is what track_corners lists a section's turns by. Matching
    by the name instead goes wrong twice over: two sections can share a
    name, and a section called "Turn 1" would claim "Turn 1 (La Source)".
    """
    out = [dict(t) for t in turns]
    if not sections:
        return out, [None] * len(out)
    owner = [_section_for(t, sections) for t in out]
    # Sections sharing a name are one name to a driver, so their turns are
    # numbered together, in lap order: by the section's place in the lap,
    # then by how far past its IN the apex is, which keeps a section across
    # the start/finish line in order where the raw position would not.
    by_name: dict[str, list[int]] = {}
    for i, j in enumerate(owner):
        if j is not None:
            by_name.setdefault(sections[j]["name"], []).append(i)
    for name, idxs in by_name.items():
        idxs.sort(key=lambda i: (
            sections[owner[i]]["in_pos"], owner[i],
            (out[i]["apex_pos"] - sections[owner[i]]["in_pos"]) % 1.0))
        for k, i in enumerate(idxs):
            label = name
            if len(idxs) > 1:
                label += f" ({ORDINALS[k] if k < len(ORDINALS) else k + 1})"
            out[i]["name"] = label
    return out, owner


def name_turns(turns: list[dict], sections: list[dict]) -> list[dict]:
    """The turns, each copied with `name` from the section it lies in.

    A turn no section claims keeps no name. Where the detector found more
    than one turn inside one named section -- the Esses, a double-apex
    corner -- each gets the name with its order, "Turn 5 (2nd)", because a
    driver has to be able to tell the two apart and "Turn 5" twice would
    not. Two sections with the same name, a circuit with two "Chicane"s,
    are numbered as one: their turns run "Chicane (1st)" to "Chicane
    (4th)" in lap order, so no two turns share a name.
    """
    return claim(turns, sections)[0]
