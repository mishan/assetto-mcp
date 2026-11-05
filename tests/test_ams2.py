"""Automobilista 2's shared memory: its layout, reading it whole, recording
it, and reading it as Frames.

The layout is checked against the size and offsets the game's own
SharedMemory.h gives when compiled. The reader is checked on pages built in
memory, and on a recording from the game with the lap times its screen
showed.
"""

import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402

from telemetrist.sims import ams2, recording  # noqa: E402


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


REAL = (Path(__file__).resolve().parent / "recordings"
        / "ams2_g40_brands_indy.simrec")


def test_a_session_driven_in_the_game_replays_to_the_times_it_showed():
    """A Ginetta G40 Cup at Brands Hatch Indy in Free Practice, from the
    menus: out of the garage, four timed laps and a long off on the
    fourth, then back to the garage mid-lap."""
    from telemetrist import db
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "real.db"
        recording.replay_into(REAL, path)
        conn = db.connect(path)
        session = conn.execute("SELECT * FROM sessions").fetchone()
        laps = conn.execute("SELECT * FROM laps ORDER BY lap_number"
                            ).fetchall()
        rows = conn.execute(
            "SELECT * FROM samples WHERE lap_id IN (SELECT id FROM laps"
            " WHERE NOT out_lap)").fetchall()
        conn.close()
    assert (session["sim"], session["car"], session["track_config"]) == (
        "ams2", "Ginetta G40 Cup", "BrandsHatch_Indy"), dict(session)
    assert abs(session["track_length_m"] - 1932.8) < 0.1
    # The game's lap screen: 1:00.019, 0:58.220, 0:58.160, 1:25.345, after
    # an out-lap it does not time.
    assert [(l["lap_time_ms"], l["out_lap"]) for l in laps] == [
        (0, 1), (60019, 0), (58220, 0), (58160, 0), (85345, 0)]
    # A wheel on the grass for an instant on the third; all four off for
    # half a minute on the fourth.
    assert [l["invalid"] for l in laps[1:]] == [0, 0, 0, 1]
    ranges = {"norm_pos": (0, 1), "speed_kmh": (0, 200), "gas": (0, 1),
              "brake": (0, 1), "steer": (-1, 1), "acc_lat": (-2, 2),
              "acc_lon": (-2, 2), "ride_f": (0.02, 0.15),
              "ride_r": (0.02, 0.15),
              # 1.75 bar cold in the garage; carcass temperatures from K.
              **{f"press_{w}": (24, 32) for w in ("fl", "fr", "rl", "rr")},
              **{f"core_{w}": (25, 90) for w in ("fl", "fr", "rl", "rr")},
              **{f"wear_{w}": (95, 100) for w in ("fl", "fr", "rl", "rr")}}
    for col, (lo, hi) in ranges.items():
        values = [r[col] for r in rows]
        assert lo <= min(values) and max(values) <= hi, (col, min(values),
                                                         max(values))
    _axes_match_ac(rows)


def _axes_match_ac(rows):
    """The signs AC's fixture has, which analysis and the maps assume:
    heading is the direction of travel as atan2(-dx, dz) of the positions,
    steering and heading turn together, lateral g opposes them, roll goes
    with lateral g and pitch with longitudinal g."""
    def wrap(a):
        return (a + math.pi) % (2 * math.pi) - math.pi
    off, turn, lateral = [], [], []
    for a, b in zip(rows, rows[1:]):
        dx, dz = b["pos_x"] - a["pos_x"], b["pos_z"] - a["pos_z"]
        if (b["lap_id"] != a["lap_id"] or b["t_ms"] - a["t_ms"] > 100
                or math.hypot(dx, dz) < 0.5):
            continue
        off.append(abs(wrap(math.atan2(-dx, dz) - b["heading"])))
        if abs(b["steer"]) > 0.08:
            turn.append(wrap(b["heading"] - a["heading"]) * b["steer"] > 0)
            lateral.append(b["acc_lat"] * b["steer"] < 0)
    off.sort()
    assert off[len(off) // 2] < 0.05, off[len(off) // 2]
    assert sum(turn) > 0.9 * len(turn) and sum(lateral) > 0.9 * len(lateral)

    def cov(x, y):
        mx, my = sum(x) / len(x), sum(y) / len(y)
        return sum((a - mx) * (b - my) for a, b in zip(x, y))
    assert cov([r["roll"] for r in rows], [r["acc_lat"] for r in rows]) > 0
    assert cov([r["pitch"] for r in rows], [r["acc_lon"] for r in rows]) > 0


def test_the_server_replays_an_ams2_recording_as_ams2():
    """TELEMETRIST_REPLAY stands in for whichever game made the file."""
    import json
    import subprocess
    from test_supervisor import _mcp_missing, _server_env
    if _mcp_missing():
        from lua_harness import skip
        skip("mcp is not installed")
        return
    code = ("import json, telemetrist.server as s; r = s._sim_factory();"
            " v = s._open_sim(); print(json.dumps([r.name, v.name,"
            " v.session().car])); s._shutdown()")
    with tempfile.TemporaryDirectory() as d:
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=Path(__file__).parent.parent,
            env=_server_env(d, TELEMETRIST_REPLAY=str(REAL)),
            capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.splitlines()[-1]) == [
        "ams2", "ams2", "Ginetta G40 Cup"], proc.stdout


class _Pages:
    """A page for the reader, set field by field between reads. Car 1 is
    another driver, always on its seventh lap."""

    def __init__(self):
        self.p = ams2.SharedMemory(mVersion=ams2.VERSION, mNumParticipants=2,
                                   mTrackLength=1000.0)
        for car in self.p.mParticipantInfo[:2]:
            car.mIsActive = True
        self.p.mParticipantInfo[1].mLapsCompleted = 7

    def page(self):
        return self.p

    def close(self):
        pass

    def set(self, game=2, session=1, laps=0, clock=-1.0, dist=0.0,
            last=-1.0, pit=0, viewed=0, length=1000.0):
        p = self.p
        p.mGameState, p.mSessionState, p.mPitMode = game, session, pit
        p.mViewedParticipantIndex, p.mTrackLength = viewed, length
        p.mCurrentTime, p.mLastLapTime = clock, last
        car = p.mParticipantInfo[0]
        car.mLapsCompleted, car.mCurrentLapDistance = laps, dist


def _run(steps):
    """Each step: the page, then the reader's status, completed_laps and
    last_lap_ms, None where it does not matter."""
    pages = _Pages()
    reader = ams2.Reader(pages)
    for i, (page, want) in enumerate(steps):
        pages.set(**page)
        f = reader.read()
        got = tuple(None if w is None else g for w, g in zip(
            want, (f.status, f.completed_laps, f.last_lap_ms)))
        assert got == want, (i, page, got)


# Down the pit lane and over the line, which starts no timer, to a place
# on the out-lap.
_OUT = [(dict(pit=4, dist=980), ("live", 0, 0)),
        (dict(pit=3, dist=995), ("live", 0, 0)),
        (dict(pit=3, dist=5), ("live", 0, 0)),
        (dict(dist=950), ("live", 0, 0))]


def test_every_crossing_of_the_line_is_a_lap_and_the_out_lap_has_no_time():
    _run([
        # The menus: no car, no track.
        (dict(game=1, viewed=-1, length=0.0), ("off", None, None)),
        # A car but no track yet.
        (dict(game=1, length=0.0), ("off", None, None)),
        *_OUT,
        # The out-lap ends: the timer starts, the game's count does not move.
        (dict(dist=3, clock=0.02), ("live", 1, 0)),
        (dict(dist=990, clock=59.9), ("live", 1, 0)),
        (dict(laps=1, dist=2, clock=0.03, last=60.0187), ("live", 2, 60019)),
        # Paused mid-lap, then back to the garage: the timer stops, the
        # last time stays the old one.
        (dict(game=3, laps=1, dist=500, clock=30.0, last=60.0187),
         ("pause", 2, 60019)),
        (dict(game=4, laps=1, dist=980, pit=4, last=60.0187),
         ("pause", 2, 60019)),
        # Out again: this crossing ends an out-lap too, whatever the game
        # still shows as its last lap, and on a paused frame.
        (dict(laps=1, dist=960, last=60.0187), ("live", 2, 60019)),
        (dict(game=3, laps=1, dist=4, clock=0.04, last=60.0187),
         ("pause", 3, 0)),
        (dict(laps=1, dist=500, clock=30.0, last=60.0187), ("live", 3, 0)),
        (dict(laps=2, dist=1, clock=0.01, last=58.22), ("live", 4, 58220)),
    ])


def test_a_restart_or_a_new_session_starts_the_count_again():
    for restart in (
            # The game's count drops.
            [(dict(laps=0, dist=950), ("live", 0, 0))],
            # Only the restart state, seen during the first timed lap,
            # where the game's count is already 0 and cannot drop.
            [(dict(game=5, dist=950), ("off", 0, None)),
             (dict(dist=950), ("live", 0, 0))],
            # The next session.
            [(dict(session=3, dist=950), ("live", 0, 0))]):
        _run([*_OUT, (dict(dist=3, clock=0.02), ("live", 1, 0)),
              (dict(dist=500, clock=30.0), ("live", 1, 0)),
              *([(dict(laps=1, dist=2, clock=0.03, last=60.0), (
                  "live", 2, 60000))] if restart[0][0].get("laps") == 0
                else []),
              *restart])


def test_only_a_timer_started_at_the_line_ends_an_out_lap():
    # (what comes first, the start, the game's count after, the reader's)
    for before, start, game, want in (
            # The timer one read after the car crosses.
            (_OUT, [(dict(dist=3), ("live", 0, 0)),
              (dict(dist=20, clock=0.4), ("live", 1, 0))], 0, 1),
            # A standing start: on the grid, the timer starts short of the
            # line, and the car crosses a second later.
            ([(dict(dist=985), ("live", 0, 0))],
             [(dict(dist=990, clock=0.0), ("live", 0, 0))]
             + [(dict(dist=992 + i / 4, clock=0.04 * (i + 1)),
                 ("live", 0, 0)) for i in range(25)]
             + [(dict(dist=2, clock=1.1), ("live", 0, 0))], 0, 0),
            # A crossing the game counts itself.
            (_OUT, [(dict(laps=1, dist=3, clock=0.02), ("live", 1, 0))],
             1, 1)):
        _run([*before, *start,
              (dict(laps=game, dist=500, clock=30.0), ("live", want, None))])


def test_the_count_stays_on_the_driver_whoever_the_camera_follows():
    _run([*_OUT, (dict(dist=3, clock=0.02), ("live", 1, 0)),
          (dict(viewed=1, dist=500, clock=30.0), ("live", 1, 0)),
          (dict(viewed=1, laps=1, dist=2, clock=0.03, last=60.0),
           ("live", 2, 60000)),
          # Nobody followed as the game's count drops on a restart.
          (dict(viewed=-1, dist=950), ("live", 0, 0)),
          (dict(viewed=1, dist=960), ("live", 0, 0)),
          (dict(viewed=1, laps=1, dist=2, clock=0.03, last=58.0),
           ("live", 1, 58000))])


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
