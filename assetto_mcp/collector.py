"""Background collector: samples AC shared memory into SQLite, one lap at a time.

Runs as a daemon thread inside the MCP server process. Lap boundaries are
detected via graphics.completedLaps incrementing; the finished lap's official
time comes from graphics.iLastTime.
"""

import os
import socket
import threading
import time
import uuid

from . import analysis, db, retention

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
# here (at write time) and by db.backfill_outliers (over laps stored before
# the rule existed). Being an outlier only sets a flag: the lap is stored,
# readable, and still included in comparisons, which say it was slow.
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

# Why a collector that was recording stopped. A sentinel rather than a bare
# string because one of the two callers has to tell them apart: standing
# down because someone switched recording off is not a failure and must not
# be reported as last_error, while losing the claim is.
SWITCHED_OFF = "recording was switched off by stop_recording"


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
        # Set by the thread as soon as it has published a status, so start()
        # can wait on it instead of polling an attribute.
        self._announced = threading.Event()
        # Who this collector is, in the recorder table. The pid alone is not
        # enough -- an OS recycles them, and a recycled pid would look like
        # our own stale claim and be taken back without a takeover ever being
        # noticed. The token makes each run of each process distinct.
        self._owner = (f"{socket.gethostname()}/{os.getpid()}"
                       f"/{uuid.uuid4().hex[:8]}")
        self._last_beat = 0.0
        # Whether this instance is the one allowed to write laps. False is a
        # normal, healthy state: another server process has the claim.
        self.holds_recorder = False
        self.standby_owner: str | None = None
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
        self.out_laps_recorded = 0
        self.last_retention: dict | None = None
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

    # How long to wait before asking again whether the recorder claim has
    # come free. Short, because the case it exists for is the holder's
    # process dying mid-session, and the gap is laps nobody is recording.
    STANDBY_RETRY_SECONDS = 5.0

    # How often to re-assert the claim and touch the session row. Must stay
    # comfortably under db.RECORDER_STALE_SECONDS so an ordinary hitch --
    # a slow disk, a long GC pause -- cannot be mistaken for a dead process
    # by another instance that is watching this same clock.
    HEARTBEAT_SECONDS = 3.0

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._announced.clear()
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
        #
        # An Event rather than a sleep loop: this is now on the import path
        # via autostart, where a busy-wait is startup latency the driver
        # pays on every server launch.
        self._announced.wait(self.START_STATUS_TIMEOUT)

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
        self.holds_recorder = False
        self.standby_owner = None
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

    def _announce(self, status: str) -> None:
        self.status = status
        self._announced.set()

    def _beat(self, force: bool = False) -> str | None:
        """Re-assert the claim and the heartbeat, and check we still may.

        None means carry on. A string is the reason to stop writing, and
        both reasons are things another process decided while this one was
        inside the recording loop:

        * the claim is gone -- another instance took it after we went quiet
          for longer than db.RECORDER_STALE_SECONDS. Two collectors storing
          the same laps is the failure this whole mechanism exists to
          prevent, and losing a claim is the one way it can still happen.
        * recording was switched off. `enabled` is shared precisely so that
          stop_recording reaches whichever process is holding the recorder
          rather than whichever chat received the call -- and the holder is
          normally inside _loop, which is the one place that never returns
          to the outer loop where the flag was being read. So a driver in
          any other chat could call stop_recording, be told recording had
          stopped, and have the holder carry on writing laps until the game
          closed. The flag has to be read where the writing happens.

        Both checks share the heartbeat's timer, so this costs one extra
        query every HEARTBEAT_SECONDS rather than one per sample.
        """
        now = time.monotonic()
        if not force and now - self._last_beat < self.HEARTBEAT_SECONDS:
            return None
        self._last_beat = now
        if not db.renew_recorder(self._conn, self._owner):
            return ("another instance took over recording; this one stopped "
                    "to avoid storing every lap twice")
        if not db.recorder_enabled(self._conn):
            return SWITCHED_OFF
        if self.session_id is not None:
            db.touch_session(self._conn, self.session_id)
        return None

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

        Standing by because another instance holds the recorder is a state
        to wait in for the same reason and with the same shape. That
        instance can be killed at any moment -- it is a server process the
        host owns -- and when its claim goes stale this loop is what picks
        the session up.
        """
        from .sim_info import AC_LIVE  # local import keeps module testable

        try:
            self._conn = db.connect(self._db_path)
        except Exception as e:      # unwritable data dir, corrupt file
            # Said out loud. This used to kill the thread before it had
            # published anything, leaving status at "starting", last_error
            # at None, and _collector_state reporting "died" with no reason
            # -- a collector that has failed should be at least as legible
            # as one that is merely waiting.
            self.last_error = str(e)
            self._announce("error: could not open the database")
            return
        try:
            while not self._stop.is_set():
                if not db.recorder_enabled(self._conn):
                    # Someone called stop_recording. That is an instruction
                    # about the car and it is shared, so every instance
                    # honours it -- including one started fresh after a
                    # restart, which is what made the old per-process stop
                    # last only until the host felt like recycling us.
                    db.release_recorder(self._conn, self._owner)
                    self.holds_recorder = False
                    self.standby_owner = None
                    # Cleared, because being switched off is not a failure
                    # and there may be an old one sitting here. A collector
                    # doing exactly what it was told, reporting the reason
                    # some earlier problem gave, is how recording_status
                    # sends someone after a fault that is not there.
                    self.last_error = None
                    self._announce("standby (recording is switched off)")
                    if self._stop.wait(self.STANDBY_RETRY_SECONDS):
                        break
                    continue

                claim = db.claim_recorder(self._conn, self._owner)
                if not claim["held"]:
                    # Not an error, and not something the driver has to fix.
                    # Another server process is recording; this one waits in
                    # case that process dies.
                    #
                    # last_error cleared for the same reason as in the
                    # switched-off branch above: an instance that is healthy
                    # and correctly standing aside must not keep reporting
                    # whatever went wrong before it got here. A transient
                    # failure to open shared memory, then a standby, and
                    # recording_status would show a working collector next
                    # to a stale reason to worry about it.
                    self.holds_recorder = False
                    self.standby_owner = claim["owner"]
                    self.last_error = None
                    self._announce("standby (another instance is recording)")
                    if self._stop.wait(self.STANDBY_RETRY_SECONDS):
                        break
                    continue
                self.holds_recorder = True
                self.standby_owner = None
                self._last_beat = 0.0

                try:
                    sim = self._sim_factory()
                except Exception as e:  # AC not running / not Windows
                    # Reported, but as a state rather than a failure -- the
                    # driver has not done anything wrong by not having the
                    # game open yet.
                    self._announce("waiting for Assetto Corsa")
                    self.last_error = str(e)
                    # Result ignored on purpose: this path goes straight back
                    # round the outer loop, which re-reads both the enabled
                    # flag and the claim before doing anything. _loop is the
                    # only place that has to act on it, because it is the
                    # only place that does not come back here.
                    self._beat()
                    if self._stop.wait(self.SIM_RETRY_SECONDS):
                        break
                    continue

                self._announce("waiting for AC to go live")
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
            # Hand the claim back rather than making the next instance wait
            # out RECORDER_STALE_SECONDS for a process that has politely
            # finished. Best-effort: if this fails the staleness rule still
            # frees it, just later.
            try:
                db.release_recorder(self._conn, self._owner)
            except Exception:          # noqa: BLE001 - teardown only
                pass
            self.holds_recorder = False
            self._conn.close()
            self._conn = None

    def _housekeeping(self) -> None:
        """One retention pass, at session start, if this instance may do it.

        Three guards, each for a way this can hurt rather than help:

        - Only the claim holder runs it. A standby instance thinning the
          database underneath the one actually recording is pure contention
          for no benefit.
        - The heartbeat is forced either side. A VACUUM over a multi-gigabyte
          file can exceed RECORDER_STALE_SECONDS, and a claim that goes stale
          mid-pass is handed to another instance, which then opens a second
          session for the same drive and blocks on the exclusive lock this
          one is still holding.
        - Anything raised is recorded and swallowed. Housekeeping must never
          be the reason a session is not recorded.

        It still costs a pause while the car is in the garage, which is why
        it is here and not between two flying laps.
        """
        if not self.holds_recorder:
            return
        try:
            self._beat(force=True)
            result = retention.enforce_budget(self._conn, self._db_path)
            if result.get("acted"):
                self.last_retention = result
        except Exception as e:
            self.last_error = f"retention pass failed: {e}"
        finally:
            try:
                self._beat(force=True)
            except Exception:
                pass

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
            # Before anything is read, let alone written. Both reasons this
            # can refuse were decided by another process while we were in
            # here: the claim taken over, or recording switched off. This is
            # the only place either is noticed, because this loop does not
            # return to the outer one while a session is live.
            stand_down = self._beat()
            if stand_down:
                # Being switched off is not an error, so it does not go in
                # last_error. Losing the claim is: something stalled this
                # process for RECORDER_STALE_SECONDS and somebody should
                # know. The outer loop announces the switched-off case
                # properly on its next pass.
                self.last_error = (None if stand_down == SWITCHED_OFF
                                   else stand_down)
                self.status = f"standby ({stand_down})"
                self.holds_recorder = False
                # Clear the session here, not on the way out of _run. The
                # outer loop's finally does it eventually, but "eventually"
                # is a window in which holds_recorder is already False and
                # session_id is still set -- and _collector_state reads
                # session_id first, so recording_status would answer
                # "recording" for a collector that had just stopped writing.
                # Reporting the stop late is the same class of lie the state
                # machine exists to end.
                if self.session_id is not None:
                    self.last_session_id = self.session_id
                    self.session_id = None
                return

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
                    self._announce("waiting for AC to go live")
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
                self._housekeeping()
                last_completed = g.completedLaps
                last_pos = None
                lap_samples = []
                lap_dirty = False
                lap_pitted = False
                lap_start_wall = time.monotonic()
                self.sessions_started += 1
                self.current_lap_dirty = False
                self.current_lap_pitted = False
                self._announce(f"recording (session {self.session_id})")
                # Immediately, not on the next tick: the session row is what
                # every other instance reads to decide whether anything is
                # recording, and it has to be true from the moment it exists.
                self._beat(force=True)

            # Lap boundary?
            if g.completedLaps != last_completed:
                lap_time = g.iLastTime
                # First crossing of the line after leaving the pits produces
                # an out-lap with no meaningful time; iLastTime is 0 then.
                # An out-lap used to be dropped here, samples and all. The
                # driving after pit exit is real telemetry; what is not real
                # is its lap time, so it is stored and flagged instead.
                if lap_samples:
                    out_lap = lap_time <= 0
                    # Track limits are no longer decided here. store_lap
                    # scores them from the samples, which is what lets the
                    # threshold change later and be re-applied to laps
                    # already driven. lap_dirty survives only to tell the
                    # in-game overlay something is happening right now.
                    #
                    # setup_name omitted: store_lap snapshots whatever setup
                    # the session is currently marked as running, so there
                    # is one source of truth rather than a cached copy here
                    # that another instance's set_session_setup can't reach.
                    db.store_lap(
                        self._conn, self.session_id, last_completed + 1,
                        lap_time, True, lap_samples,
                        out_lap=out_lap, pitted=lap_pitted,
                        outlier=(not out_lap and not lap_pitted
                                 and _is_outlier(lap_time, session_best)))
                    self.laps_recorded += 1
                    if out_lap:
                        self.out_laps_recorded += 1
                    # Reference for the outlier rule: fastest lap that was
                    # actually driven, whether or not it was clean. Deriving
                    # it from valid laps only made this rule a dependent of
                    # the dirty-lap rule -- at a track with tight limits
                    # every lap can be dirty, leaving no reference at all.
                    if (not out_lap and not lap_pitted
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
                             lap_samples, complete=False, pitted=lap_pitted)
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
                # Drives the in-game overlay's "running wide" indicator
                # only. The stored verdict is scored from the samples at
                # store time -- but this must use the same threshold, or
                # the overlay flags a lap dirty in real time that the
                # database then records as clean, which is the original
                # complaint surviving in the one place the driver sees it.
                if p.numberOfTyresOut >= db.TRACK_LIMITS_WHEELS:
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
