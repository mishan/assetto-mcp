"""Assetto Corsa, read through its shared memory or a recording of it."""

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


class Reader:
    """AC's three pages read as Frames: `pages` is a SimInfo, or anything
    with the same physics, graphics and static pages."""

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
            lap_time_ms=g.iCurrentTime,
            norm_pos=g.normalizedCarPosition,
            position=g.position if g.position and g.position > 0 else None,
            race_laps=(g.numberOfLaps if g.numberOfLaps
                       and g.numberOfLaps > 0 else None),
            time_left_ms=left if left and left > 0 else None,
            checkered=g.flag == CHECKERED_FLAG,
            speed_kmh=p.speedKmh, fuel_l=p.fuel,
            air_temp=p.airTemp, road_temp=p.roadTemp,
            tyre_compound=g.tyreCompound,
            pressure_psi=tuple(p.wheelsPressure),
            core_temp_c=tuple(p.tyreCoreTemperature),
            world_pos=tuple(g.carCoordinates),
            clock_text=(g.currentTime, g.lastTime, g.bestTime))

    def session(self) -> SessionInfo:
        s = self._pages.static
        rate = float(s.aidFuelRate)
        usable = 0 <= rate <= MAX_FUEL_RATE
        return SessionInfo(
            car=s.carModel, track=s.track,
            track_config=s.trackConfiguration,
            # maxFuel is the capacity when the car's FUEL setup entry is
            # read-only, and is what lets a fuel plan say whether a stop is
            # forced.
            max_fuel_liters=(round(float(s.maxFuel), 2)
                             if s.maxFuel and s.maxFuel > 0 else None),
            fuel_rate=rate if usable else None,
            fuel_rate_unusable=None if usable else rate)

    def close(self) -> None:
        self._pages.close()
