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
import re
from pathlib import Path

_NAME_RE = re.compile(r"^[\w][\w \-.]{0,60}$")


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


def list_setups(ac_docs_dir: Path, car: str, track: str) -> list[str]:
    d = setups_root(ac_docs_dir) / car / track
    if not d.is_dir():
        # AC uses plain track dir for default layout, "track/layout" dirs
        # otherwise; try a loose match so callers don't have to know.
        parent = setups_root(ac_docs_dir) / car
        if parent.is_dir():
            hits = [p for p in parent.iterdir()
                    if p.is_dir() and p.name.startswith(track)]
            if len(hits) == 1:
                d = hits[0]
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.ini"))


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


def write_setup(ac_docs_dir: Path, ranges_dir: Path, car: str, track: str,
                name: str, values: dict, base_setup: str | None = None) -> dict:
    """Write a setup file, optionally starting from an existing one.

    values: {SECTION: number} for the fields to set/override.
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
    try:
        ranges = load_ranges(ranges_dir, car)
    except SetupParseError as e:
        ranges, ranges_problem = None, str(e)

    report = {"written": {}, "clamped": {}, "unknown_sections": [],
              "ranges_available": ranges is not None}

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
