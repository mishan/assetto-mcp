"""What a game's reader gives the rest of the server.

Every game's data reaches the tools in these shapes, in one set of units:
km/h, liters, °C, psi, meters, milliseconds. A reader converts on the way in
so nothing past it knows which game it is reading.
"""

from dataclasses import dataclass

WHEELS = ("fl", "fr", "rl", "rr")


@dataclass(frozen=True)
class SessionInfo:
    """What holds for the whole session: the car, the track, the tank."""
    car: str
    track: str
    track_config: str = ""
    max_fuel_liters: float | None = None
    # The fuel-usage multiplier, 1.0 for normal; `fuel_rate_unusable` holds
    # a reading that cannot be one, rather than a guess.
    fuel_rate: float | None = None
    fuel_rate_unusable: float | None = None


@dataclass(frozen=True)
class Frame:
    """One reading of the game, as plain values that outlive the reader."""
    status: str               # "off", "replay", "live", "pause", "unknown (n)"
    session_type: str
    completed_laps: int
    lap_time_ms: int          # the lap being driven, so far
    norm_pos: float           # 0..1 along the lap
    position: int | None
    race_laps: int | None     # None in a timed session
    time_left_ms: float | None
    checkered: bool
    speed_kmh: float
    fuel_l: float
    air_temp: float
    road_temp: float
    tyre_compound: str
    pressure_psi: tuple[float, ...]   # in WHEELS order
    core_temp_c: tuple[float, ...]
    world_pos: tuple[float, float, float]
    # The game's own current, last and best lap clocks, as it prints them.
    clock_text: tuple[str, str, str]
