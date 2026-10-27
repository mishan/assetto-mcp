"""Recording the game's shared memory, and reading it back.

A recording is what lets the readers be tested without the game, so what
comes back must be the pages exactly as they were at each moment.
"""

import ctypes
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402

from assetto_mcp import sim_info  # noqa: E402
from assetto_mcp.sims import recording  # noqa: E402


def _static(**fields) -> bytes:
    page = sim_info.SPageFileStatic(smVersion="1.7", carModel="ks_mazda_mx5_cup",
                                    track="mugello", **fields)
    return bytes(page)


def _pages(speed: float, lap: int) -> dict[str, bytes]:
    phys = sim_info.SPageFilePhysics(packetId=int(speed), speedKmh=speed)
    graph = sim_info.SPageFileGraphic(status=sim_info.AC_LIVE,
                                      completedLaps=lap)
    return {"physics": bytes(phys), "graphics": bytes(graph),
            "static": _static()}


def _write(path, ticks):
    header = {"sim": "ac", "pages": {name: ctypes.sizeof(cls)
                                     for name, cls in sim_info.PAGES.items()}}
    w = recording.Writer(path, header)
    for t, pages in ticks:
        w.write(t, pages)
    w.close()


def test_a_recording_reads_back_each_moment_as_it_was():
    ticks = [(0.0, _pages(100, 3)), (0.04, _pages(101, 3)),
             (0.08, _pages(102, 4)),
             # Nothing changed: a paused game adds nothing.
             (0.12, _pages(102, 4))]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "lap.simrec"
        _write(path, ticks)
        header, read = recording.read(path)
        assert header["sim"] == "ac" and header["format"] == 1, header
        assert list(read) == ticks[:3]
        about = recording.info(path)
        assert (about["ticks"], about["seconds"]) == (3, 0.1), about
        assert (about["car"], about["track"]) == ("ks_mazda_mx5_cup",
                                                  "mugello"), about


def test_a_recording_cut_short_reads_to_its_last_whole_record():
    ticks = [(i / 25, _pages(100 + i, i // 25)) for i in range(400)]
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "lap.simrec"
        _write(path, ticks)
        data = path.read_bytes()
        path.write_bytes(data[:len(data) * 2 // 3])
        got = list(recording.read(path)[1])
        assert 0 < len(got) < len(ticks), len(got)
        # The last tick may hold only the pages read before the cut.
        assert got[:-1] == ticks[:len(got) - 1]


def test_a_recording_leaves_the_driver_out_and_the_car_in():
    raw = _static(playerName="Jo", playerSurname="Bloggs", playerNick="JB")
    page = sim_info.from_raw({**_pages(1, 0), "static": sim_info.scrub(raw)})
    assert (page.static.playerName, page.static.playerSurname,
            page.static.playerNick) == ("", "", "")
    assert page.static.carModel == "ks_mazda_mx5_cup"
    assert page.physics.speedKmh == 1.0


def test_a_page_written_on_windows_reads_anywhere():
    # AC's static page as Windows lays it out: two 15-character versions,
    # two ints, then the car, in UTF-16.
    raw = bytearray(596)
    raw[68:68 + 32] = "ks_mazda_mx5_cup".encode("utf-16-le")
    page = sim_info.SPageFileStatic.from_buffer_copy(raw)
    assert page.carModel == "ks_mazda_mx5_cup"


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
