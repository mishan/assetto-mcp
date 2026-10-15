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
# normalizedCarPosition. A line crossing is a bigger jump still; the wrap
# window below, not this threshold, keeps it out.
ABANDON_JUMP = 0.25

# Crossing the line sends norm_pos from ~1.0 to ~0.0, a backwards jump
# larger than any teleport. completedLaps can lag the position by a tick, so
# the wrap is recognized by its shape rather than by the counter. The cost is
# that a teleport from the last 10% of the lap is missed, which is far better
# than a phantom abandoned lap at the start of every lap.
WRAP_HIGH = 0.9
WRAP_LOW = 0.1

# The outlier rule lives in analysis so the same definition is used both
# here (at write time) and by db.backfill_outliers. Being an outlier only
# sets a flag: the lap is stored, readable, and still included in
# comparisons, which say it was slow.
_is_outlier = analysis.lap_is_outlier


# Reads of AC's fixed-size arrays go through getattr, because an older
# shared-memory layout or a test stub may not carry the field, and then check
# its length. They run inside the sampling loop: an IndexError there ends the
# recording session, so a layout one element short would cost every lap of
# the run rather than one nullable field.
WHEELS_PER_CAR = 4
DAMAGE_ZONES = 5
WORLD_AXES = 3

# Why a collector that was recording stopped. A sentinel so callers can tell
# it apart: being switched off is not a failure and must not be reported as
# last_error, while losing the claim is.
SWITCHED_OFF = "recording was switched off by stop_recording"


def _seq(p, field: str, n: int) -> tuple | None:
    """`n` values from a fixed-size shared-memory array, or None.

    None means "not recorded", as distinct from a real zero. A short array
    is treated the same way rather than padded: a partial read of a
    per-corner channel is not a measurement of anything.
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

    Nothing here asks where the car was hit, only whether it was, and the
    sum answers that in one field that can be differenced between samples.
    Zero throughout when the server has damage disabled.
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
        # enough: a recycled pid would look like our own stale claim and be
        # taken back without a takeover being noticed.
        self._owner = (f"{socket.gethostname()}/{os.getpid()}"
                       f"/{uuid.uuid4().hex[:8]}")
        self._last_beat = 0.0
        # Whether this instance is the one allowed to write laps. False is a
        # normal, healthy state: another server process has the claim.
        self.holds_recorder = False
        self.standby_owner: str | None = None
        self.status = "stopped"
        # These tell a server restarted underneath a driving session apart
        # from one someone stopped on purpose; status alone reads "stopped"
        # for both.
        self.ever_started = False
        self.stopped_by_request = False
        self.session_id: int | None = None
        self.last_session_id: int | None = None
        self.laps_recorded = 0
        self.abandoned_laps = 0
        self.out_laps_recorded = 0
        self.last_retention: dict | None = None
        self.last_error: str | None = None
        # Observable progress, so "has the collector noticed yet?" can be
        # answered without a sleep, and a stalled collector does not look
        # identical to an idle one in recording_status.
        self.sessions_started = 0
        self.samples_taken = 0
        self.current_lap_dirty = False
        self.current_lap_pitted = False
        # Whether the last tick saw the game in a live session, with the car
        # out of the menus. status cannot answer this: it keeps saying
        # "recording (session 7)" through a pause. restart_server reads this
        # so it never ends a lap being driven.
        self.live = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # How long start() waits for the new thread to publish a status. Before
    # the first _announce, _run opens the database (creating the file and
    # migrating the schema), reads the enabled flag and takes the claim, and
    # any of those writes can block for up to db.BUSY_TIMEOUT_MS on the write
    # lock. A shorter deadline reports "starting" from a collector that is
    # working. Still bounded, so a wedged thread cannot hang a tool call.
    START_STATUS_TIMEOUT = db.BUSY_TIMEOUT_MS / 1000 + 5.0

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
        # Cleared before the thread starts, so a caller reading status
        # never sees the previous run's error on a fresh start.
        self.last_error = None
        self.status = "starting"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait for the thread's first status, so start_recording does not
        # report the constructor's "stopped". An Event rather than a sleep
        # loop because this is on the import path via autostart.
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
        self.live = False
        # The bridge asks the collector which session inbound driver data
        # belongs to, so a leftover id would file notes and rival telemetry
        # against a session that has ended. Keep it separately for
        # reporting, where being stale is only cosmetic.
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
        * recording was switched off. `enabled` is shared so stop_recording
          reaches whichever process holds the recorder, and the holder is
          normally inside _loop, which does not return to the outer loop
          while a session is live. The flag has to be read where the
          writing happens.

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

        The loop outlives the sim. AC being absent (not started yet, or
        between sessions) is a state to wait in, not an error to die of, and
        an exception from the recording loop is logged and retried rather
        than fatal. Every wait goes through the stop event, so stop() is
        immediate.

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
            # Announced, so the failure is as legible as a collector that
            # is merely waiting rather than "died" with no reason.
            self.last_error = str(e)
            self._announce("error: could not open the database")
            return
        try:
            while not self._stop.is_set():
                if not db.recorder_enabled(self._conn):
                    # Someone called stop_recording. The flag is shared, so
                    # every instance honors it, including one started fresh
                    # after a restart.
                    db.release_recorder(self._conn, self._owner)
                    self.holds_recorder = False
                    self.standby_owner = None
                    # Being switched off is not a failure; a stale error
                    # here would send someone after a fault that is not
                    # there.
                    self.last_error = None
                    self._announce("standby (recording is switched off)")
                    if self._stop.wait(self.STANDBY_RETRY_SECONDS):
                        break
                    continue

                claim = db.claim_recorder(self._conn, self._owner)
                if not claim["held"]:
                    # Not an error: another server process is recording,
                    # and this one waits in case that process dies. A
                    # healthy standby must not report an earlier failure.
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
                    # A state rather than a failure: the game is not open
                    # yet.
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
                    # Keep the reason, then go round again.
                    self.last_error = str(e)
                    self.status = "error, retrying"
                finally:
                    self.live = False
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

    def _housekeeping(self) -> str | None:
        """One retention pass, at session start. Returns a stand-down reason.

        A string means STOP: the pass took long enough that another instance
        took the claim, and the caller must leave the loop before writing
        anything, or two collectors record the same laps. That is why the
        heartbeat is forced on the way out.

        Three other guards:

        - Only the claim holder runs it. A standby thinning the database
          underneath the one actually recording is contention for no benefit.
        - The heartbeat is forced either side. A VACUUM over a
          multi-gigabyte file can exceed RECORDER_STALE_SECONDS.
        - Anything raised is recorded and swallowed. Housekeeping must never
          be the reason a session is not recorded.

        It still costs a pause while the car is in the garage, which is why
        it is here and not between two flying laps.
        """
        if not self.holds_recorder:
            return None
        # Cleared on every pass, not only on one that acted, so
        # recording_status reports only the latest pass.
        self.last_retention = None
        try:
            stand_down = self._beat(force=True)
            if stand_down:
                return stand_down
            result = retention.enforce_budget(self._conn, self._db_path)
            if result.get("acted"):
                self.last_retention = result
        except Exception as e:
            self.last_error = f"retention pass failed: {e}"
        try:
            return self._beat(force=True)
        except Exception:
            return None

    def _loop(self, sim, AC_LIVE):
        interval = 1.0 / TARGET_HZ
        session_started = False
        lap_samples: list[tuple] = []
        wide_since = None        # t_ms the current excursion began
        lap_pitted = False       # driver entered the pit lane this lap
        session_best = None      # fastest valid lap so far, for outlier check
        last_completed = None
        lap_start_wall = None
        last_packet = -1
        last_pos = None

        while not self._stop.is_set():
            # Before anything is read, let alone written. This loop does not
            # return to the outer one while a session is live, so this is the
            # only place a lost claim or a switch-off is noticed.
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
                self.live = False
                # Clear the session here, not on the way out of _run:
                # _collector_state reads session_id first, and would report
                # "recording" for a collector that has stopped writing.
                if self.session_id is not None:
                    self.last_session_id = self.session_id
                    self.session_id = None
                return

            g = sim.graphics
            p = sim.physics

            self.live = g.status == AC_LIVE
            if not self.live:
                # Session ended / paused / menu: drop the partial lap.
                if g.status == 0:  # AC_OFF -> back to waiting
                    session_started = False
                    last_completed = None
                    session_best = None
                    lap_samples = []
                    # Not "recording (session 7)" while AC sits in the menus.
                    self._announce("waiting for AC to go live")
                time.sleep(0.25)
                continue

            # Restarting a session from the in-game menu resets completedLaps
            # without passing through AC_OFF. A lap counter that goes
            # backwards can only mean a restart, so roll a fresh session.
            if (session_started and last_completed is not None
                    and g.completedLaps < last_completed):
                session_started = False
                last_completed = None
                session_best = None
                lap_samples = []
                wide_since = None
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
                # The game's own length, so every position can be given in
                # meters even for sessions recorded without the in-game app.
                length = getattr(s, "trackSPlineLength", 0) or 0
                if length >= analysis.MIN_TRACK_LENGTH_M:
                    db.set_fuel_basis(self._conn, self.session_id,
                                      track_length_m=length)
                session_started = True
                # A retention pass can outlast the claim; handled exactly
                # like the beat at the top of the loop.
                stand_down = self._housekeeping()
                if stand_down:
                    self.last_error = (None if stand_down == SWITCHED_OFF
                                       else stand_down)
                    self.status = f"standby ({stand_down})"
                    self.holds_recorder = False
                    if self.session_id is not None:
                        self.last_session_id = self.session_id
                        self.session_id = None
                    return
                last_completed = g.completedLaps
                last_pos = None
                lap_samples = []
                wide_since = None
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
                # The driving after pit exit is real telemetry; only its lap
                # time is not, so an out-lap is stored and flagged.
                if lap_samples:
                    out_lap = lap_time <= 0
                    # store_lap scores track limits from the samples, so the
                    # threshold can change and be re-applied to laps already
                    # driven; current_lap_dirty only feeds the in-game overlay.
                    #
                    # setup_name omitted: store_lap snapshots the session's
                    # current setup, which another instance's
                    # set_session_setup can change.
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
                    # actually driven, clean or not. At a track with tight
                    # limits every lap can be dirty.
                    if (not out_lap and not lap_pitted
                            and (session_best is None
                                 or lap_time < session_best)):
                        session_best = lap_time
                        # Laps already stored were judged against a slower
                        # reference (or none). Re-scoring is a handful of
                        # rows and only happens on a new best.
                        db.backfill_outliers(self._conn, self.session_id)
                last_completed = g.completedLaps
                lap_samples = []
                wide_since = None
                lap_pitted = False
                lap_start_wall = time.monotonic()
                # Forget the previous position too. The lap counter and the
                # position wrap do not land on the same tick, so ~0.98 then
                # ~0.01 would look like a teleport.
                last_pos = None
                self.current_lap_dirty = False
                self.current_lap_pitted = False

            # Did the car leave the lap without finishing it?
            #
            # A crash, a retirement, or a reset to the pits teleports the car
            # backwards down the spline without completedLaps advancing. Keep
            # that lap, marked incomplete: it is often the one the driver
            # most wants to look at. The wrap window keeps a line crossing
            # out of this check when the counter lags a tick.
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
                wide_since = None
                lap_pitted = False
                lap_start_wall = time.monotonic()
                self.current_lap_dirty = False
                self.current_lap_pitted = False
            last_pos = pos

            # A lap containing a pit visit has a meaningless time. Note it
            # before the sampling guard below, which skips pit-lane ticks.
            if g.isInPitLane:
                lap_pitted = True
                self.current_lap_pitted = True

            # New physics tick since last sample?
            if p.packetId != last_packet and not g.isInPitLane:
                last_packet = p.packetId
                t_ms = int((time.monotonic() - lap_start_wall) * 1000)
                # Drives the in-game overlay's "running wide" indicator
                # only; the stored verdict is scored at store time. It uses
                # the same rule, wheel count AND minimum duration, so the
                # overlay and the database agree.
                if p.numberOfTyresOut >= db.TRACK_LIMITS_WHEELS:
                    if wide_since is None:
                        wide_since = t_ms
                    elif t_ms - wide_since >= db.MIN_EXCURSION_MS:
                        self.current_lap_dirty = True
                else:
                    wide_since = None
                self.samples_taken += 1
                # Position, attitude and electronics activity, read through
                # getattr/_seq because a test stub or an older shared-memory
                # layout may not carry them. A missing field records None,
                # not 0: a zero coordinate is the track origin, a place on
                # the map.
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
