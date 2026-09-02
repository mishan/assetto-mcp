"""Read and write AC setup files.

Setups live at:  <Documents>/Assetto Corsa/setups/<car>/<track>/<name>.ini
Format is INI with one VALUE per section:

    [PRESSURE_LF]
    VALUE=26

IMPORTANT: AC silently ignores values outside the ranges the car defines in
its setup.ini (packed inside data.acd). To clamp safely, unpack the car's
setup.ini with Content Manager and drop it at:

    <data_dir>/ranges/<car>.ini

where <data_dir> is wherever the server keeps its DB -- or just run the
in-game app, which reports every car's ranges without any file at all.

With no ranges from either source, write_setup *refuses* rather than writing
values nothing could check. allow_unclamped=True overrides it.
"""

import configparser
import math
import re
import shutil
import time
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

    Its message says what is wrong with the file and nothing about what the
    caller should do -- callers quote it in more than one context, and at
    least one of them (write_setup with allow_unclamped) has already gone
    ahead by the time it is printed.
    """


class SetupExistsError(ValueError):
    """Refused to replace an existing setup file.

    Its own type, rather than a bare ValueError, so the tool layer can offer
    the one-flag way past it without pattern-matching on message text.
    """


class UnclampedWriteError(ValueError):
    """Refused to write values nothing could check against the car's limits."""


class EmptySetupError(ValueError):
    """Refused to write a setup file with no settings in it.

    A [CAR] header and nothing else loads in the garage without complaint
    and changes nothing, which is indistinguishable from a setup the car
    declined -- the failure this whole module is shaped around.
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

    On failure the first element is a folder that exists and turned out to
    hold no .ini files, or "" if no folder was found at all -- never the id
    it was asked for. Returning the requested id claimed a folder had been
    read that was never even opened: identify_setup then reported
    track_dir: 'ks_nowhere_special' for a directory that does not exist,
    indistinguishable from a real folder that happened to be empty. The two
    want opposite next steps -- correct the track id, or go and save a
    setup -- so they are answered differently.
    """
    tried: list[str] = []
    empty = ""
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
            # Keep the first empty one: the search carries on in case a
            # later candidate has setups in it, but if none does, this is
            # the folder that genuinely exists and the caller should hear
            # about it rather than about a name nothing was found under.
            empty = empty or d.name
    return empty, []


def read_setup(ac_docs_dir: Path, car: str, track: str, name: str) -> dict:
    """One setup file, resolved the same way list_setups resolves it.

    Through setup_dir rather than the literal path, so a track prefix --
    "mugello" for "mugello_osrw" -- reads the folder that list_setups and
    write_setup use. Reading the literal path meant a name that listing had
    just offered came back as "no setup at ...", and a base_setup that
    plainly existed could not be found.
    """
    d = setup_dir(ac_docs_dir, car, track)
    path = (d or setups_root(ac_docs_dir) / car / track) / f"{name}.ini"
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
    if names and track_dir and track_dir != track:
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
            #
            # And the two ways of having nothing to read are themselves
            # distinct, which is why track_dir is "" for one and the folder
            # for the other: an empty folder means save a setup, no folder
            # at all means the track id never resolved to one.
            if track_dir:
                out["reason"] = (
                    f"the setup folder for {car!r} at {track_dir!r} holds no "
                    f"setups -- nothing was there to compare against, which "
                    f"is not the same as nothing matching")
            else:
                out["reason"] = (
                    f"no setup folder for {car!r} at {track!r} -- nothing was "
                    f"there to compare against, which is not the same as "
                    f"nothing matching")
        else:
            out["reason"] = "no saved setup matches the car"
    return out


def load_ranges(ranges_dir: Path, car: str) -> dict | None:
    """Parse an unpacked car setup.ini into {SECTION: (min, max, step)}.

    None means "no ranges file for this car" -- write_setup then refuses,
    unless the caller passes allow_unclamped. An empty dict must never be
    returned in its place: it reads as "ranges available, and this car allows
    nothing", which made write_setup drop every requested value and report
    success while writing a setup file containing no settings at all.
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
        # Descriptive, not prescriptive: this same string is quoted both by
        # the refusal in write_setup and by the warning it prints when the
        # caller asked to write anyway. Saying "writes are refused" here
        # made the second one contradict the write that had just happened.
        raise SetupParseError(
            f"{path.name}: no usable MIN/MAX sections found"
            + (f" ({len(skipped)} section(s) had unreadable numbers)"
               if skipped else "")
            + ". No min, max or step can be read from it.")
    return ranges


# Two stored values closer together than this, relative to their own
# magnitude, are the same spinner position. Deliberately not exact equality:
# a continuous entry can report stored values that differ only in the last
# bit, and dividing by that difference gave a slope of 2e14.
_STORED_EPS = 1e-9


def fit_display(observations: list[tuple], multiplier: float | None = None
                ) -> dict | None:
    """A linear stored -> displayed mapping from what the driver read.

    observations: (stored, displayed) pairs. Returns
    {slope, offset, basis, points} or None if nothing can be fitted.

    Two distinct stored values give both slope and offset outright: two
    points define a line, and that covers a negated axis, a wrong scale and
    a wrong zero at once.

    One observation cannot separate a scale error from an offset error, so
    it borrows the game's own `display_multiplier` as the slope and fits
    only the offset. That is not a guess dressed up -- it is the NSX toe
    case exactly, where the scale was right and the zero was not (stored 10
    reads 0.00 degrees), and it costs the driver one number instead of two.
    `basis` says which happened so a reader can tell a fitted line from a
    half-fitted one.

    Fits from the two most extreme stored values rather than a least
    squares over all of them. The relationship is exactly linear -- it is a
    multiply and an add inside the game -- so extra points are confirmation,
    not signal, and the widest pair gives the least error from rounding on
    the screen.
    """
    pts = set()
    for s, d in observations:
        try:
            fs, fd = float(s), float(d)
        except (TypeError, ValueError):
            continue        # a NULL or a string in the table is not a point
        if math.isfinite(fs) and math.isfinite(fd):
            pts.add((fs, fd))
    pts = sorted(pts)
    if not pts:
        return None

    (s0, d0), (s1, d1) = pts[0], pts[-1]
    # Relative, not exact. Two readings whose stored values differ only by
    # floating-point noise are one reading, and dividing by that difference
    # produced a slope of 2e14 and a screen value of 1.5e15 reported as
    # `observed`. Continuous entries (step 0) are supported deliberately, so
    # this is reachable rather than theoretical.
    spread = abs(s1 - s0)
    if spread > _STORED_EPS * max(1.0, abs(s0), abs(s1)):
        if d1 == d0:
            # Both ends read the same. That is a screen that rounds, or the
            # same number written down twice -- not a horizontal line. A
            # slope of 0 would report every value of the entry as one
            # number, with `observed` beside it.
            return _offset_only(s0, d0, multiplier, pts,
                                why="both readings showed the same number, "
                                    "so they cannot define a scale")
        slope = (d1 - d0) / (s1 - s0)
        out = {"slope": slope, "offset": d0 - slope * s0,
               "basis": "two observations", "points": len(pts),
               "fitted_over": [s0, s1]}
        # Residual against the fitted line. `points` used to be published as
        # "observations: 3" while a wildly contradictory middle reading was
        # silently discarded -- confirmation counted without being checked.
        worst = max(((s, d, abs(d - (slope * s + out["offset"])))
                     for s, d in pts), key=lambda t: t[2], default=None)
        if worst and worst[2] > _residual_tolerance(pts):
            out["disagreement"] = {
                "stored": worst[0], "read": worst[1],
                "predicted": round(slope * worst[0] + out["offset"], 4),
                "note": "a reading disagrees with the line the others make. "
                        "One of them was misread; forget_display_value "
                        "clears them all so they can be taken again."}
        return out

    # One point, or several at the same spinner position.
    return _offset_only(s0, d0, multiplier, pts,
                        why="only one spinner position has been read")


def _residual_tolerance(pts: list) -> float:
    """How far a reading may sit off the line before it is a disagreement.

    Proportional to the displayed span, because a screen showing degrees to
    two places and one showing millimetres to one place round differently.
    Never zero, so an exactly-linear set does not flag itself.
    """
    span = abs(pts[-1][1] - pts[0][1])
    return max(span * 0.02, 1e-9)


def _offset_only(s0: float, d0: float, multiplier, pts: list,
                 why: str) -> dict | None:
    """Fit the zero against the game's scale, or admit there is no fit.

    None when the game gives no usable multiplier -- which is exactly the
    click-index entries, the ones with no known display at all. Returning a
    mapping there would mean inventing the scale, and returning success
    without one was worse: the tool reported the reading accepted, the entry
    stayed "unknown", and the advice was to do the thing that had just been
    done.
    """
    if multiplier in (None, 0) or multiplier is False:
        return None
    mult = float(multiplier)
    return {"slope": mult, "offset": d0 - mult * s0,
            "basis": "one observation, scale from the game", "points": len(pts),
            "why_partial": why, "fitted_over": [s0, s0]}


def apply_display(stored: float, mapping: dict) -> float:
    return mapping["slope"] * stored + mapping["offset"]


def describe_display(conv: dict | None) -> dict:
    """What is known about one entry's screen display, without a value.

    For listings, where there is no particular stored number to convert but
    a reader still has to be told whether anything here knows what the
    screen says.
    """
    if not conv:
        return {"source": "unknown",
                "reason": "no spinner data and no readings for this entry"}
    out: dict = {}
    if conv.get("note"):
        out["note"] = conv["note"]
    if conv.get("units"):
        out["units"] = conv["units"]

    mapping = conv.get("mapping")
    if mapping:
        partial = mapping["points"] < 2 or "why_partial" in mapping
        out.update(source="observed_offset" if partial else "observed",
                   basis=mapping["basis"],
                   observations=mapping["points"],
                   slope=round(mapping["slope"], 6),
                   offset=round(mapping["offset"], 6),
                   formula=f"screen = {mapping['slope']:g} * stored"
                           f" + {mapping['offset']:g}")
        if mapping.get("fitted_over"):
            out["fitted_over"] = mapping["fitted_over"]
        if mapping.get("disagreement"):
            out["disagreement"] = mapping["disagreement"]
        return out
    if conv.get("show_clicks_mode"):
        out.update(
            source="unknown",
            reason=f"the game reports a click index (mode "
                   f"{conv['show_clicks_mode']}), which says the stored "
                   f"number is a position rather than a value",
            how_to_fix="record_display_value with what the driver reads")
        return out
    if conv.get("from_game", True) is False:
        out.update(
            source="unknown",
            reason="the in-game app has never reported this entry, so a "
                   "missing multiplier means nobody said rather than no "
                   "conversion",
            how_to_fix="open the setup screen with the app running, then "
                       "record_display_value")
        return out
    mult = conv.get("display_multiplier")
    if mult in (None, 0, 1):
        out.update(source="stored", formula="screen = stored")
        return out
    out.update(source="game", display_multiplier=mult,
               formula=f"screen = {mult:g} * stored",
               caveat="the game's own multiplier, which has been wrong "
                      "before about sign, scale and zero")
    return out


def _displays_as(stored: float, conv: dict | None) -> dict | None:
    """How a stored value reads on AC's setup screen.

    Returns a dict rather than the bare string it used to. The string said
    things like "0 (click index, mode 2)", which is the tool admitting it
    does not know what the screen will say, in a format that reads like an
    answer. It got repeated back to the driver as one five times in an
    evening.

    Always: `source`, and `stored`. Then, depending on `source`:

      shown       the value as text, or None when source is "unknown"
      value       the same number, rounded, when there is one
      units       when the entry has any
      note        when something non-numeric was recorded about it
      basis       how an observed mapping was fitted
      caveat      why a non-observed answer may be wrong
      reason      why nothing is known, when source is "unknown"
      how_to_fix  what would make it known
      disagreement / extrapolated  when the readings warrant it

    `source` is the load-bearing field:
      observed         a line fitted from two readings off the actual screen
      observed_offset  the zero came from a reading, the scale from the game
      game             the game's display_multiplier, applied as documented
      stored           the game says the screen shows the stored number
      unknown          nothing here knows; do not state a displayed value

    `observed_offset` is separate from `observed` on purpose. It borrows the
    game's multiplier for the scale -- the same number that has been wrong
    about scale -- so it is not the same claim, and `source` is the field
    every docstring tells a reader to key off.

    None only when there is no entry at all.
    """
    if not conv:
        return None
    units = (conv.get("units") or "").strip()
    clicks = conv.get("show_clicks_mode")
    mult = conv.get("display_multiplier")
    mapping = conv.get("mapping")
    note = conv.get("note")

    def out(**kw):
        if units:
            kw["units"] = units
        if note:
            kw["note"] = note
        return kw

    if mapping:
        shown = apply_display(stored, mapping)
        partial = mapping["points"] < 2 or "why_partial" in mapping
        res = out(shown=f"{shown:g}{(' ' + units) if units else ''}",
                  value=round(shown, 4),
                  source="observed_offset" if partial else "observed",
                  basis=mapping["basis"], stored=stored)
        if partial:
            res["caveat"] = (
                "the zero is measured, the scale is the game's -- which has "
                "been wrong about scale. A reading at a different spinner "
                "position would settle it.")
        if mapping.get("disagreement"):
            res["disagreement"] = mapping["disagreement"]
        lo, hi = mapping.get("fitted_over", (None, None))
        if lo is not None and not (lo - _STORED_EPS <= stored
                                   <= hi + _STORED_EPS):
            res["extrapolated"] = (
                f"outside the range these readings covered ({lo:g} to "
                f"{hi:g}); the further out, the more any rounding on the "
                f"screen is multiplied")
        return res

    # show_clicks_mode says the number IS an index into the car's positions,
    # so there is no unit and no multiplier to apply. Nothing here can turn
    # it into what the screen shows, and saying so plainly is the whole
    # point -- this is where the guessing used to happen.
    if clicks:
        return out(
            shown=None, source="unknown", stored=stored,
            reason=f"the game reports this as a click index (mode {clicks}), "
                   f"not a value. What the screen shows for it is not "
                   f"derivable from anything here.",
            how_to_fix="ask the driver what the setup screen reads for this "
                       "entry and record it with record_display_value")

    # Defaults to True: a caller handing us a conv dict directly is handing
    # us the game's own fields. db.setup_display sets it explicitly either
    # way, so only an entry it KNOWS the game never described lands here.
    if conv.get("from_game", True) is False:
        # Here only because somebody left a note or a reading on it. A NULL
        # multiplier then means "nobody said", not "no conversion", and
        # reporting it as `stored` is a confident answer built from an
        # absence.
        return out(shown=None, source="unknown", stored=stored,
                   reason="the in-game app has never reported this entry, so "
                          "nothing here knows how it is displayed",
                   how_to_fix="open the setup screen with the app running, "
                              "then record_display_value")

    if mult in (None, 0, 1):
        # 0 and 1 both mean "shown as stored"; 0 is how some entries report
        # having no conversion, and coalescing it to a multiplier would
        # scale the value to nothing.
        return out(shown=f"{stored:g}{(' ' + units) if units else ''}",
                   value=stored, source="stored", stored=stored)

    shown = stored * mult
    return out(shown=f"{shown:g}{(' ' + units) if units else ''}",
               value=round(shown, 4), source="game", stored=stored,
               caveat="from the game's display_multiplier, which has been "
                      "wrong before (a negated axis, a non-zero zero). "
                      "record_display_value replaces it with a reading.")


def legal_values(lo: float, hi: float, step: float) -> list[float]:
    """Every value this entry can actually reach on the setup screen.

    AC does not snap to a grid. It adds or subtracts step from wherever the
    spinner already is and clamps at the ends, so the reachable set depends
    on which way you arrived and is not `lo + n*step`.

    Driver-observed on the RSS Formula 4's rear wheel rate, lo 53, hi 88,
    step 17. Counting up: 53, 70, 87, then 87+17 clamped to 88. Counting
    back down from 88: 71, 54, then 53. Six reachable values, of which a
    grid anchored at lo can express three.

    This is not pedantry. 54 is a rate this car is actually run at, and it
    sits only on the descending ladder. Asked to write 54, the old snapping
    returned 53 and reported it as clamping -- the correct value, declared
    illegal and quietly replaced by a 2% softer spring. The same offset
    ladders exist on the front anti-roll bar: 126607 is on the descending
    one and 126608 on the ascending one, and a one-step reduction landed on
    117358 when 117357 was available and exact.

    Enumerating is for reporting and for tests. snap() does not use it: a
    1-unit step over a wide range -- which nothing rules out, since these
    numbers come from whatever the game reports for whatever car is loaded
    -- is a hundred thousand values per field per write, and there is no
    reason to build them to pick one. Returns [] above LEGAL_VALUES_MAX
    rungs, which means "too many to list", not "none".
    """
    n = _rungs(lo, hi, step)
    if n is None or n > LEGAL_VALUES_MAX:
        return []
    out = {round(lo, 6), round(hi, 6)}
    for k in range(n + 1):
        out.add(round(lo + k * step, 6))
        out.add(round(hi - k * step, 6))
    return sorted(x for x in out if lo - _EPS <= x <= hi + _EPS)


# Above this the ladders are not worth materialising, and nothing needs them
# to be: legal_values is a reporting helper, and snap works arithmetically.
LEGAL_VALUES_MAX = 10000
_EPS = 1e-9


def _rungs(lo: float, hi: float, step: float) -> int | None:
    """How many whole steps fit between lo and hi. None if the range is
    not a range at all."""
    if step <= 0 or hi < lo:
        return None
    return int(math.floor((hi - lo) / step + _EPS))


def snap(value: float, lo: float, hi: float, step: float) -> float:
    """The reachable value nearest what was asked for.

    Both ladders are arithmetic, so the nearest rung on each is computed
    rather than searched: at most six candidates, whatever the range. The
    enumerating version cost 80ms and 100,001 floats on a 1-unit step, per
    field, on every write.

    Ties go to the lower value, so a request exactly between two rungs is
    answered the same way twice rather than depending on set ordering.
    """
    n = _rungs(lo, hi, step)
    if n is None:
        return min(max(value, lo), hi)
    value = min(max(value, lo), hi)
    out = {round(lo, 6), round(hi, 6)}
    for base, sign in ((lo, 1.0), (hi, -1.0)):
        exact = sign * (value - base) / step
        for k in (math.floor(exact), math.floor(exact) + 1):
            k = min(max(int(k), 0), n)
            out.add(round(base + sign * k * step, 6))
    return min((x for x in out if lo - _EPS <= x <= hi + _EPS),
               key=lambda x: (abs(x - value), x))


def _free_name(d: Path, name: str) -> str | None:
    """A name like the one asked for that nothing is using yet.

    None when there isn't one worth suggesting -- which in practice means the
    stem is already so long that appending _vN would exceed what _NAME_RE
    accepts. Suggesting a name the very next call rejects, with a message
    about unsupported *characters* when the problem is length, is worse than
    suggesting nothing.
    """
    stem = re.sub(r"_v(\d+)$", "", name)
    m = re.search(r"_v(\d+)$", name)
    n = int(m.group(1)) + 1 if m else 2
    while (d / f"{stem}_v{n}.ini").exists():
        n += 1
    candidate = f"{stem}_v{n}"
    return candidate if _NAME_RE.match(candidate) else None


_KEEP_BACKUPS = 5


def _backup_age(path: Path) -> tuple:
    """Sort key for a backup filename: (timestamp, collision number).

    The collision number has to be compared as a *number*. Sorting the
    whole name as text puts "-9" after "-10", so once ten backups landed in
    the same second the pruner deleted the newest ones and kept the oldest
    -- the precise opposite of the job. Ten writes inside one second is
    unlikely by hand and trivial in a loop.
    """
    # The timestamp contains a hyphen of its own (yyyymmdd-HHMMSS), so the
    # collision number is the THIRD segment when there is one. Partitioning
    # on the first hyphen instead read "20260101-120000" as stamp
    # "20260101" collision 120000, which sorted the very first backup of a
    # second as the newest of them.
    # rsplit, not split: a setup name may itself contain ".ini.bak-" --
    # dots and hyphens are both legal in _NAME_RE -- and splitting on the
    # FIRST marker then parsed part of the driver's own name as a timestamp,
    # so every backup of that setup fell to the fallback age and pruning
    # stopped keeping the newest.
    parts = path.name.rsplit(".ini.bak-", 1)[-1].split("-")
    try:
        if len(parts) == 2:
            return (f"{parts[0]}-{parts[1]}", 0)
        if len(parts) == 3:
            return (f"{parts[0]}-{parts[1]}", int(parts[2]))
    except ValueError:
        pass
    # Something else matched the glob. Sort it oldest so it is pruned
    # before any backup we actually wrote.
    return ("", -1)


def _prune_backups(d: Path, name: str) -> int:
    """Keep the newest few backups of one setup. Returns how many remain.

    These sit in the driver's own Assetto Corsa setup folder, which they
    open in Explorer, so an unbounded pile of them is a mess in a place that
    is not ours to make messy. Ordered by the timestamp in the name rather
    than by mtime, which would not survive the driver copying the folder.
    """
    baks = sorted(d.glob(f"{name}.ini.bak-*"), key=_backup_age, reverse=True)
    for old in baks[_KEEP_BACKUPS:]:
        try:
            old.unlink()
        except OSError:
            pass  # A backup we cannot delete is not worth failing a write for.
    return min(len(baks), _KEEP_BACKUPS)


def free_name_for(ac_docs_dir: Path, car: str, track: str,
                  name: str) -> str | None:
    """The name write_setup would suggest, for callers reporting a refusal."""
    d = setup_dir(ac_docs_dir, car, track)
    return _free_name(d, name) if d else None


def write_setup(ac_docs_dir: Path, ranges_dir: Path, car: str, track: str,
                name: str, values: dict, base_setup: str | None = None,
                game_ranges: dict | None = None,
                display: dict | None = None,
                overwrite: bool = False,
                allow_unclamped: bool = False) -> dict:
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

    Two refusals, both because the failure they prevent is silent:

    overwrite: an existing file of this name is not replaced. A setup the
    driver built by hand looks identical from here to one this tool wrote a
    minute ago, and the garage gives no hint that the thing under the old
    name is now something else. Pass True to replace it; the previous file
    is copied to <name>.ini.bak-<stamp> first, so the answer to "put it
    back" is never "you can't".

    allow_unclamped: with no ranges for the car -- no in-game app running
    and no ranges file installed -- nothing here knows the legal min, max or
    step, and AC *silently ignores* values outside them. Writing anyway
    produces a setup that loads without complaint and does not do what it
    says, which costs a whole run to notice. Pass True to write regardless.

    Returns a report of what was written, clamped, or dropped.
    """
    if not _NAME_RE.match(name):
        raise ValueError(
            "setup name must be 1-61 characters of letters, digits, spaces, "
            "hyphens, dots or underscores, starting with a letter or digit")

    # Write into the folder every *reader* will look in, not into the
    # literal name the caller passed. setup_dir resolves a track prefix to
    # the real directory -- "mugello" to "mugello_osrw" -- and taking the
    # literal path instead did two bad things at once: the new setup landed
    # in a folder AC never reads, and creating that folder stopped the
    # prefix fallback firing at all, so `list_setups(car, "mugello")` went
    # from listing the driver's own setups to listing only ours. Their files
    # were still on disk and invisible to every tool here.
    #
    # None means no folder for this track yet, which is ordinary for the
    # first setup at a new circuit -- use the name as given.
    #
    # Deliberately no mkdir yet either. A refusal must leave the filesystem
    # exactly as it found it, for the same reason: an empty directory
    # created on the way to failing breaks the fallback permanently, and a
    # refused write is the likeliest moment for a track id to be wrong.
    d = setup_dir(ac_docs_dir, car, track) or (
        setups_root(ac_docs_dir) / car / track)
    path = d / f"{name}.ini"
    if path.exists() and not overwrite:
        free = _free_name(d, name)
        raise SetupExistsError(
            f"'{name}' already exists for {car} at {track} ({path}). "
            + (f"Write it as '{free}' instead" if free else "Pick another name")
            + ", or ask the driver whether to replace it -- overwrite=true "
              "backs the old file up alongside first. Do not assume it was "
              "ours; it may be one they built by hand.")

    merged: dict = {}
    if base_setup:
        merged = {k: v for k, v in
                  read_setup(ac_docs_dir, car, track, base_setup).items()
                  if not isinstance(v, dict)}

    # A broken ranges file does not have to stop us knowing *why* we have no
    # ranges, and the distinction matters in the refusal below: "your ranges
    # file is malformed" and "you have no ranges file" need different fixes.
    ranges_problem = None
    ranges_source = "none"
    try:
        ranges, ranges_source = resolve_ranges(ranges_dir, car, game_ranges)
    except SetupParseError as e:
        ranges, ranges_problem = None, str(e)

    if ranges is None and not allow_unclamped:
        raise UnclampedWriteError(
            (f"The ranges file for {car} is unusable ({ranges_problem}). "
             if ranges_problem else
             f"No setup ranges are known for {car}. ")
            + "Values cannot be checked against the car's legal min/max/step, "
              "and AC silently ignores anything outside them -- the setup "
              "would load, appear fine, and not do what it says. "
              "Start Assetto Corsa with the in-game app enabled and open the "
              "setup screen once (the app reports ranges automatically), or "
              "unpack the car's setup.ini into the ranges directory. "
              "Pass allow_unclamped=true to write without the check.")

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
            # Snap to a value the setup screen can actually reach, which is
            # not the same as a grid anchored at the minimum -- see
            # legal_values for why the car has two offset ladders.
            clamped = snap(min(max(value, lo), hi), lo, hi, step)
            if clamped != value:
                report["clamped"][section] = {"requested": value,
                                              "written": clamped}
            value = clamped
        merged[section] = value
        report["written"][section] = value
        shown = _displays_as(value, (display or {}).get(section))
        if shown:
            report.setdefault("displays_as", {})[section] = shown
            if shown.get("source") == "unknown":
                report.setdefault("display_unknown", []).append(section)

    # Refuse an empty result *before* touching the file. This check used to
    # run after the write, which was survivable when the write created a new
    # file and became destructive the moment overwrite=True existed: a
    # request naming only sections the car does not have truncated a real
    # setup to a [CAR] header and then said "NOTHING WAS WRITTEN". The
    # backup made it recoverable; not doing it is better.
    #
    # merged carries base_setup's values, so "nothing was written" is about
    # this request, not about the file -- a base plus zero valid overrides
    # would still produce a usable setup, and is allowed through.
    if not report["written"] and not merged:
        raise EmptySetupError(
            "Nothing would be written - the setup file would contain no "
            "settings at all, and loading it in the garage would appear to "
            "work and change nothing. "
            + ("None of the requested sections exist for this car: "
               + ", ".join(report["unknown_sections"]) + ". Check the names "
               "against setup_ranges."
               if report["unknown_sections"] else "No values were supplied."))

    cp = _parser()
    cp["CAR"] = {"MODEL": car}
    for section, value in merged.items():
        if section == "CAR":
            continue
        if isinstance(value, float) and value == int(value):
            value = int(value)
        cp[section] = {"VALUE": str(value)}

    # Snapshot before replacing. Only reached with overwrite=True, since the
    # existence check above is what stands between here and a lost setup.
    # Re-tested rather than assumed: overwrite=True with no existing file is
    # a perfectly ordinary call.
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = d / f"{name}.ini.bak-{stamp}"
        n = 1
        while backup.exists():
            backup = d / f"{name}.ini.bak-{stamp}-{n}"
            n += 1
        shutil.copy2(path, backup)
        report["replaced"] = str(path)
        report["backup"] = str(backup)
        report["backups_kept"] = _prune_backups(d, name)

    d.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f, space_around_delimiters=False)

    report["path"] = str(path)
    if ranges is None:
        report["warning"] = (
            (f"Ranges file unusable: {ranges_problem} " if ranges_problem
             else "No ranges file found for this car; ")
            + "values were written unclamped at your request. AC silently "
              "ignores out-of-range values, so verify every one of them in "
              "the setup screen before driving.")
    elif not report["written"]:
        # Reachable only with a base_setup: the file is usable, but nothing
        # the caller asked for is in it, and saying so beats a silent
        # "written: {}" that reads like success.
        report["warning"] = (
            "None of the requested values were written - the file is a copy "
            "of " + str(base_setup) + ". Sections not valid for this car: "
            + ", ".join(report["unknown_sections"] or ["(none supplied)"]))
    if report.get("display_unknown"):
        report["display_note"] = (
            "The values were written correctly, but for "
            + ", ".join(report["display_unknown"]) + " nothing here knows "
            "what the setup SCREEN will show. Do not state one: say it is "
            "unknown, and offer to record what the driver reads with "
            "record_display_value so it is known next time.")
    return report
