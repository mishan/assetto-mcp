"""The games the server can read, and what differs between them."""

from . import ac

GAMES = {ac.Reader.name: ac}


def calibration(sim: str, name: str, default):
    """The threshold `name` for game `sim`.

    The game's own value where it differs, else `default`: the thresholds
    were tuned on Assetto Corsa, and each game lists only what it changes
    in its module's CALIBRATION.
    """
    return getattr(GAMES.get(sim), "CALIBRATION", {}).get(name, default)
