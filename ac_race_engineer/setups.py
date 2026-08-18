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


def _parser() -> configparser.ConfigParser:
    # Kunos' own setup.ini files use C-style '//' banner comments between
    # sections. configparser only knows ';' and '#', so parsing an unpacked
    # setup.ini -- exactly what the README tells people to drop into the
    # ranges folder -- raised ParsingError and took the tool down with it.
    cp = configparser.ConfigParser(
        strict=False, interpolation=None,
        comment_prefixes=(";", "#", "//"))
    cp.optionxform = str  # AC keys are case-sensitive-ish; preserve as-is
    return cp


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
    cp.read(path, encoding="utf-8")
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


def load_ranges(ranges_dir: Path, car: str) -> dict | None:
    """Parse an unpacked car setup.ini into {SECTION: (min, max, step)}."""
    path = ranges_dir / f"{car}.ini"
    if not path.is_file():
        return None
    cp = _parser()
    cp.read(path, encoding="utf-8")
    ranges = {}
    for section in cp.sections():
        s = cp[section]
        if "MIN" in s and "MAX" in s:
            try:
                ranges[section] = (
                    float(s["MIN"]), float(s["MAX"]),
                    float(s.get("STEP", 1) or 1),
                )
            except ValueError:
                continue
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

    ranges = load_ranges(ranges_dir, car)
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
            "No ranges file found for this car; values were written "
            "unclamped. AC silently ignores out-of-range values, so verify "
            "in the setup screen. To enable clamping, unpack the car's "
            "setup.ini via Content Manager into the ranges directory.")
    return report
