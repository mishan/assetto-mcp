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
from ctypes import c_float, c_int32, c_uint16
from types import SimpleNamespace

AC_OFF = 0
AC_REPLAY = 1
AC_LIVE = 2
AC_PAUSE = 3

# Wheel index order everywhere in AC: FL, FR, RL, RR
WHEELS = ("fl", "fr", "rl", "rr")


def _text_fields(cls):
    """Text the way AC writes it: UTF-16, two bytes a character.

    Declared as 16-bit units, not c_wchar, which is four bytes on Linux: a
    recording made on Windows has to read back anywhere. Each `_name` field
    is read and set as the string `name`.
    """
    def field(name, length):
        def get(self):
            raw = bytes(getattr(self, name)).decode("utf-16-le", "replace")
            return raw.split("\0", 1)[0]

        def put(self, value):
            units = value.encode("utf-16-le")[:2 * (length - 1)]
            buf = (c_uint16 * length)()
            ctypes.memmove(buf, units, len(units))
            setattr(self, name, buf)
        return property(get, put)

    for name, typ in cls._fields_:
        if name.startswith("_") and getattr(typ, "_type_", None) is c_uint16:
            setattr(cls, name[1:], field(name, typ._length_))
    return cls


class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    # The MSVC layout these structs mirror. With _pack_ set and no _layout_,
    # CPython warns from 3.14 and errors from 3.19; inert on older versions.
    _layout_ = "ms"
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


@_text_fields
class SPageFileGraphic(ctypes.Structure):
    _pack_ = 4
    _layout_ = "ms"
    _fields_ = [
        ("packetId", c_int32),
        ("status", c_int32),  # AC_OFF / AC_REPLAY / AC_LIVE / AC_PAUSE
        ("session", c_int32),
        ("_currentTime", c_uint16 * 15),
        ("_lastTime", c_uint16 * 15),
        ("_bestTime", c_uint16 * 15),
        ("_split", c_uint16 * 15),
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
        ("_tyreCompound", c_uint16 * 33),
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


@_text_fields
class SPageFileStatic(ctypes.Structure):
    _pack_ = 4
    _layout_ = "ms"
    _fields_ = [
        ("_smVersion", c_uint16 * 15),
        ("_acVersion", c_uint16 * 15),
        ("numberOfSessions", c_int32),
        ("numCars", c_int32),
        ("_carModel", c_uint16 * 33),
        ("_track", c_uint16 * 33),
        ("_playerName", c_uint16 * 33),
        ("_playerSurname", c_uint16 * 33),
        ("_playerNick", c_uint16 * 33),
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
        ("_trackConfiguration", c_uint16 * 33),
        ("ersMaxJ", c_float),
    ]


PAGES = {"physics": SPageFilePhysics, "graphics": SPageFileGraphic,
         "static": SPageFileStatic}
# Who drove: blanked in a recording unless its maker keeps them.
PLAYER_FIELDS = ("playerName", "playerSurname", "playerNick")


def from_raw(raw: dict[str, bytes]) -> SimpleNamespace:
    """The three pages, as SimInfo shows them, from their raw bytes."""
    return SimpleNamespace(**{name: cls.from_buffer_copy(raw[name])
                              for name, cls in PAGES.items()})


def scrub(static: bytes) -> bytes:
    page = SPageFileStatic.from_buffer_copy(static)
    for field in PLAYER_FIELDS:
        setattr(page, field, "")
    return bytes(page)


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


def page_is_live(static) -> bool:
    """Has AC actually written this static page, or is it freshly zeroed?

    A function of its own so it can be tested off Windows, where SimInfo
    refuses to construct. smVersion is written once when the session starts
    and never blank in a running game, so an empty one means Windows
    created this mapping on our behalf and nothing has written to it.
    """
    return bool(getattr(static, "smVersion", "").strip())


class SharedMemoryUnavailable(RuntimeError):
    """AC's shared memory could not be read as a running session.

    A RuntimeError like the not-on-Windows refusal, so callers that treat
    `except RuntimeError` as "no live data" handle both the same way.
    """


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

        # Opening the mapping is not evidence that AC is running:
        # mmap.mmap(-1, size, tag) on Windows *creates* a zeroed mapping when
        # nothing owns that name. Zeros read as plausible values -- an
        # aidFuelRate of 0.0 is a real "burns nothing" league setting.
        if not page_is_live(self._static.data):
            self.close()
            raise SharedMemoryUnavailable(
                "Assetto Corsa's shared memory is empty -- the game does "
                "not appear to be running. (The mapping opened, but Windows "
                "creates it on demand, so that proves nothing.)")

    def raw(self) -> dict[str, bytes]:
        """Each page's bytes as they are now, for a recording."""
        return {"physics": self._physics._mm[:],
                "graphics": self._graphics._mm[:],
                "static": self._static._mm[:]}

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


# AC reports the fuel-usage assist as a multiplier, 1.0 being 100%. Anything
# past this is a misread page or an unconverted percentage; scaling a fuel
# plan by 100 is far worse than reporting the rate as unknown, so
# out-of-range values are handed back raw for the caller to report.
MAX_FUEL_RATE = 10.0


def fuel_facts(open_sim=None) -> dict:
    """Tank capacity and the session's fuel-usage multiplier.

    maxFuel is the capacity when the car's FUEL setup entry is read-only,
    and is what lets a fuel plan say whether a stop is forced. aidFuelRate
    is the multiplier a league session changes: at 50% or 200% every
    liter-per-lap figure moves by that factor.

    Keys are absent rather than guessed when the game gives a value that
    cannot be one, and `fuel_rate` of 0 is a real setting -- a session that
    burns nothing -- so callers must test it against None, not for truth.
    """
    sim = (open_sim or SimInfo)()
    try:
        s = sim.static
        out: dict = {}
        if s.maxFuel and s.maxFuel > 0:
            out["max_fuel_liters"] = round(float(s.maxFuel), 2)
        rate = float(s.aidFuelRate)
        if 0 <= rate <= MAX_FUEL_RATE:
            out["fuel_rate"] = rate
        else:
            out["fuel_rate_unusable"] = rate
        return out
    finally:
        # Release the ctypes view before closing the mapping, as
        # live_snapshot does: from_buffer() keeps an export on the mmap.
        s = None
        sim.close()
