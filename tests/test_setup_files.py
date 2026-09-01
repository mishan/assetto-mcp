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
    """Asking for more than the car allows gets the car's maximum.

    This used to write 9. MIN=1 MAX=10 STEP=2 is not a whole number of
    steps, so a grid anchored at the minimum stops at 9 and the maximum is
    not on it -- asking for the stiffest bar the car has quietly returned
    one notch softer. AC itself clamps at the end, so 10 is reachable and is
    the right answer.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "carx", "[ARB_FRONT]\nMIN=1\nMAX=10\nSTEP=2\n")
        docs = d / "docs"
        docs.mkdir()
        report = setups.write_setup(
            docs, rng, car="carx", track="mugello", name="v1",
            values={"ARB_FRONT": 99})
        assert report["written"]["ARB_FRONT"] == 10, report
        assert "ARB_FRONT" in report["clamped"], report
        print("  out-of-range value clamped to the maximum:",
              report["clamped"])


# --- what the setup screen can actually reach ---------------------------
#
# Driver-observed, and the reason any of this is here: the RSS Formula 4's
# rear wheel rate is MIN=53 MAX=88 STEP=17. Counting up on the spinner gives
# 53, 70, 87, 88. Counting back down from 88 gives 71, 54, 53. The set is
# not a grid, it is two ladders that miss each other, because AC adds and
# subtracts from where it is and clamps at the ends.


def test_both_ladders_are_reachable():
    assert setups.legal_values(53, 88, 17) == [53, 54, 70, 71, 87, 88]
    print(f"  rear wheel rate: {setups.legal_values(53, 88, 17)}")


def test_a_value_only_on_the_descending_ladder_is_written_exactly():
    """54 is a real rear spring rate, and the old code called it 53.

    The failure was quiet in the worst way: it reported the substitution as
    clamping, so a 2% softer spring looked like the request being tidied up.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "rss", "[SPRING_RATE_LR]\nMIN=53\nMAX=88\nSTEP=17\n")
        docs = d / "docs"
        docs.mkdir()
        report = setups.write_setup(
            docs, rng, car="rss", track="suzuka", name="v1",
            values={"SPRING_RATE_LR": 54})
        assert report["written"]["SPRING_RATE_LR"] == 54, report
        assert "SPRING_RATE_LR" not in report["clamped"], report
        print("  54 written as 54, not reported as clamped")


def test_the_anti_roll_bar_has_the_same_two_ladders():
    """The setting this car is run at sits on the descending ladder, which
    is how the problem surfaced in the first place.

    126607 is 182107 - 6*9250. The ascending ladder from 52608 passes
    through 126608 instead, one N/m away, and a one-step reduction that
    should have landed on 117357 was snapped to 117358.
    """
    vals = setups.legal_values(52608, 182107, 9250)
    for v in (126607, 126608, 117357, 117358, 52608, 182107):
        assert v in vals, v
    assert setups.snap(117357, 52608, 182107, 9250) == 117357
    print(f"  {len(vals)} reachable bar settings, both ladders present")


def test_a_request_between_rungs_is_answered_the_same_way_twice():
    """Ties go low, deterministically, rather than by set iteration order."""
    assert setups.snap(61.5, 53, 88, 17) == 54
    assert setups.snap(63, 53, 88, 17) == 70
    # 62 is exactly 8 from both 54 and 70. Low wins, every time it is asked.
    assert [setups.snap(62.0, 53, 88, 17) for _ in range(5)] == [54] * 5


def test_a_degenerate_range_does_not_raise():
    assert setups.legal_values(5, 5, 1) == [5]
    assert setups.legal_values(10, 1, 1) == []
    assert setups.legal_values(1, 10, 0) == []
    # With no reachable set to consult, snap still has to clamp.
    assert setups.snap(99, 1, 10, 0) == 10


def test_step_of_zero_does_not_become_a_divide_trap():
    with tempfile.TemporaryDirectory() as tmp:
        rng = _ranges(Path(tmp), "carx", "[ARB_FRONT]\nMIN=1\nMAX=10\nSTEP=0\n")
        assert setups.load_ranges(rng, "carx")["ARB_FRONT"][2] == 1.0
        print("  STEP=0 falls back to 1")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
