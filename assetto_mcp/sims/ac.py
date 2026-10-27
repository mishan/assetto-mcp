"""Assetto Corsa, read through its shared memory or a recording of it."""

import time

from .base import Frame, SessionInfo

STATUS = {0: "off", 1: "replay", 2: "live", 3: "pause"}

# AC's session types, from the shared-memory documentation.
SESSION = {0: "practice", 1: "qualifying", 2: "race", 3: "hotlap",
           4: "time attack", 5: "drift", 6: "drag"}

# AC's flag enum; only the checkered flag is read.
CHECKERED_FLAG = 5

# AC reports the fuel-usage assist as a multiplier, 1.0 being 100%. Anything
# past this is a misread page or an unconverted percentage; scaling a fuel
# plan by 100 is far worse than reporting the rate as unknown, so
# out-of-range values are handed back raw for the caller to report.
MAX_FUEL_RATE = 10.0

TITLE = "Assetto Corsa"

# What the server can do in this game beyond recording laps, and what
# provides it: "app" is the in-game app, missing while it is not running;
# "files" are files the game reads.
CAPABILITIES = {"opponents": "app", "suspension": "app",
                "setup_values": "app", "messages": "app",
                "setup_files": "files", "track_sections": "files"}

# Thresholds that differ from the defaults (sims.calibration); none do, as
# the defaults were tuned on AC.
CALIBRATION: dict = {}


# Reads of AC's fixed-size arrays go through getattr, because an older
# shared-memory layout or a test stub may not carry the field, and then check
# its length. They run inside the sampling loop: an IndexError there ends the
# recording session, so a layout one element short would cost every lap of
# the run rather than one nullable field.
WHEELS_PER_CAR = 4
DAMAGE_ZONES = 5
WORLD_AXES = 3


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


class Reader:
    """AC's three pages read as Frames: `pages` is a SimInfo, or anything
    with the same physics, graphics and static pages."""

    name = "ac"

    def __init__(self, pages):
        self._pages = pages

    def read(self) -> Frame:
        p, g = self._pages.physics, self._pages.graphics
        left = g.sessionTimeLeft
        return Frame(
            # Mapped, not indexed: a partial or corrupt read gives an
            # out-of-range value, and an IndexError would take down exactly
            # the call made to find out whether shared memory reads at all.
            status=STATUS.get(g.status, f"unknown ({g.status})"),
            session_type=SESSION.get(g.session, f"unknown ({g.session})"),
            completed_laps=g.completedLaps,
            last_lap_ms=g.iLastTime,
            lap_time_ms=g.iCurrentTime,
            norm_pos=g.normalizedCarPosition,
            in_pit_lane=bool(g.isInPitLane),
            tick=p.packetId,
            position=g.position if g.position and g.position > 0 else None,
            race_laps=(g.numberOfLaps if g.numberOfLaps
                       and g.numberOfLaps > 0 else None),
            time_left_ms=left if left and left > 0 else None,
            checkered=g.flag == CHECKERED_FLAG,
            speed_kmh=p.speedKmh, gas=p.gas, brake=p.brake,
            steer=p.steerAngle,
            gear=p.gear - 1,  # AC counts 0 as reverse; shift to match the HUD
            rpm=p.rpms,
            acc_lat=p.accG[0], acc_lon=p.accG[2],
            slip=tuple(p.wheelSlip[i] for i in range(WHEELS_PER_CAR)),
            pressure_psi=tuple(p.wheelsPressure[i]
                               for i in range(WHEELS_PER_CAR)),
            core_temp_c=tuple(p.tyreCoreTemperature[i]
                              for i in range(WHEELS_PER_CAR)),
            ride_height_m=(p.rideHeight[0], p.rideHeight[1]),
            tyres_out=p.numberOfTyresOut,
            world_pos=_seq(g, "carCoordinates", WORLD_AXES),
            heading=getattr(p, "heading", None),
            pitch=getattr(p, "pitch", None),
            roll=getattr(p, "roll", None),
            tc_active=getattr(p, "tc", None),
            abs_active=getattr(p, "abs", None),
            wear=_wear(p), damage=_damage(p),
            fuel_l=getattr(p, "fuel", None),
            air_temp=p.airTemp, road_temp=p.roadTemp,
            tyre_compound=g.tyreCompound,
            clock_text=(g.currentTime, g.lastTime, g.bestTime))

    def session(self) -> SessionInfo:
        s = self._pages.static
        rate = float(s.aidFuelRate)
        usable = 0 <= rate <= MAX_FUEL_RATE
        return SessionInfo(
            car=s.carModel, track=s.track,
            track_config=s.trackConfiguration,
            track_length_m=getattr(s, "trackSPlineLength", 0) or None,
            # maxFuel is the capacity when the car's FUEL setup entry is
            # read-only, and is what lets a fuel plan say whether a stop is
            # forced.
            max_fuel_liters=(round(float(s.maxFuel), 2)
                             if s.maxFuel and s.maxFuel > 0 else None),
            fuel_rate=rate if usable else None,
            fuel_rate_unusable=None if usable else rate)

    def now(self) -> float:
        """The game's clock: a replay keeps its own, so it is the same every
        run and need not take as long as the driving did."""
        return getattr(self._pages, "now", time.monotonic)()

    def wait(self, seconds: float) -> None:
        getattr(self._pages, "wait", time.sleep)(seconds)

    def close(self) -> None:
        self._pages.close()
