"""Automobilista 2, read through its shared memory or a recording of it.

AMS2 publishes one page, in the Project CARS 2 layout, when its Options >
System > Shared Memory is set to "Project CARS 2"; the game keeps that
setting where nothing else can change it. The layout follows the field list
of the SharedMemory.h the game ships, version 14, written out here as ctypes
fields: plain C alignment, no packing.
"""

import ctypes
import sys
import time
from ctypes import c_bool, c_char, c_float, c_int, c_uint

from .base import Frame, SessionInfo

TITLE = "Automobilista 2"
# Opponents (#107) and setup files (#112) are still to come.
CAPABILITIES: dict = {}
CALIBRATION: dict = {}
MAPPING = "$pcars2$"
VERSION = 14
SETTING = ("Automobilista 2's shared memory is off: set Options > System > "
           "Shared Memory to \"Project CARS 2\"")

STRING = 64
PARTICIPANTS = 64
COMPOUND = 40
VEC = 3
TYRES = 4


class ParticipantInfo(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("mIsActive", c_bool),
        ("mName", c_char * STRING),
        ("mWorldPosition", c_float * VEC),
        ("mCurrentLapDistance", c_float),
        ("mRacePosition", c_uint),
        ("mLapsCompleted", c_uint),
        ("mCurrentLap", c_uint),
        ("mCurrentSector", c_int),
    ]


def _per_tyre(*names, kind=c_float):
    return [(n, kind * TYRES) for n in names]


def _per_car(*names, kind=c_float):
    return [(n, kind * PARTICIPANTS) for n in names]


class SharedMemory(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("mVersion", c_uint), ("mBuildVersionNumber", c_uint),
        ("mGameState", c_uint), ("mSessionState", c_uint),
        ("mRaceState", c_uint),
        ("mViewedParticipantIndex", c_int), ("mNumParticipants", c_int),
        ("mParticipantInfo", ParticipantInfo * PARTICIPANTS),
        ("mUnfilteredThrottle", c_float), ("mUnfilteredBrake", c_float),
        ("mUnfilteredSteering", c_float), ("mUnfilteredClutch", c_float),
        ("mCarName", c_char * STRING), ("mCarClassName", c_char * STRING),
        ("mLapsInEvent", c_uint),
        ("mTrackLocation", c_char * STRING),
        ("mTrackVariation", c_char * STRING),
        ("mTrackLength", c_float),
        ("mNumSectors", c_int), ("mLapInvalidated", c_bool),
        *[(n, c_float) for n in (
            "mBestLapTime", "mLastLapTime", "mCurrentTime",
            "mSplitTimeAhead", "mSplitTimeBehind", "mSplitTime",
            "mEventTimeRemaining", "mPersonalFastestLapTime",
            "mWorldFastestLapTime",
            "mCurrentSector1Time", "mCurrentSector2Time",
            "mCurrentSector3Time",
            "mFastestSector1Time", "mFastestSector2Time",
            "mFastestSector3Time",
            "mPersonalFastestSector1Time", "mPersonalFastestSector2Time",
            "mPersonalFastestSector3Time",
            "mWorldFastestSector1Time", "mWorldFastestSector2Time",
            "mWorldFastestSector3Time")],
        ("mHighestFlagColour", c_uint), ("mHighestFlagReason", c_uint),
        ("mPitMode", c_uint), ("mPitSchedule", c_uint),
        ("mCarFlags", c_uint),
        *[(n, c_float) for n in (
            "mOilTempCelsius", "mOilPressureKPa", "mWaterTempCelsius",
            "mWaterPressureKPa", "mFuelPressureKPa", "mFuelLevel",
            "mFuelCapacity", "mSpeed", "mRpm", "mMaxRPM", "mBrake",
            "mThrottle", "mClutch", "mSteering")],
        ("mGear", c_int), ("mNumGears", c_int),
        ("mOdometerKM", c_float), ("mAntiLockActive", c_bool),
        ("mLastOpponentCollisionIndex", c_int),
        ("mLastOpponentCollisionMagnitude", c_float),
        ("mBoostActive", c_bool), ("mBoostAmount", c_float),
        *[(n, c_float * VEC) for n in (
            "mOrientation", "mLocalVelocity", "mWorldVelocity",
            "mAngularVelocity", "mLocalAcceleration", "mWorldAcceleration",
            "mExtentsCentre")],
        *_per_tyre("mTyreFlags", "mTerrain", kind=c_uint),
        *_per_tyre(
            "mTyreY", "mTyreRPS", "mTyreSlipSpeed", "mTyreTemp",
            "mTyreGrip", "mTyreHeightAboveGround", "mTyreLateralStiffness",
            "mTyreWear", "mBrakeDamage", "mSuspensionDamage",
            "mBrakeTempCelsius", "mTyreTreadTemp", "mTyreLayerTemp",
            "mTyreCarcassTemp", "mTyreRimTemp", "mTyreInternalAirTemp"),
        ("mCrashState", c_uint),
        ("mAeroDamage", c_float), ("mEngineDamage", c_float),
        *[(n, c_float) for n in (
            "mAmbientTemperature", "mTrackTemperature", "mRainDensity",
            "mWindSpeed", "mWindDirectionX", "mWindDirectionY",
            "mCloudBrightness")],
        # Odd while the game is writing the page, even when it is not.
        ("mSequenceNumber", c_uint),
        *_per_tyre("mWheelLocalPositionY", "mSuspensionTravel",
                   "mSuspensionVelocity", "mAirPressure"),
        ("mEngineSpeed", c_float), ("mEngineTorque", c_float),
        ("mWings", c_float * 2), ("mHandBrake", c_float),
        *_per_car(
            "mCurrentSector1Times", "mCurrentSector2Times",
            "mCurrentSector3Times", "mFastestSector1Times",
            "mFastestSector2Times", "mFastestSector3Times",
            "mFastestLapTimes", "mLastLapTimes"),
        *_per_car("mLapsInvalidated", kind=c_bool),
        *_per_car("mRaceStates", "mPitModes", kind=c_uint),
        ("mOrientations", (c_float * VEC) * PARTICIPANTS),
        *_per_car("mSpeeds"),
        ("mCarNames", (c_char * STRING) * PARTICIPANTS),
        ("mCarClassNames", (c_char * STRING) * PARTICIPANTS),
        ("mEnforcedPitStopLap", c_int),
        ("mTranslatedTrackLocation", c_char * STRING),
        ("mTranslatedTrackVariation", c_char * STRING),
        ("mBrakeBias", c_float), ("mTurboBoostPressure", c_float),
        ("mTyreCompound", (c_char * COMPOUND) * TYRES),
        *_per_car("mPitSchedules", "mHighestFlagColours",
                  "mHighestFlagReasons", "mNationalities", kind=c_uint),
        ("mSnowDensity", c_float),
        ("mSessionDuration", c_float), ("mSessionAdditionalLaps", c_int),
        *_per_tyre("mTyreTempLeft", "mTyreTempCenter", "mTyreTempRight"),
        ("mDrsState", c_uint),
        *_per_tyre("mRideHeight"),
        ("mJoyPad0", c_uint), ("mDPad", c_uint),
        ("mAntiLockSetting", c_int), ("mTractionControlSetting", c_int),
        ("mErsDeploymentMode", c_int), ("mErsAutoModeEnabled", c_bool),
        ("mClutchTemp", c_float), ("mClutchWear", c_float),
        ("mClutchOverheated", c_bool), ("mClutchSlipping", c_bool),
        ("mYellowFlagState", c_int), ("mSessionIsPrivate", c_bool),
        ("mLaunchStage", c_int),
    ]


SIZE = ctypes.sizeof(SharedMemory)
_SEQ = SharedMemory.mSequenceNumber.offset
# A read that lands while the game writes is retried; it writes once a
# graphics frame, so a handful of tries always finds a quiet moment.
TORN_READ_TRIES = 50


def stable_copy(read) -> bytes | None:
    """The page as one write left it, from `read()`, which returns its bytes
    as they are now; None if every try caught the game mid-write."""
    for _ in range(TORN_READ_TRIES):
        data = read()
        seq = int.from_bytes(data[_SEQ:_SEQ + 4], "little")
        if seq % 2 == 0 and read()[_SEQ:_SEQ + 4] == data[_SEQ:_SEQ + 4]:
            return data
    return None


def decode(raw: dict[str, bytes]) -> SharedMemory:
    return SharedMemory.from_buffer_copy(raw["shared"])


class Pages:
    """AMS2's page, opened only if the game has made it.

    Opening a mapping by name with mmap would create an empty one when the
    game has not, and a game started later might then fail to map its own.
    """

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Automobilista 2's shared memory is only "
                               "available on Windows")
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingW.restype = wintypes.HANDLE
        k32.OpenFileMappingW.argtypes = (wintypes.DWORD, wintypes.BOOL,
                                         wintypes.LPCWSTR)
        k32.MapViewOfFile.restype = wintypes.LPVOID
        k32.MapViewOfFile.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                      wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_size_t)
        k32.UnmapViewOfFile.argtypes = (wintypes.LPCVOID,)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._k32 = k32
        file_map_read = 0x0004
        self._handle = k32.OpenFileMappingW(file_map_read, False, MAPPING)
        if not self._handle:
            raise RuntimeError(f"Automobilista 2 is not running, or {SETTING}")
        self._view = k32.MapViewOfFile(self._handle, file_map_read, 0, 0,
                                       SIZE)
        if not self._view:
            self.close()
            raise RuntimeError("Automobilista 2's shared memory could not "
                               f"be read (error {ctypes.get_last_error()})")
        version = self.page().mVersion
        if version != VERSION:
            self.close()
            raise RuntimeError(f"Automobilista 2's shared memory is version "
                               f"{version}; this reads version {VERSION}")

    def raw(self) -> dict[str, bytes]:
        data = stable_copy(lambda: ctypes.string_at(self._view, SIZE))
        if data is None:
            raise RuntimeError("Automobilista 2's shared memory never held "
                               "still long enough to read")
        return {"shared": data}

    def page(self) -> SharedMemory:
        return decode(self.raw())

    def close(self) -> None:
        if getattr(self, "_view", None):
            self._k32.UnmapViewOfFile(self._view)
            self._view = None
        if getattr(self, "_handle", None):
            self._k32.CloseHandle(self._handle)
            self._handle = None


def scrub(raw: dict[str, bytes]) -> dict[str, bytes]:
    """The page with every driver's name replaced by "Driver N"."""
    page = decode(raw)
    for i, p in enumerate(page.mParticipantInfo):
        p.mName = f"Driver {i + 1}".encode() if p.mIsActive else b""
    return {"shared": bytes(page)}


def describe(raw: dict[str, bytes]) -> dict:
    page = decode(raw)
    return {"car": page.mCarName.decode(errors="replace"),
            "track": page.mTrackLocation.decode(errors="replace"),
            "track_config": page.mTrackVariation.decode(errors="replace"),
            "game_version": [page.mVersion, page.mBuildVersionNumber]}


class Snapshot:
    """One page as a recording holds it, with what Pages offers a Reader."""

    def __init__(self, page: SharedMemory):
        self._page = page

    def page(self) -> SharedMemory:
        return self._page

    def close(self) -> None:
        pass


def pages_from_raw(raw: dict[str, bytes]) -> Snapshot:
    return Snapshot(decode(raw))


# The enums, from SharedMemory.h.
STATUS = {0: "off", 1: "off", 2: "live", 3: "pause", 4: "pause", 5: "off",
          6: "replay", 7: "replay"}
GAME_RESTARTING = 5
SESSION = {1: "practice", 2: "test", 3: "qualifying", 4: "formation lap",
           5: "race", 6: "time attack"}
RACE_FINISHED = 3
# Surfaces a wheel can be on and still be on the track: road of every kind,
# marbles, kerbs and drains. 48, newer than the published header, is the
# garage floor and painted areas; 49, always found between a kerb and the
# gravel, is not on the list.
ON_TRACK = {0, 1, 2, 3, 4, 5, 10, 11, 21, 25, 29, 35, 37, 40, 41, 48}

KPA_TO_PSI = 0.1450377
G = 9.80665
# A position this close to the line on each side, from one read to the
# next, is the car crossing it.
WRAP_HIGH, WRAP_LOW = 0.9, 0.1
# Reads apart that the timer starting and the car crossing the line still
# count as one event. They landed on the same read in every recording so
# far, but AC's lap counter and position are known to land a tick apart. A
# standing start, whose timer starts on the grid a second or more before
# the car reaches the line, stays well outside it.
CROSSING_READS = 2


def _text(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def _clock(seconds: float) -> str:
    """A lap time as AC prints one, which is what clock_text carries."""
    if seconds <= 0:
        return "-:--:---"
    ms = round(seconds * 1000)
    return f"{ms // 60000}:{ms // 1000 % 60:02d}:{ms % 1000:03d}"


def _ms(seconds: float) -> int:
    # round(): the game keeps times as float32, and 60.019 truncates to
    # 60018.
    return round(seconds * 1000) if seconds > 0 else 0


class Reader:
    """AMS2's page read as Frames: `pages` is a Pages, or anything with its
    page() and close().

    AMS2 does not count the out-lap: leaving the garage, the first crossing
    of the line starts the lap timer without advancing mLapsCompleted. The
    collector, like AC, needs every crossing counted and the out-lap given
    no time, so the Reader counts those crossings itself. That makes it
    stateful, one per run of the collector; a Reader made for a single read
    gives the game's own lap count.
    """

    name = "ams2"

    def __init__(self, pages):
        self._pages = pages
        self._reset()
        self._session_state = None

    def _reset(self) -> None:
        self._player = None
        self._out_laps = 0
        self._laps = None
        self._time = None
        self._pos = None
        # mLapsCompleted's value while the lap just finished is an out-lap,
        # whose time the game leaves at the previous timed lap's.
        self._untimed_at = None
        self._reads = 0
        # The read on which the car last crossed the line, and on which the
        # lap timer last started, with mLapsCompleted before and after.
        self._wrapped = None
        self._started = None

    def read(self) -> Frame:
        s = self._pages.page()
        if (s.mGameState == GAME_RESTARTING
                or s.mSessionState != self._session_state):
            self._reset()
            self._session_state = s.mSessionState
        viewed = s.mViewedParticipantIndex
        count = min(s.mNumParticipants, PARTICIPANTS)
        if ((self._player is None or self._player >= count)
                and STATUS.get(s.mGameState) in ("live", "pause")):
            # Latched: a view switched to another car must not move the lap
            # count to that car's. A replay may be following anyone.
            self._player = viewed if 0 <= viewed < count else None
        # In the menus there is no car and no track: the index is -1, which
        # ctypes would wrap to the last car, and the length 0.
        ready = self._player is not None and s.mTrackLength > 0
        p = s.mParticipantInfo[self._player if ready else 0]
        pos = p.mCurrentLapDistance / s.mTrackLength if ready else 0.0
        laps, clock = p.mLapsCompleted, s.mCurrentTime
        if self._laps is not None and laps < self._laps:
            player = self._player
            self._reset()
            self._player = player
        self._reads += 1
        if (self._pos is not None and self._pos > WRAP_HIGH
                and pos < WRAP_LOW):
            self._wrapped = (self._reads, self._laps, laps)
        if self._time is not None and self._time < 0 <= clock:
            self._started = (self._reads, self._laps, laps)
        # The timer starting as the car crosses the line, with the game's
        # count unmoved throughout, is an out-lap ending; a crossing the game
        # counted itself is not. Both are cleared once counted.
        if (self._wrapped and self._started
                and abs(self._wrapped[0] - self._started[0]) <= CROSSING_READS
                and len({*self._wrapped[1:], *self._started[1:], laps}) == 1):
            self._out_laps += 1
            self._untimed_at = laps
            self._wrapped = self._started = None
        elif self._untimed_at is not None and laps != self._untimed_at:
            self._untimed_at = None
        self._laps, self._time, self._pos = laps, clock, pos

        o, acc = s.mOrientation, s.mLocalAcceleration
        tank = s.mFuelCapacity
        heights = s.mRideHeight
        return Frame(
            status=(STATUS.get(s.mGameState, f"unknown ({s.mGameState})")
                    if ready else "off"),
            session_type=SESSION.get(s.mSessionState,
                                     f"unknown ({s.mSessionState})"),
            completed_laps=laps + self._out_laps,
            last_lap_ms=(0 if self._untimed_at is not None
                         else _ms(s.mLastLapTime)),
            lap_time_ms=_ms(clock),
            norm_pos=pos,
            in_pit_lane=s.mPitMode != 0,
            tick=s.mSequenceNumber,
            position=p.mRacePosition if p.mRacePosition > 0 else None,
            race_laps=s.mLapsInEvent if s.mLapsInEvent > 0 else None,
            time_left_ms=(s.mEventTimeRemaining * 1000
                          if s.mEventTimeRemaining > 0 else None),
            checkered=s.mRaceState == RACE_FINISHED,
            speed_kmh=s.mSpeed * 3.6,
            gas=s.mThrottle, brake=s.mBrake, steer=s.mSteering,
            gear=s.mGear, rpm=round(s.mRpm),
            # AMS2's car frame has z pointing backward and its world is
            # mirrored from AC's along z; these match AC's signs.
            acc_lat=acc[0] / G, acc_lon=-acc[2] / G,
            slip=tuple(s.mTyreSlipSpeed),
            pressure_psi=tuple(k * KPA_TO_PSI for k in s.mAirPressure),
            core_temp_c=tuple(k - 273.15 for k in s.mTyreCarcassTemp),
            ride_height_m=((heights[0] + heights[1]) / 200,
                           (heights[2] + heights[3]) / 200),
            tyres_out=sum(t not in ON_TRACK for t in s.mTerrain),
            world_pos=(p.mWorldPosition[0], p.mWorldPosition[1],
                       -p.mWorldPosition[2]),
            heading=o[1], pitch=-o[0], roll=-o[2],
            tc_active=None, abs_active=float(s.mAntiLockActive),
            wear=tuple(100 * (1 - w) for w in s.mTyreWear),
            # mEngineDamage creeps up with wear on a clean lap; bodywork is
            # what a contact shows in.
            damage=s.mAeroDamage + sum(s.mSuspensionDamage),
            fuel_l=s.mFuelLevel * tank if tank > 0 else None,
            air_temp=s.mAmbientTemperature, road_temp=s.mTrackTemperature,
            tyre_compound=_text(s.mTyreCompound[0].value),
            clock_text=(_clock(clock),
                        _clock(0 if self._untimed_at is not None
                               else s.mLastLapTime),
                        _clock(s.mBestLapTime)))

    def session(self) -> SessionInfo:
        s = self._pages.page()
        return SessionInfo(
            car=_text(s.mCarName), track=_text(s.mTrackLocation),
            track_config=_text(s.mTrackVariation),
            track_length_m=s.mTrackLength or None,
            max_fuel_liters=(round(s.mFuelCapacity, 2)
                             if s.mFuelCapacity > 0 else None))

    def now(self) -> float:
        return getattr(self._pages, "now", time.monotonic)()

    def wait(self, seconds: float) -> None:
        getattr(self._pages, "wait", time.sleep)(seconds)

    def close(self) -> None:
        self._pages.close()


def live() -> Reader:
    """Automobilista 2's shared memory, as the game writes it."""
    return Reader(Pages())
