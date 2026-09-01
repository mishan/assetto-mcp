"""Background collector: samples AC shared memory into SQLite, one lap at a time.

Runs as a daemon thread inside the MCP server process. Lap boundaries are
detected via graphics.completedLaps incrementing; the finished lap's official
time comes from graphics.iLastTime.
"""

import threading
import time

from . import analysis, db

TARGET_HZ = 25  # plenty for setup work; keeps the DB small

# A backwards jump in track position this large, with the lap counter not
# advancing, is a teleport: a reset to the pits, a retirement, or the car
# being returned after a crash. Comfortably above the noise in
# normalizedCarPosition. A legitimate line crossing is a bigger jump still,
# and usually advances completedLaps on the same tick, so it usually never
# reaches this check -- usually, because the counter can lag the position by
# a tick. That is what the wrap window below is for; this threshold on its
# own does not rule a line crossing out.
ABANDON_JUMP = 0.25

# Crossing the line sends norm_pos from ~1.0 to ~0.0, which is a backwards
# jump larger than any teleport. completedLaps normally advances on the same
# tick and the check below is skipped -- but not always, and one tick of lag
# was enough to manufacture a phantom 400ms "abandoned lap" at the start of
# every lap. Recognize the wrap by its shape instead of trusting the counter
# to arrive first. The cost is that a teleport from the last 10% of the lap
# is missed, which is far better than inventing one every lap.
WRAP_HIGH = 0.9
WRAP_LOW = 0.1

# The outlier rule lives in analysis so the same definition is used both
# here (at write time) and by db.revalidate_outlier_laps (over laps stored
# before the rule existed). Marking a lap invalid only excludes it from
# best-lap maths -- it is still stored and still readable.
_is_outlier = analysis.lap_is_outlier


# Reads of AC's fixed-size arrays. Both go through getattr because an older
# shared-memory layout, or a test stub standing in for one, may not carry
# the field at all -- and both then check the LENGTH, because "the field
# exists" and "the field has as many entries as I expect" are different
# claims and only the first one getattr answers.
#
# The distinction has teeth here specifically: these run inside the sampling
# loop, twenty-five times a second. An IndexError from either would come out
# as an exception from _loop, which ends the recording session and retries --
# so a layout one element short would cost the driver every lap of that run
# rather than one nullable field.
WHEELS_PER_CAR = 4
DAMAGE_ZONES = 5
WORLD_AXES = 3


def _seq(p, field: str, n: int) -> tuple | None:
    """`n` values from a fixed-size shared-memory array, or None.

    None means "not recorded", which is what a reader has to be able to tell
    from a real zero. A short array is treated the same way rather than
    padded: a partial read of a per-corner channel is not a measurement of
    anything, and pretending three wheels is four is worse than saying we
    do not have it.
    """
    v = getattr(p, field, None)
    if v is None:
        return None
    try:
        if len(v) < n:
            return None
    except TypeError:      # a ctypes array without __len__
        pass
    try:
        return tuple(v[i] for i in range(n))
    except (IndexError, TypeError):
        return None


def _wear(p) -> tuple:
    """Per-corner tyre wear, or four Nones if this layout lacks it."""
    return _seq(p, "tyreWear", WHEELS_PER_CAR) or (None,) * WHEELS_PER_CAR


def _damage(p):
    """Bodywork damage as one number: AC's five zones summed.

    Five separate columns would say where the car was hit, which nothing
    here asks. What is worth recording is whether it was hit at all, and
    the sum answers that in one field that can be differenced between
    samples. Zero throughout when the server has damage disabled.
    """
    zones = _seq(p, "carDamage", DAMAGE_ZONES)
    return float(sum(zones)) if zones is not None else None


class Collector:
    def __init__(self, db_path, sim_info_factory):
        """sim_info_factory: zero-arg callable returning a SimInfo-like object.

        Injected so tests can run without Windows/AC. The collector opens its
        own SQLite connection inside its thread; sharing one connection across
        the MCP main thread, the bridge, and this thread isn't safe.
        """
        self._db_path = db_path
        self._conn = None
        self._sim_factory = sim_info_factory
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.status = "stopped"
        # Whether this collector has ever been asked to run. A fresh object
        # and one that was deliberately stopped both reported status
        # "stopped" with no error and no session, which are the constructor's
        # defaults -- so a server that had been restarted underneath a
        # driving session was indistinguishable from one someone had turned
        # off on purpose. That ambiguity cost sixteen laps across three
        # sessions and I misread it twice.
        self.ever_started = False
        self.stopped_by_request = False
        self.session_id: int | None = None
        self.last_session_id: int | None = None
        self.laps_recorded = 0
        self.abandoned_laps = 0
        self.last_error: str | None = None
        # Observable progress. These exist so "has the collector noticed
        # yet?" is answerable rather than something callers have to guess at
        # with a sleep -- which is what made the test suite flaky on a
        # loaded CI runner, and what makes a stalled collector look
        # identical to an idle one in recording_status.
        self.sessions_started = 0
        self.samples_taken = 0
        self.current_lap_dirty = False
        self.current_lap_pitted = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # How long start() waits for the new thread to publish a status before
    # returning. The thread sets one almost immediately; this only has to
    # outlast scheduling. Bounded so a wedged thread cannot hang a tool call.
    START_STATUS_TIMEOUT = 2.0

    # How long to wait before looking for Assetto Corsa again. Long enough
    # that a closed game costs nothing, short enough that the driver never
    # waits for recording to pick up after launching it.
    SIM_RETRY_SECONDS = 3.0

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self.ever_started = True
        self.stopped_by_request = False
        # Cleared before the thread starts rather than after: a caller
        # reading status while the previous run's error was still set got a
        # failure report from a collector that had just been restarted.
        self.last_error = None
        self.status = "starting"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait for the thread to say something. start() used to return the
        # instant the thread was spawned, so start_recording read `status`
        # before the thread had touched it and reported the constructor's
        # "stopped" -- on every first call, for months. Calling it twice
        # "worked" only because the second call arrived late enough to see
        # the truth, which taught both of us to distrust a working tool.
        deadline = time.monotonic() + self.START_STATUS_TIMEOUT
        while self.status == "starting" and time.monotonic() < deadline:
            time.sleep(0.01)

    def stop(self):
        self._stop.set()
        self.stopped_by_request = True
        if self._thread:
            # Join generously and report if it didn't take. The thread owns
            # a SQLite connection, so returning while it is still alive
            # means the caller can delete the database out from under it --
            # which on Windows surfaces as an unrelated-looking
            # NotADirectoryError from a thread nobody was watching.
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                self.last_error = ("collector thread did not stop within 10s;"
                                   " its database connection is still open")
            else:
                self._thread = None
        self.status = "stopped"
        # session_id has to be cleared, not just left behind. The bridge
        # asks the collector which session inbound driver data belongs to; a
        # leftover id means notes and rival telemetry keep being filed
        # against a session that has ended, which is harder to spot than
        # filing them against none. Keep it separately for reporting, where
        # being stale is only cosmetic.
        if self.session_id is not None:
            self.last_session_id = self.session_id
        self.session_id = None

    # ------------------------------------------------------------------

    def _run(self):
        """Attach to AC, record until told to stop, and keep trying.

        This used to give up the first time shared memory could not be
        opened: AC not started yet, or between sessions, and the collector
        was finished for the lifetime of the process. Combined with the
        server being restarted by its host whenever it liked, that is how
        sixteen laps were lost across three evenings -- every one of them
        driven past a collector that had already quietly retired.

        So the loop outlives the sim now. AC being absent is a state to wait
        in, not an error to die of, and an exception from the recording loop
        is logged and retried rather than fatal. Nothing here spins: every
        wait goes through the stop event, so stop() is still immediate.
        """
        from .sim_info import AC_LIVE  # local import keeps module testable

        self._conn = db.connect(self._db_path)
        try:
            while not self._stop.is_set():
                try:
                    sim = self._sim_factory()
                except Exception as e:  # AC not running / not Windows
                    # Reported, but as a state rather than a failure -- the
                    # driver has not done anything wrong by not having the
                    # game open yet.
                    self.status = "waiting for Assetto Corsa"
                    self.last_error = str(e)
                    if self._stop.wait(self.SIM_RETRY_SECONDS):
                        break
                    continue

                self.status = "waiting for AC to go live"
                self.last_error = None
                try:
                    self._loop(sim, AC_LIVE)
                except Exception as e:
                    # Keep the reason, then go round again. A crash in the
                    # recording loop used to end recording permanently and
                    # look, from outside, exactly like a collector that had
                    # never been asked to run.
                    self.last_error = str(e)
                    self.status = "error, retrying"
                finally:
                    try:
                        sim.close()
                    except Exception:      # noqa: BLE001 - teardown only
                        pass
                    # The session belongs to the sim connection that is
                    # ending, not to the next one.
                    if self.session_id is not None:
                        self.last_session_id = self.session_id
                        self.session_id = None
                if self._stop.is_set():
                    break
                self._stop.wait(self.SIM_RETRY_SECONDS)
        finally:
            self._conn.close()
            self._conn = None

    def _loop(self, sim, AC_LIVE):
        interval = 1.0 / TARGET_HZ
        session_started = False
        lap_samples: list[tuple] = []
        lap_dirty = False        # any tyres-out excursion this lap
        lap_pitted = False       # driver entered the pit lane this lap
        session_best = None      # fastest valid lap so far, for outlier check
        last_completed = None
        lap_start_wall = None
        last_packet = -1
        last_pos = None

        while not self._stop.is_set():
            g = sim.graphics
            p = sim.physics

            if g.status != AC_LIVE:
                # Session ended / paused / menu: drop the partial lap.
                if g.status == 0:  # AC_OFF -> back to waiting
                    session_started = False
                    last_completed = None
                    session_best = None
                    lap_samples = []
                    # Say so. Leaving this reading "recording (session 7)"
                    # while AC sits in the menus is the same class of lie as
                    # the overlay claiming nothing is being recorded.
                    self.status = "waiting for AC to go live"
                time.sleep(0.25)
                continue

            # Restarting a session from the in-game menu resets completedLaps
            # without ever passing through AC_OFF, so watching only for that
            # transition left the new run appended to the old session -- same
            # session_id, lap numbers starting over, and two different track
            # states averaged together. A lap counter that goes backwards can
            # only mean a restart, so roll a fresh session on it.
            if (session_started and last_completed is not None
                    and g.completedLaps < last_completed):
                session_started = False
                last_completed = None
                session_best = None
                lap_samples = []
                lap_dirty = False
                lap_pitted = False

            if not session_started:
                s = sim.static
                self.session_id = db.create_session(
                    self._conn,
                    car=s.carModel, track=s.track,
                    track_config=s.trackConfiguration,
                    tyre_compound=g.tyreCompound,
                    air_temp=p.airTemp, road_temp=p.roadTemp,
                )
                session_started = True
                last_completed = g.completedLaps
                last_pos = None
                lap_samples = []
                lap_dirty = False
                lap_pitted = False
                lap_start_wall = time.monotonic()
                self.sessions_started += 1
                self.current_lap_dirty = False
                self.current_lap_pitted = False
                self.status = f"recording (session {self.session_id})"

            # Lap boundary?
            if g.completedLaps != last_completed:
                lap_time = g.iLastTime
                # First crossing of the line after leaving the pits produces
                # an out-lap with no meaningful time; iLastTime is 0 then.
                if lap_samples and lap_time > 0:
                    valid = (not lap_dirty
                             and not lap_pitted
                             and not _is_outlier(lap_time, session_best))
                    # setup_name omitted: store_lap snapshots whatever setup
                    # the session is currently marked as running, so there
                    # is one source of truth rather than a cached copy here
                    # that another instance's set_session_setup can't reach.
                    db.store_lap(self._conn, self.session_id,
                                 last_completed + 1, lap_time, valid,
                                 lap_samples)
                    self.laps_recorded += 1
                    # Reference for the outlier rule: fastest lap that was
                    # actually driven, whether or not it was clean. Deriving
                    # it from valid laps only made this rule a dependent of
                    # the dirty-lap rule -- at a track with tight limits
                    # every lap can be dirty, leaving no reference at all.
                    if (not lap_pitted
                            and (session_best is None
                                 or lap_time < session_best)):
                        session_best = lap_time
                last_completed = g.completedLaps
                lap_samples = []
                lap_dirty = False
                lap_pitted = False
                lap_start_wall = time.monotonic()
                # Forget the previous position too. The lap counter and the
                # position wrap do not land on the same tick, so keeping
                # ~0.98 here and meeting ~0.01 next time round looks exactly
                # like a teleport -- and clearing lap_samples alone did not
                # prevent it, because a sample or two arrives in between.
                last_pos = None
                self.current_lap_dirty = False
                self.current_lap_pitted = False

            # Did the car leave the lap without finishing it?
            #
            # A crash, a retirement, or a reset to the pits teleports the car
            # backwards down the spline without completedLaps advancing. The
            # samples were simply dropped, so the one lap a driver most wants
            # to look at -- the one that ended in the barrier -- was the only
            # one guaranteed not to be stored. Keep it, marked incomplete.
            #
            # Crossing the line also sends norm_pos from ~1.0 to ~0.0. It
            # usually advances completedLaps first, which resets lap_samples
            # above and settles the matter -- but when the counter lags a
            # tick it does not, so it is the wrap window just below, not the
            # reset above, that keeps a line crossing out of this check.
            pos = g.normalizedCarPosition
            wrapped = (last_pos is not None
                       and last_pos > WRAP_HIGH and pos < WRAP_LOW)
            if (lap_samples and last_pos is not None and not wrapped
                    and not g.isInPitLane
                    and last_pos - pos > ABANDON_JUMP):
                elapsed = int((time.monotonic() - lap_start_wall) * 1000)
                db.store_lap(self._conn, self.session_id,
                             last_completed + 1, elapsed, False,
                             lap_samples, complete=False)
                self.laps_recorded += 1
                self.abandoned_laps += 1
                lap_samples = []
                lap_dirty = False
                lap_pitted = False
                lap_start_wall = time.monotonic()
                self.current_lap_dirty = False
                self.current_lap_pitted = False
            last_pos = pos

            # A lap containing a pit visit is wall-clock nonsense -- the
            # 4:34 and 10:22 "valid" laps in testing were both stops. Note it
            # before the sampling guard below, which skips pit-lane ticks and
            # would otherwise hide the visit entirely.
            if g.isInPitLane:
                lap_pitted = True
                self.current_lap_pitted = True

            # New physics tick since last sample?
            if p.packetId != last_packet and not g.isInPitLane:
                last_packet = p.packetId
                t_ms = int((time.monotonic() - lap_start_wall) * 1000)
                if p.numberOfTyresOut > 2:
                    lap_dirty = True
                    self.current_lap_dirty = True
                self.samples_taken += 1
                # Position, attitude and electronics activity. Read through
                # getattr because a test stub or an older shared-memory
                # layout may not carry them, and a missing field must record
                # None rather than 0 -- for a coordinate, zero is a claim
                # that the car was at the track origin, which is a place on
                # the map.
                #
                # Through _seq for the same reason tyre wear and damage are:
                # a field that exists but is short raises IndexError here, in
                # the sampling loop, which ends the session rather than
                # losing one column. Position is the one where a partial read
                # would also be meaningless -- an x with no z is not a place.
                pos_x, pos_y, pos_z = _seq(g, "carCoordinates",
                                           WORLD_AXES) or (None, None, None)
                lap_samples.append((
                    t_ms,
                    g.normalizedCarPosition,
                    p.speedKmh, p.gas, p.brake, p.steerAngle,
                    p.gear - 1,  # shift so 0=N, -1=R, 1=1st: matches HUD
                    p.rpms,
                    p.accG[0], p.accG[2],  # lateral, longitudinal
                    p.wheelSlip[0], p.wheelSlip[1],
                    p.wheelSlip[2], p.wheelSlip[3],
                    p.wheelsPressure[0], p.wheelsPressure[1],
                    p.wheelsPressure[2], p.wheelsPressure[3],
                    p.tyreCoreTemperature[0], p.tyreCoreTemperature[1],
                    p.tyreCoreTemperature[2], p.tyreCoreTemperature[3],
                    p.rideHeight[0], p.rideHeight[1],
                    p.numberOfTyresOut,
                    pos_x, pos_y, pos_z,
                    getattr(p, "heading", None),
                    getattr(p, "pitch", None),
                    getattr(p, "roll", None),
                    getattr(p, "tc", None),
                    getattr(p, "abs", None),
                    *_wear(p),
                    _damage(p),
                ))

            time.sleep(interval)
