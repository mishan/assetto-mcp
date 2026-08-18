"""Shared memory reader for original Assetto Corsa (not Competizione).

AC exposes three named shared memory pages on Windows:
  Local\\acpmf_physics  - updated every physics tick
  Local\\acpmf_graphics - session/lap/timing state
  Local\\acpmf_static   - car/track constants, set once per session

Struct layouts follow the official Kunos shared memory reference for AC 1.16.
Field order matters; do not reorder.
"""

import ctypes
import mmap
import sys
from ctypes import c_float, c_int32, c_wchar

AC_OFF = 0
AC_REPLAY = 1
AC_LIVE = 2
AC_PAUSE = 3

# Wheel index order everywhere in AC: FL, FR, RL, RR
WHEELS = ("fl", "fr", "rl", "rr")


class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", c_int32),
        ("gas", c_float),
        ("brake", c_float),
        ("fuel", c_float),
        ("gear", c_int32),  # 0=R, 1=N, 2=1st...
        ("rpms", c_int32),
        ("steerAngle", c_float),
        ("speedKmh", c_float),
        ("velocity", c_float * 3),
        ("accG", c_float * 3),
        ("wheelSlip", c_float * 4),
        ("wheelLoad", c_float * 4),
        ("wheelsPressure", c_float * 4),
        ("wheelAngularSpeed", c_float * 4),
        ("tyreWear", c_float * 4),
        ("tyreDirtyLevel", c_float * 4),
        ("tyreCoreTemperature", c_float * 4),
        ("camberRAD", c_float * 4),
        ("suspensionTravel", c_float * 4),
        ("drs", c_float),
        ("tc", c_float),
        ("heading", c_float),
        ("pitch", c_float),
        ("roll", c_float),
        ("cgHeight", c_float),
        ("carDamage", c_float * 5),
        ("numberOfTyresOut", c_int32),
        ("pitLimiterOn", c_int32),
        ("abs", c_float),
        ("kersCharge", c_float),
        ("kersInput", c_float),
        ("autoShifterOn", c_int32),
        ("rideHeight", c_float * 2),  # front, rear
        ("turboBoost", c_float),
        ("ballast", c_float),
        ("airDensity", c_float),
        ("airTemp", c_float),
        ("roadTemp", c_float),
        ("localAngularVel", c_float * 3),
        ("finalFF", c_float),
        ("performanceMeter", c_float),
        ("engineBrake", c_int32),
        ("ersRecoveryLevel", c_int32),
        ("ersPowerLevel", c_int32),
        ("ersHeatCharging", c_int32),
        ("ersIsCharging", c_int32),
        ("kersCurrentKJ", c_float),
        ("drsAvailable", c_int32),
        ("drsEnabled", c_int32),
        ("brakeTemp", c_float * 4),
        ("clutch", c_float),
        ("tyreTempI", c_float * 4),
        ("tyreTempM", c_float * 4),
        ("tyreTempO", c_float * 4),
        ("isAIControlled", c_int32),
        ("tyreContactPoint", (c_float * 3) * 4),
        ("tyreContactNormal", (c_float * 3) * 4),
        ("tyreContactHeading", (c_float * 3) * 4),
        ("brakeBias", c_float),
        ("localVelocity", c_float * 3),
    ]


class SPageFileGraphic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", c_int32),
        ("status", c_int32),  # AC_OFF / AC_REPLAY / AC_LIVE / AC_PAUSE
        ("session", c_int32),
        ("currentTime", c_wchar * 15),
        ("lastTime", c_wchar * 15),
        ("bestTime", c_wchar * 15),
        ("split", c_wchar * 15),
        ("completedLaps", c_int32),
        ("position", c_int32),
        ("iCurrentTime", c_int32),  # ms
        ("iLastTime", c_int32),  # ms
        ("iBestTime", c_int32),  # ms
        ("sessionTimeLeft", c_float),
        ("distanceTraveled", c_float),
        ("isInPit", c_int32),
        ("currentSectorIndex", c_int32),
        ("lastSectorTime", c_int32),
        ("numberOfLaps", c_int32),
        ("tyreCompound", c_wchar * 33),
        ("replayTimeMultiplier", c_float),
        ("normalizedCarPosition", c_float),  # 0.0 -> 1.0 along track spline
        ("carCoordinates", c_float * 3),
        ("penaltyTime", c_float),
        ("flag", c_int32),
        ("idealLineOn", c_int32),
        ("isInPitLane", c_int32),
        ("surfaceGrip", c_float),
        ("mandatoryPitDone", c_int32),
    ]


class SPageFileStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion", c_wchar * 15),
        ("acVersion", c_wchar * 15),
        ("numberOfSessions", c_int32),
        ("numCars", c_int32),
        ("carModel", c_wchar * 33),
        ("track", c_wchar * 33),
        ("playerName", c_wchar * 33),
        ("playerSurname", c_wchar * 33),
        ("playerNick", c_wchar * 33),
        ("sectorCount", c_int32),
        ("maxTorque", c_float),
        ("maxPower", c_float),
        ("maxRpm", c_int32),
        ("maxFuel", c_float),
        ("suspensionMaxTravel", c_float * 4),
        ("tyreRadius", c_float * 4),
        ("maxTurboBoost", c_float),
        ("deprecated_1", c_float),
        ("deprecated_2", c_float),
        ("penaltiesEnabled", c_int32),
        ("aidFuelRate", c_float),
        ("aidTireRate", c_float),
        ("aidMechanicalDamage", c_float),
        ("allowTyreBlankets", c_float),
        ("aidStability", c_float),
        ("aidAutoClutch", c_int32),
        ("aidAutoBlip", c_int32),
        ("hasDRS", c_int32),
        ("hasERS", c_int32),
        ("hasKERS", c_int32),
        ("kersMaxJ", c_float),
        ("engineBrakeSettingsCount", c_int32),
        ("ersPowerControllerCount", c_int32),
        ("trackSPlineLength", c_float),
        ("trackConfiguration", c_wchar * 33),
        ("ersMaxJ", c_float),
    ]


class _Page:
    """One mmap'd shared memory page, viewed through a ctypes struct."""

    def __init__(self, tag: str, struct_cls: type):
        # Windows page granularity means the mapping AC creates is >= 4KB,
        # comfortably larger than any of these structs, so requesting
        # sizeof(struct) always fits inside the existing mapping.
        self._mm = mmap.mmap(-1, ctypes.sizeof(struct_cls), tag)
        self.data = struct_cls.from_buffer(self._mm)

    def close(self):
        # from_buffer() registers an export on the mmap, so mmap.close()
        # raises BufferError while *any* reference to the struct is alive --
        # including a caller's local, or a frame pinned by a live traceback.
        # Drop our own reference; if others remain, leave the mapping to be
        # reclaimed by refcounting rather than blowing up the caller.
        self.data = None
        try:
            self._mm.close()
        except BufferError:
            pass


class SimInfo:
    """Handle to all three AC shared memory pages."""

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError(
                "AC shared memory is only available on Windows "
                "(this must run on the machine running Assetto Corsa)."
            )
        self._physics = _Page("Local\\acpmf_physics", SPageFilePhysics)
        self._graphics = _Page("Local\\acpmf_graphics", SPageFileGraphic)
        self._static = _Page("Local\\acpmf_static", SPageFileStatic)

    @property
    def physics(self) -> SPageFilePhysics:
        return self._physics.data

    @property
    def graphics(self) -> SPageFileGraphic:
        return self._graphics.data

    @property
    def static(self) -> SPageFileStatic:
        return self._static.data

    def close(self):
        self._physics.close()
        self._graphics.close()
        self._static.close()
