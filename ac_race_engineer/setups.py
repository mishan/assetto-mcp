"""Read and write AC setup files.

Setups live at:  <Documents>/Assetto Corsa/setups/<car>/<track>/<name>.ini
Format is INI with one VALUE per section:

    [PRESSURE_LF]
    VALUE=26

IMPORTANT: AC silently ignores values outside the ranges the car defines in
its setup.ini (packed inside data.acd). To clamp safely, unpack the car's
setup.ini with Content Manager and drop it at:

    <data_dir>/ranges/<car>.ini

where <data_dir> is wherever the server keeps its DB. If no ranges file is
present, writes go through unclamped with a warning in the result.
"""

import configparser
import math
import re
from pathlib import Path

_NAME_RE = re.compile(r"^[\w][\w \-.]{0,60}$")

# What fraction of the live setup a saved file must actually cover before
# agreeing on all of it counts as identification. A file holding only
# [FUEL] agrees with every setup carrying the same fuel load; calling that a
# match is the same failure as the brake-bias fingerprint this replaced --
# a confident answer from channels that cannot separate the candidates.
#
# A share rather than a count, because the count that means "thorough"
# depends on the car: 2 of 2 adjustable entries is everything there is to
# know, while 2 of 21 is a coincidence.
MIN_IDENTIFY_COVERAGE = 0.5


class SetupParseError(ValueError):
    """A setup or ranges file could not be read.

    Subclasses ValueError so the MCP tools' existing except clauses turn it
    into a message instead of an unhandled traceback.
    """


def _parser() -> configparser.ConfigParser:
    # Kunos' own setup.ini files are hand-maintained and hit most of
    # configparser's strictness in turn:
    #   //  C-style banner comments between sections
    #   ;   trailing comments on a value ("MIN=15 ; psi")
    #   KEY with no '=' at all
    #   duplicate keys within a section
    # Each of these raised out of the tool call rather than being handled.
    cp = configparser.ConfigParser(
        strict=False,                       # tolerate duplicate keys/sections
        interpolation=None,                 # '%' in a value is not a format
        allow_no_value=True,                # bare flags
        comment_prefixes=(";", "#", "//"),
        inline_comment_prefixes=(";", "#"),
    )
    cp.optionxform = str  # AC keys are case-sensitive-ish; preserve as-is
    return cp


def _read_ini(cp: configparser.ConfigParser, path: Path) -> None:
    """Parse `path`, tolerating the encodings AC files show up in.

    utf-8-sig first: a BOM makes the first section header unparseable and
    configparser reports it as a missing section header, which reads like a
    corrupt file rather than an encoding problem. cp1252 second: Kunos and
    mod authors write accented names in comments.
    """
    last: Exception | None = None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with open(path, encoding=encoding) as f:
                cp.read_file(f, source=str(path))
            return
        except UnicodeDecodeError as e:
            last = e
            continue
        except configparser.MissingSectionHeaderError as e:
            raise SetupParseError(
                f"{path.name}: content before the first [SECTION] header "
                f"(line {e.lineno}). Is this really an AC setup file?") from e
        except configparser.Error as e:
            raise SetupParseError(f"{path.name}: {e}") from e
    raise SetupParseError(
        f"{path.name}: could not decode as UTF-8 or Windows-1252 ({last})")


def setups_root(ac_docs_dir: Path) -> Path:
    return ac_docs_dir / "setups"


def setup_dir(ac_docs_dir: Path, car: str, track: str,
              loose: bool = True) -> Path | None:
    """The folder holding this car's setups for this track, if there is one.

    Returns the real directory rather than the name it was asked for. A
    loose match can land on a folder called something else entirely, and
    every caller then has to read from *that* -- listing one folder and
    reading from another is how identification once searched a directory it
    could not open and reported finding nothing.
    """
    d = setups_root(ac_docs_dir) / car / track
    if d.is_dir():
        return d
    if not loose:
        return None
    # AC uses plain track dir for default layout, "track/layout" dirs
    # otherwise; try a loose match so callers don't have to know.
    parent = setups_root(ac_docs_dir) / car
    if parent.is_dir():
        hits = [p for p in parent.iterdir()
                if p.is_dir() and p.name.startswith(track)]
        if len(hits) == 1:
            return hits[0]
    return None


def list_setups(ac_docs_dir: Path, car: str, track: str) -> list[str]:
    d = setup_dir(ac_docs_dir, car, track)
    return sorted(p.stem for p in d.glob("*.ini")) if d else []


def _track_candidates(track: str) -> list[str]:
    """A track id, then the same id with trailing suffixes stripped.

    AC ids come in two shapes: `<track>_<layout>` for what the original game
    shipped ("mugello_osrw"), and `<vendor>_<track>[_<layout>]` for
    everything since ("ks_nordschleife_endurance"). Taking the text before
    the FIRST underscore reads the second shape as its vendor tag -- "ks",
    which is not a folder in any install -- so that fallback only ever
    resolved the first shape, and the first shape is the one circuit it was
    developed against.

    Stripping the LAST suffix instead walks ks_barcelona_layout_moto ->
    ks_barcelona_layout -> ks_barcelona -> ks, reaching the real folder in a
    step or two whichever shape the id has.

    The bare vendor tag is left at the end of the list rather than filtered
    out, because _resolve_track_dir matches every shortened candidate
    EXACTLY. "ks" as a prefix would match whichever ks_ folder that car
    happens to have setups in -- reading another circuit's setups and
    reporting a confident match -- while "ks" as a folder name exists
    nowhere, so an exact match can never accept it. Screening by a list of
    known vendor tags would need maintaining for every mod pack; requiring
    the folder to actually be called that does not, and it protects
    "mugello" and "spa", which are single segments and real folders.
    """
    parts = [p for p in track.split("_") if p]
    return ["_".join(parts[:i]) for i in range(len(parts), 0, -1)]


def _resolve_track_dir(ac_docs_dir: Path, car: str,
                       *tracks: str) -> tuple[str, list[str]]:
    """Pick the setup folder for the first track id that has one.

    Takes several ids because a session reports the layout two ways and
    neither is reliably the folder name: `trackConfiguration` is the whole
    id at some circuits ("mugello_osrw") and the bare layout at others
    ("layout_moto"), while `track` is the folder for the latter and too
    generic for the former.
    """
    tried: list[str] = []
    for track in tracks:
        for i, cand in enumerate(_track_candidates(track or "")):
            if cand in tried:
                continue
            tried.append(cand)
            # The full id gets the loose, starts-with match -- that is what
            # finds a "track/layout" folder from a plain track name. The
            # shortened ones do not: see _track_candidates.
            d = setup_dir(ac_docs_dir, car, cand, loose=(i == 0))
            if d is None:
                continue
            names = sorted(p.stem for p in d.glob("*.ini"))
            if names:
                return d.name, names
    return (tracks[0] if tracks else ""), []


def read_setup(ac_docs_dir: Path, car: str, track: str, name: str) -> dict:
    path = setups_root(ac_docs_dir) / car / track / f"{name}.ini"
    if not path.is_file():
        raise FileNotFoundError(f"no setup at {path}")
    cp = _parser()
    _read_ini(cp, path)
    out = {}
    for section in cp.sections():
        if "VALUE" in cp[section]:
            raw = cp[section]["VALUE"]
            try:
                out[section] = float(raw) if "." in raw else int(raw)
            except ValueError:
                out[section] = raw
        else:  # e.g. [CAR] MODEL=...
            out[section] = dict(cp[section])
    return out


def _num(raw, default: float | None = None) -> float | None:
    """First number in a value, or default. None if there isn't one."""
    if raw is None:
        return default
    m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(raw))
    return float(m.group()) if m else default


def resolve_ranges(ranges_dir: Path, car: str,
                   game_ranges: dict | None) -> tuple[dict | None, str]:
    """Ranges to clamp against, and where they came from.

    The game's own answer wins. ac.getSetupSpinners() reports each entry's
    legal min/max/step for the car actually loaded, which is authoritative,
    arrives without unpacking data.acd, and works for encrypted paid mods
    where unpacking may not be possible at all. The hand-installed ranges
    file stays as a fallback for when the in-game app isn't running.
    """
    if game_ranges:
        return game_ranges, "game"
    from_file = load_ranges(ranges_dir, car)
    return from_file, ("file" if from_file else "none")


def identify_setup(ac_docs_dir: Path, car: str, track: str,
                   values: dict, track_folder: str | None = None) -> dict:
    """Which saved setup matches the values currently on the car.

    Matching is on content, not on a fingerprint of a couple of observable
    channels. Shared memory exposes only brake bias and fuel, which cannot
    separate setups differing in ARB or camber -- precisely what gets
    changed between runs. The values reported by the setup menu cover every
    entry, so a match is exact.

    Never guesses: several matches are reported as several, and the caller
    is expected to ask rather than pick one.

    track is whatever the session calls the layout; track_folder is the
    plain track name when the session knows it separately. Which of the two
    is the folder on disk varies by circuit, so both are tried.
    """
    if not values:
        return {"match": None, "candidates": [], "compared": 0,
                "reason": "no live setup values; is the in-game app running?"}

    # Sessions report a layout id ('mugello_osrw', 'ks_silverstone_gp')
    # while setups are filed under the track folder. The resolved folder has
    # to be used for reading as well as listing: fixing only the listing
    # left every read_setup() raising FileNotFoundError and being skipped,
    # so identification still found nothing while looking like it had
    # searched.
    track_dir, names = _resolve_track_dir(ac_docs_dir, car, track,
                                          track_folder or "")
    # At least half of what the game reports, and never fewer than one --
    # a car with a single adjustable entry is identified by that entry.
    needed = max(1, math.ceil(MIN_IDENTIFY_COVERAGE * len(values)))
    exact, near, thin = [], [], []
    for name in names:
        try:
            saved = read_setup(ac_docs_dir, car, track_dir, name)
        except (FileNotFoundError, ValueError):
            continue
        saved = {k: v for k, v in saved.items()
                 if isinstance(v, (int, float))}
        shared = [k for k in saved if k in values]
        if not shared:
            continue
        diffs = [k for k in shared if abs(saved[k] - values[k]) > 0.5]
        if diffs:
            if len(diffs) <= 2:
                near.append({"name": name, "differs_in": sorted(diffs)[:4]})
            continue
        # Agreeing on everything compared means little when almost nothing
        # was compared. A saved file holding only [FUEL] agrees with every
        # setup that has the same fuel load, and would otherwise be reported
        # as an exact match at full confidence.
        if len(shared) < needed:
            thin.append({"name": name, "compared": len(shared)})
        else:
            exact.append({"name": name, "compared": len(shared)})

    out = {"candidates": [e["name"] for e in exact],
           "compared": min((e["compared"] for e in exact), default=0),
           "near_misses": sorted(near, key=lambda n: len(n["differs_in"]))[:3],
           # Always reported, and reported even when nothing matched. A
           # fallback that quietly reads a different track's setups looks
           # exactly like one that read the right folder, and the answer it
           # gives is a setup name with no way to tell where it came from.
           "track_dir": track_dir or None}
    if track_dir and track_dir != track:
        out["track_dir_note"] = (
            f"the session reports the layout as {track!r}, which is not a "
            f"setup folder; read {track_dir!r} instead")
    if thin:
        out["too_few_fields_to_judge"] = sorted(
            thin, key=lambda t: -t["compared"])[:3]
    if len(exact) == 1:
        out["match"] = exact[0]["name"]
    else:
        out["match"] = None
        if exact:
            out["reason"] = (f"{len(exact)} saved setups have identical "
                             f"values; they cannot be told apart by content")
        elif thin:
            out["reason"] = (
                f"the only agreeing setups cover fewer than {needed} of the "
                f"car's {len(values)} live entries, which is not enough to "
                f"identify one -- agreeing on a handful of shared fields "
                f"says little about the rest")
        elif not names:
            # Distinct from "nothing matched": one says the saved setups
            # disagree with the car, the other that there were none to read.
            # They call for opposite next steps, and answering both with
            # "no saved setup matches the car" is what made a broken folder
            # lookup look like a genuine result.
            out["reason"] = (
                f"no setup folder for {car!r} at {track!r} -- nothing was "
                f"there to compare against, which is not the same as nothing "
                f"matching")
        else:
            out["reason"] = "no saved setup matches the car"
    return out


def load_ranges(ranges_dir: Path, car: str) -> dict | None:
    """Parse an unpacked car setup.ini into {SECTION: (min, max, step)}.

    None means "no ranges file for this car" -- write_setup then warns and
    writes unclamped. An empty dict must never be returned in its place: it
    reads as "ranges available, and this car allows nothing", which made
    write_setup drop every requested value and report success while writing
    a setup file containing no settings at all.
    """
    path = ranges_dir / f"{car}.ini"
    if not path.is_file():
        return None
    cp = _parser()
    _read_ini(cp, path)
    ranges = {}
    skipped = []
    for section in cp.sections():
        s = cp[section]
        if "MIN" not in s or "MAX" not in s:
            continue
        lo, hi = _num(s["MIN"]), _num(s["MAX"])
        if lo is None or hi is None:
            skipped.append(section)
            continue
        step = _num(s.get("STEP"), 1.0)
        ranges[section] = (lo, hi, step if step and step > 0 else 1.0)

    if not ranges:
        raise SetupParseError(
            f"{path.name}: no usable MIN/MAX sections found"
            + (f" ({len(skipped)} section(s) had unreadable numbers)"
               if skipped else "")
            + ". Clamping cannot be applied from this file; move or fix it "
              "and the write will go through unclamped with a warning.")
    return ranges


def _displays_as(stored: float, conv: dict | None) -> str | None:
    """How a stored value reads on AC's setup screen, or None if unknown.

    Deliberately a string, not a converted number. The stored value is what
    the file must contain, so replacing it would be wrong; this exists so a
    report cannot say "wrote 20" when the driver asked for 20 mm and the car
    took 20 clicks.

    None means only that nothing is known about this entry's display. It is
    not the answer for an entry the game describes as needing no conversion
    at all -- that entry has a display, it is just the stored number.
    """
    if not conv:
        return None
    mult = conv.get("display_multiplier")
    units = (conv.get("units") or "").strip()
    clicks = conv.get("show_clicks_mode")

    # show_clicks_mode is not a multiplier: it says the number *is* an index
    # into the car's positions, so there is no unit to convert to.
    if clicks:
        return f"{stored:g} (click index, mode {clicks})"
    if mult in (None, 0, 1):
        # 0 and 1 both mean "shown as stored"; 0 is how some entries report
        # having no conversion, and coalescing it to a multiplier would
        # scale the value to nothing.
        #
        # No units either just means the screen shows a bare number, which is
        # a complete answer rather than a missing one. Returning None dropped
        # the entry out of `displays_as` altogether -- so the report that
        # promises to say what every written value means went quiet on
        # exactly the entries where the answer was simplest.
        return f"{stored:g}{(' ' + units) if units else ''}"
    shown = stored * mult
    return f"{shown:g}{(' ' + units) if units else ''} (stored {stored:g})"


def write_setup(ac_docs_dir: Path, ranges_dir: Path, car: str, track: str,
                name: str, values: dict, base_setup: str | None = None,
                game_ranges: dict | None = None,
                display: dict | None = None) -> dict:
    """Write a setup file, optionally starting from an existing one.

    values: {SECTION: number} for the fields to set/override.
    game_ranges: ranges as reported by ac.getSetupSpinners(), which take
    precedence over any hand-installed ranges file.
    display: {SECTION: {units, display_multiplier, show_clicks_mode}} so the
    report can say what each written number means on the setup screen.

    Setup files store raw values, and the number on the screen is often a
    different one: camber is stored in tenths of a degree, ride height as a
    click index. Nothing here converts -- writing the stored value is
    correct -- but a report saying "wrote 20" without saying whether that is
    20 mm or 20 clicks is how a setup that looks fine and isn't gets
    written. Every written entry carries `displays_as`.

    Returns a report of what was written, clamped, or dropped.
    """
    if not _NAME_RE.match(name):
        raise ValueError("setup name contains unsupported characters")

    d = setups_root(ac_docs_dir) / car / track
    d.mkdir(parents=True, exist_ok=True)

    merged: dict = {}
    if base_setup:
        merged = {k: v for k, v in
                  read_setup(ac_docs_dir, car, track, base_setup).items()
                  if not isinstance(v, dict)}

    # A broken ranges file must not block the write -- it only costs us
    # clamping -- but it has to be said out loud, because the whole point of
    # clamping is that AC silently ignores out-of-range values.
    ranges_problem = None
    ranges_source = "none"
    try:
        ranges, ranges_source = resolve_ranges(ranges_dir, car, game_ranges)
    except SetupParseError as e:
        ranges, ranges_problem = None, str(e)

    report = {"written": {}, "clamped": {}, "unknown_sections": [],
              "ranges_available": ranges is not None,
              "ranges_source": ranges_source}

    for section, value in values.items():
        if not isinstance(value, (int, float)):
            report["unknown_sections"].append(section)
            continue
        if ranges is not None:
            if section not in ranges:
                # Not in the car's setup.ini: AC would silently ignore it.
                report["unknown_sections"].append(section)
                continue
            lo, hi, step = ranges[section]
            clamped = min(max(value, lo), hi)
            # Snap to the car's step grid so the in-game UI shows it exactly.
            if step > 0:
                clamped = lo + round((clamped - lo) / step) * step
            if clamped != value:
                report["clamped"][section] = {"requested": value,
                                              "written": clamped}
            value = clamped
        merged[section] = value
        report["written"][section] = value
        shown = _displays_as(value, (display or {}).get(section))
        if shown:
            report.setdefault("displays_as", {})[section] = shown

    cp = _parser()
    cp["CAR"] = {"MODEL": car}
    for section, value in merged.items():
        if section == "CAR":
            continue
        if isinstance(value, float) and value == int(value):
            value = int(value)
        cp[section] = {"VALUE": str(value)}

    path = d / f"{name}.ini"
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f, space_around_delimiters=False)

    report["path"] = str(path)
    if ranges is None:
        report["warning"] = (
            (f"Ranges file unusable: {ranges_problem} " if ranges_problem
             else "No ranges file found for this car; ")
            + "values were written unclamped. AC silently ignores "
              "out-of-range values, so verify in the setup screen. To enable "
              "clamping, unpack the car's setup.ini via Content Manager into "
              "the ranges directory.")
    if not report["written"]:
        # Every requested value was dropped, so the file on disk has a [CAR]
        # header and nothing else. Loading it in the garage would appear to
        # work and change nothing.
        report["warning"] = (
            "NOTHING WAS WRITTEN - the setup file contains no settings. "
            + ("None of the requested sections exist in this car's ranges "
               "file: " + ", ".join(report["unknown_sections"])
               if report["unknown_sections"] else "No values were supplied.")
            + " Do not load this setup; fix the section names and rewrite.")
    return report
