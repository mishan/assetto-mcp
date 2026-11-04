"""Automobilista 2's shared memory: its page, opened without creating it.

AMS2 publishes one page, in the Project CARS 2 layout, when its Options >
System > Shared Memory is set to "Project CARS 2"; the game keeps that
setting where nothing else can change it. The layout follows the field list
of the SharedMemory.h the game ships, version 14, written out here as ctypes
fields: plain C alignment, no packing.
"""

import ctypes
import sys
from ctypes import c_bool, c_char, c_float, c_int, c_uint

TITLE = "Automobilista 2"
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
