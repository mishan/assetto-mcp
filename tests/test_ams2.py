"""Automobilista 2's shared memory: its layout, reading it whole, recording it.

No game here, so the page is built in memory; the layout is checked against
the size and offsets the game's own SharedMemory.h gives when compiled.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402

from assetto_mcp.sims import ams2, recording  # noqa: E402


def test_the_page_is_laid_out_as_the_game_writes_it():
    # SharedMemory.h version 14, compiled: sizeof and offsetof.
    s = ams2.SharedMemory
    assert ams2.SIZE == 20700
    assert (s.mParticipantInfo.offset, s.mSequenceNumber.offset,
            s.mCarNames.offset, s.mLaunchStage.offset) == (28, 7320, 11056,
                                                           20696)


def _page(seq: int, speed: float) -> bytes:
    page = ams2.SharedMemory(mVersion=ams2.VERSION, mSequenceNumber=seq,
                             mSpeed=speed)
    return bytes(page)


def test_a_read_that_lands_mid_write_is_taken_again():
    for reads, want in (
            # Odd: the game is writing. Then even, but changed by the
            # second look. Then even and holding.
            ([_page(7, 1), _page(8, 2), _page(10, 3), _page(10, 4),
              _page(10, 4)], 4),
            ([_page(9, 1)] * 200, None)):
        it = iter(reads)
        got = ams2.stable_copy(lambda: next(it))
        assert (None if got is None
                else ams2.SharedMemory.from_buffer_copy(got).mSpeed) == want


def test_a_recording_names_no_driver_and_says_what_was_driven():
    page = ams2.SharedMemory(mVersion=ams2.VERSION, mCarName=b"F-Ultimate",
                             mTrackLocation=b"Interlagos",
                             mTrackVariation=b"GP")
    for i, name in enumerate((b"Jo Bloggs", b"A. Rival")):
        page.mParticipantInfo[i].mIsActive = True
        page.mParticipantInfo[i].mName = name
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ams2.simrec"
        w = recording.Writer(path, {"sim": "ams2",
                                    "pages": {"shared": ams2.SIZE}})
        w.write(0.0, ams2.scrub({"shared": bytes(page)}))
        w.close()
        _, ticks = recording.read(path)
        stored = ams2.decode(next(ticks)[1])
        ticks.close()
        about = recording.info(path)
    assert [p.mName for p in stored.mParticipantInfo[:3]] == [
        b"Driver 1", b"Driver 2", b""]
    assert (about["car"], about["track"], about["track_config"]) == (
        "F-Ultimate", "Interlagos", "GP"), about


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
