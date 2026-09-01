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

from assetto_mcp import setups  # noqa: E402

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


def test_a_setup_with_nothing_in_it_is_refused_before_anything_is_written():
    """And refused *before* the file is opened, which is the load-bearing part.

    The check used to run after the write. That was survivable while a write
    could only create a new file, and became destructive the moment
    overwrite=True existed: a request naming only sections the car does not
    have truncated a real setup to a [CAR] header and then reported
    "NOTHING WAS WRITTEN" about the wreckage.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "carx", "[ARB_FRONT]\nMIN=1\nMAX=10\nSTEP=1\n")
        docs = d / "docs"
        docs.mkdir()
        good = setups.write_setup(docs, rng, car="carx", track="mugello",
                                  name="good", values={"ARB_FRONT": 5})
        before = Path(good["path"]).read_text()

        try:
            setups.write_setup(docs, rng, car="carx", track="mugello",
                               name="good", values={"NOT_A_REAL_SECTION": 5},
                               overwrite=True)
            raise AssertionError("expected EmptySetupError")
        except setups.EmptySetupError as e:
            assert "NOT_A_REAL_SECTION" in str(e), e
        assert Path(good["path"]).read_text() == before, \
            "a valid setup was truncated by a request that wrote nothing"
        print("  refused; the existing setup is untouched")


def test_a_base_setup_with_no_valid_overrides_still_writes():
    # Not empty -- the base's values are in it. Worth writing, worth saying
    # that none of the requested changes landed.
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "carx", "[ARB_FRONT]\nMIN=1\nMAX=10\nSTEP=1\n")
        docs = d / "docs"
        docs.mkdir()
        setups.write_setup(docs, rng, car="carx", track="mugello",
                           name="base", values={"ARB_FRONT": 5})
        report = setups.write_setup(
            docs, rng, car="carx", track="mugello", name="derived",
            values={"NOT_A_REAL_SECTION": 5}, base_setup="base")
        assert report["written"] == {}, report
        assert "ARB_FRONT" in Path(report["path"]).read_text()
        assert "None of the requested values" in report.get("warning", ""), report


def test_a_refusal_leaves_no_trace_on_disk():
    """Not even an empty directory.

    setup_dir falls back to prefix-matching a track folder only while the
    exact name does not exist, so creating an empty 'mugello' beside the real
    'mugello_osrw' breaks that fallback permanently -- and a refused write is
    exactly when someone has guessed the track id wrong.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = d / "ranges"
        rng.mkdir()
        docs = d / "docs"
        real = docs / "setups" / "carx" / "mugello_osrw"
        real.mkdir(parents=True)
        (real / "real.ini").write_text("[CAR]\nMODEL=carx\n")

        assert setups.list_setups(docs, "carx", "mugello") == ["real"]
        try:
            setups.write_setup(docs, rng, car="carx", track="mugello",
                               name="v1", values={"ARB_FRONT": 5})
            raise AssertionError("expected UnclampedWriteError")
        except setups.UnclampedWriteError:
            pass
        assert not (docs / "setups" / "carx" / "mugello").exists(), \
            "a refusal created the directory and poisoned the loose match"
        assert setups.list_setups(docs, "carx", "mugello") == ["real"]
        print("  refusal left the filesystem alone")


def test_an_unusable_ranges_file_refuses_the_write_and_says_why():
    """A silently-ignored setup costs a whole run to notice.

    This used to write unclamped with a warning. The warning was true and
    useless: the file loads, the garage shows no complaint, and the first
    hint that values were discarded is a run that feels exactly like the one
    before it. Refusing costs one round trip, and the message has to say
    which of the two causes it is, because the fix differs.
    """
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
        try:
            setups.write_setup(docs, rng, car="carx", track="mugello",
                               name="v1", values={"PRESSURE_LF": 26})
            raise AssertionError("expected UnclampedWriteError")
        except setups.UnclampedWriteError as e:
            assert "unusable" in str(e), e
            assert "allow_unclamped" in str(e), e
        assert not list(docs.rglob("v1.ini")), \
            "refusing must not leave a half-written file behind"
        print("  unusable ranges -> refused, nothing written")


def test_no_ranges_at_all_refuses_too_and_names_the_other_cause():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = d / "ranges"
        rng.mkdir()
        docs = d / "docs"
        docs.mkdir()
        try:
            setups.write_setup(docs, rng, car="carx", track="mugello",
                               name="v1", values={"PRESSURE_LF": 26})
            raise AssertionError("expected UnclampedWriteError")
        except setups.UnclampedWriteError as e:
            assert "No setup ranges are known" in str(e), e


def test_allow_unclamped_writes_and_keeps_the_warning():
    # The escape hatch has to work: editing setups with the game closed and
    # no ranges installed is a real thing to want to do.
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = d / "ranges"
        rng.mkdir()
        docs = d / "docs"
        docs.mkdir()
        report = setups.write_setup(
            docs, rng, car="carx", track="mugello", name="v1",
            values={"PRESSURE_LF": 26}, allow_unclamped=True)
        assert report["ranges_available"] is False, report
        assert "unclamped" in report.get("warning", ""), report
        assert report["written"] == {"PRESSURE_LF": 26}, report
        assert Path(report["path"]).exists()
        print("  allow_unclamped -> written, warning retained")


def test_an_existing_setup_is_not_replaced_without_being_asked():
    """A name that already exists may be something built by hand.

    From in here a driver's own setup and one this tool wrote a minute ago
    are the same file, and the garage gives no hint that what is under the
    old name is now something else.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "carx", KUNOS_STYLE)
        docs = d / "docs"
        docs.mkdir()
        first = setups.write_setup(
            docs, rng, car="carx", track="mugello", name="claude_v1",
            values={"PRESSURE_LF": 26})
        before = Path(first["path"]).read_text()

        try:
            setups.write_setup(docs, rng, car="carx", track="mugello",
                               name="claude_v1", values={"PRESSURE_LF": 30})
            raise AssertionError("expected SetupExistsError")
        except setups.SetupExistsError as e:
            # The suggested name is the useful part: it is almost always
            # what was meant, and it keeps the A/B intact.
            assert "claude_v2" in str(e), e
        assert Path(first["path"]).read_text() == before, "file was touched"
        print("  refused, and suggested claude_v2")


def test_overwrite_backs_the_old_setup_up_first():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "carx", KUNOS_STYLE)
        docs = d / "docs"
        docs.mkdir()
        first = setups.write_setup(
            docs, rng, car="carx", track="mugello", name="claude_v1",
            values={"PRESSURE_LF": 26})
        before = Path(first["path"]).read_text()

        report = setups.write_setup(
            docs, rng, car="carx", track="mugello", name="claude_v1",
            values={"PRESSURE_LF": 30}, overwrite=True)
        assert report["written"] == {"PRESSURE_LF": 30}, report
        assert "backup" in report, report
        assert Path(report["backup"]).read_text() == before, \
            "the backup must hold what was there before, not after"
        assert "30" in Path(report["path"]).read_text()
        print("  overwritten, previous file kept at", Path(report["backup"]).name)


def test_no_name_is_suggested_when_the_suggestion_would_be_rejected():
    # _NAME_RE caps at 61 characters and _free_name appends _vN, so a long
    # enough stem produces a suggestion the very next call refuses -- with a
    # message about unsupported characters, when the problem is length.
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "carx", KUNOS_STYLE)
        docs = d / "docs"
        docs.mkdir()
        long_name = "y" * 59
        assert setups._NAME_RE.match(long_name)
        setups.write_setup(docs, rng, car="carx", track="mugello",
                           name=long_name, values={"PRESSURE_LF": 26})
        try:
            setups.write_setup(docs, rng, car="carx", track="mugello",
                               name=long_name, values={"PRESSURE_LF": 30})
            raise AssertionError("expected SetupExistsError")
        except setups.SetupExistsError as e:
            assert "Pick another name" in str(e), e
            assert "_v2" not in str(e), e


def test_backups_do_not_pile_up_in_the_drivers_setup_folder():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "carx", KUNOS_STYLE)
        docs = d / "docs"
        docs.mkdir()
        report = setups.write_setup(docs, rng, car="carx", track="mugello",
                                    name="claude_v1",
                                    values={"PRESSURE_LF": 26})
        folder = Path(report["path"]).parent
        for psi in range(20, 28):
            report = setups.write_setup(
                docs, rng, car="carx", track="mugello", name="claude_v1",
                values={"PRESSURE_LF": psi}, overwrite=True)
        baks = list(folder.glob("claude_v1.ini.bak-*"))
        assert len(baks) == 5, [b.name for b in baks]
        assert report["backups_kept"] == 5, report
        # And AC's own listing must not see them as setups.
        assert setups.list_setups(docs, "carx", "mugello") == ["claude_v1"]
        print("  8 overwrites -> 5 backups kept, none visible as a setup")


def test_a_suggested_name_skips_over_names_already_taken():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rng = _ranges(d, "carx", KUNOS_STYLE)
        docs = d / "docs"
        docs.mkdir()
        for n in ("claude_v1", "claude_v2", "claude_v3"):
            setups.write_setup(docs, rng, car="carx", track="mugello",
                               name=n, values={"PRESSURE_LF": 26})
        try:
            setups.write_setup(docs, rng, car="carx", track="mugello",
                               name="claude_v1", values={"PRESSURE_LF": 30})
            raise AssertionError("expected SetupExistsError")
        except setups.SetupExistsError as e:
            assert "claude_v4" in str(e), e


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
    assert setups.snap(5, 5, 5, 1) == 5


def test_snapping_agrees_with_enumerating_across_the_whole_range():
    """snap() is arithmetic and legal_values() enumerates, so they can
    drift apart silently. They must not: the enumerated set is the
    definition, and snap is only an optimisation of searching it.

    The optimisation is not cosmetic. Enumerating cost 80ms and 100,001
    floats for a 1-unit step over a wide range, per field, on every write --
    and nothing bounds what the game reports for a car nobody has loaded
    yet.
    """
    cases = [(53, 88, 17), (52608, 182107, 9250), (0, 10, 1), (0, 3.5, 0.1),
             (-25, 25, 5), (1, 100, 7), (5, 5, 1), (0, 1, 0.25)]
    for lo, hi, step in cases:
        options = setups.legal_values(lo, hi, step)
        assert options, (lo, hi, step)
        span = hi - lo
        for i in range(201):
            v = lo - 0.1 * span + (1.2 * span) * i / 200.0
            want = min(options, key=lambda x: (abs(x - min(max(v, lo), hi)), x))
            got = setups.snap(v, lo, hi, step)
            assert abs(got - want) < 1e-6, (lo, hi, step, v, got, want)
    print(f"  {len(cases)} ranges x 201 requests: arithmetic == enumerated")


def test_a_huge_ladder_is_not_materialised_to_snap_one_value():
    """A 1-unit step over 100,000 units is a real thing for a game-reported
    range, and building it to pick one value is pure waste.

    Asserted by asking for a range that could not be materialised at all,
    rather than by timing one that merely would be slow. A wall-clock bound
    is a statement about the runner: a loaded CI box fails it while the
    algorithm is perfect, and a fast one passes it while the algorithm is
    quadratic. A range with 10^18 rungs cannot be enumerated on any machine,
    so returning the right answer promptly is the proof, and it is the same
    proof everywhere.
    """
    # The realistic case first: 100,001 rungs, which enumerating did build.
    assert setups.snap(126607.4, 100000, 200000, 1) == 126607
    assert setups.legal_values(100000, 200000, 1) == [], (
        "the enumerating helper must decline, not allocate")

    # And the one that settles it. Materialising this is not slow, it is
    # impossible; anything that returns has done arithmetic.
    lo, hi, step = 0.0, 1e9, 1e-9
    assert abs(setups.snap(123456.789, lo, hi, step) - 123456.789) < 1e-6
    assert abs(setups.snap(-5, lo, hi, step) - lo) < 1e-9
    assert abs(setups.snap(2e9, lo, hi, step) - hi) < 1e-9
    print("  snapped inside a ladder of 10^18 rungs without building it")


def test_step_of_zero_does_not_become_a_divide_trap():
    with tempfile.TemporaryDirectory() as tmp:
        rng = _ranges(Path(tmp), "carx", "[ARB_FRONT]\nMIN=1\nMAX=10\nSTEP=0\n")
        assert setups.load_ranges(rng, "carx")["ARB_FRONT"][2] == 1.0
        print("  STEP=0 falls back to 1")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
