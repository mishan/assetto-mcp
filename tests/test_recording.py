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


def _two_laps(path):
    """Half a lap from the pits, then a 20-second lap, at 25 Hz."""
    hz, lap_s = 25, 20.0
    ticks, done, last_ms = [], 0, 0
    for i in range(int(hz * (lap_s * 1.5 + 2))):
        t = i / hz
        pos = (0.5 + t / lap_s) % 1.0
        laps = int(0.5 + t / lap_s)
        if laps != done:
            done, last_ms = laps, 0 if laps == 1 else int(lap_s * 1000)
        phys = sim_info.SPageFilePhysics(packetId=i, speedKmh=150.0)
        graph = sim_info.SPageFileGraphic(
            status=sim_info.AC_LIVE, completedLaps=done, iLastTime=last_ms,
            normalizedCarPosition=pos)
        graph.carCoordinates[0] = pos * 1000
        ticks.append((t, {"physics": bytes(phys), "graphics": bytes(graph),
                          "static": _static(trackSPlineLength=4000.0)}))
    _write(path, ticks)


def test_a_replay_stores_the_laps_the_same_way_every_time():
    from assetto_mcp import db
    with tempfile.TemporaryDirectory() as d:
        rec = Path(d) / "two.simrec"
        _two_laps(rec)
        stored = []
        for run in ("a", "b"):
            path = Path(d) / f"{run}.db"
            recording.replay_into(rec, path)
            conn = db.connect(path)
            laps = [tuple(r) for r in conn.execute(
                "SELECT lap_number, lap_time_ms, out_lap, complete FROM laps"
                " ORDER BY id")]
            samples = conn.execute(
                "SELECT l.lap_number, s.t_ms, s.norm_pos, s.pos_x FROM samples"
                " s JOIN laps l ON l.id = s.lap_id ORDER BY s.rowid"
            ).fetchall()
            conn.close()
            stored.append((laps, [tuple(r) for r in samples]))
        laps, samples = stored[0]
        assert laps == [(1, 0, 1, 1), (2, 20000, 0, 1)], laps
        # The replay's clock is the recording's: the timed lap's samples
        # span its 20 seconds.
        timed = [t for n, t, *_ in samples if n == 2]
        assert len(timed) == 500 and 19900 <= timed[-1] < 20000, timed[-3:]
        assert stored[1] == stored[0]


def test_a_trimmed_recording_starts_with_every_page_as_it_was():
    ticks = [(1.0, _pages(100, 3)), (2.0, _pages(101, 3)),
             (3.0, _pages(102, 4)), (4.0, _pages(103, 4))]
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "all.simrec", Path(d) / "part.simrec"
        _write(src, ticks)
        recording.trim(src, out, 2.0, 3.0)
        assert list(recording.read(out)[1]) == [(0.0, ticks[1][1]),
                                                (1.0, ticks[2][1])]


REAL = Path(__file__).resolve().parent / "recordings" / "ac_911rsr_thompson.simrec"


def test_a_lap_driven_in_the_game_replays_to_the_time_it_showed():
    """A 1974 911 RSR at Thompson, recorded on the gaming PC: the end of one
    lap, all of the next, and a few seconds after it."""
    from assetto_mcp import db
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "real.db"
        recording.replay_into(REAL, path)
        conn = db.connect(path)
        lap = conn.execute("SELECT * FROM laps WHERE lap_number = 17"
                           ).fetchone()
        joined = conn.execute("SELECT * FROM laps WHERE lap_number = 16"
                              ).fetchone()
        rows = conn.execute("SELECT * FROM samples WHERE lap_id = ?",
                            (lap["id"],)).fetchall()
        conn.close()
    # The game's lap screen read 2:01.087, and the lap was clean.
    assert (lap["lap_time_ms"], lap["invalid"]) == (121087, 0), dict(lap)
    assert db.lap_usability(dict(lap)) == (True, None)
    # Recording began five seconds before lap 16 ended: the game's time for
    # it, over almost none of its corners.
    assert not db.lap_usability(dict(joined))[0], dict(joined)
    assert len(rows) > 0.95 * 121.087 * 25, len(rows)
    assert 121087 - 100 < rows[-1]["t_ms"] < 121087
    ranges = {"norm_pos": (0, 1), "speed_kmh": (0, 450), "gas": (0, 1),
              "brake": (0, 1), "steer": (-1, 1), "acc_lat": (-6, 6),
              "acc_lon": (-6, 6), "ride_f": (0, 0.5), "ride_r": (0, 0.5),
              **{f"press_{w}": (5, 60) for w in sim_info.WHEELS},
              **{f"core_{w}": (-20, 250) for w in sim_info.WHEELS},
              **{f"wear_{w}": (0, 100) for w in sim_info.WHEELS}}
    for col, (lo, hi) in ranges.items():
        values = [r[col] for r in rows]
        assert lo <= min(values) and max(values) <= hi, (col, min(values),
                                                         max(values))
    assert all(r["pos_x"] is not None for r in rows)


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
