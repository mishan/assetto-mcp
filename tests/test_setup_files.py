"""Reading and writing AC setup files, and the ranges that clamp them.

The stakes here are quiet failure. AC silently ignores out-of-range setup
values, so a setup that writes nothing looks exactly like a setup that
writes something the car declined -- both present as "I loaded it and
nothing changed". Anything this module cannot honour has to say so loudly.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402

from ac_race_engineer import setups  # noqa: E402

# A ranges file in the shape Kunos actually ships: C-style banner comments
# between sections, trailing comments on values, and a valueless key.
KUNOS_STYLE = """// ---------------------------------------------
// Front suspension
// ---------------------------------------------
[PRESSURE_LF]
MIN=15 ; psi
MAX=35 ; psi
STEP=1
SHOW_CLICKS
[ARB_FRONT]
MIN=1   // clicks
MAX=10  // clicks
STEP=1
"""


def _ranges(dirpath: Path, car: str, text: str) -> Path:
    rng = dirpath / "ranges"
    rng.mkdir(exist_ok=True)
    (rng / f"{car}.ini").write_text(text, encoding="utf-8")
    return rng


def test_kunos_comment_styles_parse():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "carx", KUNOS_STYLE)
        parsed = setups.load_ranges(rng, "carx")
        assert parsed is not None
        assert parsed["PRESSURE_LF"] == (15.0, 35.0, 1.0), parsed
        assert parsed["ARB_FRONT"] == (1.0, 10.0, 1.0), parsed
        print("  // banners, inline comments and valueless keys all parse")


def test_a_setup_is_never_silently_written_empty():
    """The failure this whole module is shaped around.

    Trailing comments made every MIN/MAX fail float(), load_ranges returned
    {} rather than None, so write_setup read "ranges available, this car
    allows nothing" -- dropped every value, reported success, and wrote a
    file containing only [CAR]. The driver loads it and nothing changes.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "carx", KUNOS_STYLE)
        docs = d / "docs"
        docs.mkdir()
        report = setups.write_setup(
            docs, rng, car="carx", track="mugello", name="claude_v2",
            values={"PRESSURE_LF": 26, "ARB_FRONT": 5})
        assert report["written"] == {"PRESSURE_LF": 26, "ARB_FRONT": 5}, report
        assert not report["unknown_sections"], report
        written = Path(report["path"]).read_text()
        assert "PRESSURE_LF" in written and "26" in written, written
        print("  values survive to the file:", report["written"])


def test_a_setup_with_nothing_in_it_is_flagged_in_capitals():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "carx", "[ARB_FRONT]\nMIN=1\nMAX=10\nSTEP=1\n")
        docs = d / "docs"
        docs.mkdir()
        report = setups.write_setup(
            docs, rng, car="carx", track="mugello", name="v1",
            values={"NOT_A_REAL_SECTION": 5})
        assert report["written"] == {}, report
        assert "NOTHING WAS WRITTEN" in report.get("warning", ""), report
        print("  empty setup flagged rather than reported as success")


def test_an_unusable_ranges_file_warns_but_still_writes():
    """Losing clamping is survivable; losing the write silently is not."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "carx", "[JUNK]\nNOTHING=useful\n")
        docs = d / "docs"
        docs.mkdir()
        try:
            setups.load_ranges(rng, "carx")
            raise AssertionError("expected SetupParseError")
        except setups.SetupParseError:
            pass
        report = setups.write_setup(
            docs, rng, car="carx", track="mugello", name="v1",
            values={"PRESSURE_LF": 26})
        assert report["ranges_available"] is False, report
        assert "unclamped" in report.get("warning", ""), report
        assert report["written"] == {"PRESSURE_LF": 26}, report
        print("  unusable ranges -> unclamped write plus a warning")


def test_inline_comment_stripped_from_a_setup_value():
    """read_setup must return a number, not the string '26 ; psi'.

    load_ranges survives an inline comment either way because it pulls the
    first number out of the field, so this is the test that actually pins
    inline_comment_prefixes.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        setup_dir = d / "setups" / "carx" / "mugello"
        setup_dir.mkdir(parents=True)
        (setup_dir / "v1.ini").write_text(
            "[CAR]\nMODEL=carx\n"
            "[PRESSURE_LF]\nVALUE=26 ; psi\n"
            "[ARB_FRONT]\nVALUE=5\n")
        out = setups.read_setup(d, "carx", "mugello", "v1")
        assert out["PRESSURE_LF"] == 26, repr(out["PRESSURE_LF"])
        assert out["ARB_FRONT"] == 5, repr(out["ARB_FRONT"])
        print("  inline comments stripped from values")


def test_encodings_and_malformed_files_report_cleanly():
    with tempfile.TemporaryDirectory() as tmp:
        rng = Path(tmp) / "ranges"
        rng.mkdir()

        (rng / "bom.ini").write_bytes(
            "[ARB_FRONT]\nMIN=1\nMAX=10\nSTEP=1\nSHOW_CLICKS\n"
            .encode("utf-8-sig"))
        assert setups.load_ranges(rng, "bom")["ARB_FRONT"] == (1.0, 10.0, 1.0)

        (rng / "latin.ini").write_bytes(
            "; réglages\n[ARB_FRONT]\nMIN=1\nMAX=10\nSTEP=1\n"
            .encode("cp1252"))
        assert setups.load_ranges(rng, "latin")["ARB_FRONT"] == \
            (1.0, 10.0, 1.0)

        (rng / "bad.ini").write_text("junk line\n[X]\nMIN=1\nMAX=2\n")
        try:
            setups.load_ranges(rng, "bad")
            raise AssertionError("expected SetupParseError")
        except setups.SetupParseError as e:
            # The MCP tools already catch ValueError, so this reaches the
            # model as a message rather than an unhandled traceback.
            assert isinstance(e, ValueError)
        print("  BOM and cp1252 handled; junk reports rather than raises")


def test_values_are_clamped_and_snapped_to_the_cars_grid():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "carx", "[ARB_FRONT]\nMIN=1\nMAX=10\nSTEP=2\n")
        docs = d / "docs"
        docs.mkdir()
        report = setups.write_setup(
            docs, rng, car="carx", track="mugello", name="v1",
            values={"ARB_FRONT": 99})
        assert report["written"]["ARB_FRONT"] == 9, report   # clamped+snapped
        assert "ARB_FRONT" in report["clamped"], report
        print("  out-of-range value clamped and snapped:", report["clamped"])


def test_step_of_zero_does_not_become_a_divide_trap():
    with tempfile.TemporaryDirectory() as tmp:
        rng = _ranges(Path(tmp), "carx", "[ARB_FRONT]\nMIN=1\nMAX=10\nSTEP=0\n")
        assert setups.load_ranges(rng, "carx")["ARB_FRONT"][2] == 1.0
        print("  STEP=0 falls back to 1")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
