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
    track_length_m: float | None = None
    max_fuel_liters: float | None = None
    # The fuel-usage multiplier, 1.0 for normal; `fuel_rate_unusable` holds
    # a reading that cannot be one, rather than a guess.
    fuel_rate: float | None = None
    fuel_rate_unusable: float | None = None


@dataclass(frozen=True)
class Frame:
    """One reading of the game, as plain values that outlive the reader.

    None is "this game, or this layout of it, does not report it", never a
    stand-in number: a zero coordinate is a place on the map.
    """
    status: str               # "off", "replay", "live", "pause", "unknown (n)"
    session_type: str
    completed_laps: int
    last_lap_ms: int          # the game's time for the last lap; 0 for none
    lap_time_ms: int          # the lap being driven, so far
    norm_pos: float           # 0..1 along the lap
    in_pit_lane: bool
    tick: int                 # changes when the physics has moved on
    position: int | None
    race_laps: int | None     # None in a timed session
    time_left_ms: float | None
    checkered: bool
    speed_kmh: float
    gas: float                # pedals 0..1
    brake: float
    steer: float              # -1..1
    gear: int                 # 0 neutral, -1 reverse, 1 first
    rpm: int
    acc_lat: float            # g
    acc_lon: float
    slip: tuple               # each per wheel, in WHEELS order
    pressure_psi: tuple
    core_temp_c: tuple
    ride_height_m: tuple      # front, rear
    tyres_out: int
    world_pos: tuple | None   # x, y, z in meters
    heading: float | None     # radians
    pitch: float | None
    roll: float | None
    tc_active: float | None
    abs_active: float | None
    wear: tuple               # 100 new, counting down; Nones if unreported
    damage: float | None      # summed over the bodywork; 0 for none
    fuel_l: float | None
    air_temp: float
    road_temp: float
    tyre_compound: str
    # The game's own current, last and best lap clocks, as it prints them.
    clock_text: tuple[str, str, str]

    def sample(self, t_ms: int) -> tuple:
        """This reading as a row of db.SAMPLE_COLUMNS, after lap_id."""
        return (t_ms, self.norm_pos, self.speed_kmh, self.gas, self.brake,
                self.steer, self.gear, self.rpm, self.acc_lat, self.acc_lon,
                *self.slip, *self.pressure_psi, *self.core_temp_c,
                *self.ride_height_m, self.tyres_out,
                *(self.world_pos or (None,) * 3),
                self.heading, self.pitch, self.roll,
                self.tc_active, self.abs_active, *self.wear, self.damage)
