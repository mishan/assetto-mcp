"""The games the server can read, and what differs between them."""

from .. import config
from . import ac

GAMES = {ac.Reader.name: ac}


def chosen() -> str:
    """The game to read, from ASSETTO_MCP_SIM: a name in GAMES, or "auto"
    for whichever is running."""
    name = config.env("SIM", "ac")
    if name != "auto" and name not in GAMES:
        raise RuntimeError(f"ASSETTO_MCP_SIM is {name!r}; it can be auto, "
                           + ", ".join(GAMES))
    return name


def open_reader(name: str):
    """A reader of game `name`, or with "auto" of the first one running;
    raises while none is."""
    if name != "auto":
        return GAMES[name].live()
    why = []
    for game in GAMES.values():
        try:
            return game.live()
        except Exception as e:  # noqa: BLE001 - not running, not Windows
            why.append(f"{game.TITLE}: {e}")
    raise RuntimeError("no game is running (" + "; ".join(why) + ")")


def calibration(sim: str, name: str, default):
    """The threshold `name` for game `sim`.

    The game's own value where it differs, else `default`: the thresholds
    were tuned on Assetto Corsa, and each game lists only what it changes
    in its module's CALIBRATION.
    """
    return getattr(GAMES.get(sim), "CALIBRATION", {}).get(name, default)


# What each capability lets the driver have, for saying it is missing.
CAPABILITY_TEXT = {
    "opponents": "opponents' positions and names",
    "suspension": "suspension travel",
    "setup_values": "the setup on the car",
    "messages": "messages shown in the game",
    "setup_files": "setup files the server can read and write",
    "track_sections": "corner names from the track's files",
}


def capabilities(sim: str) -> dict[str, str]:
    """What game `sim` provides beyond recording laps, and through what."""
    return getattr(GAMES.get(sim), "CAPABILITIES", {})


def unsupported(sim: str, capability: str) -> dict | None:
    """A tool's answer when game `sim` lacks `capability`, else None."""
    if capability in capabilities(sim):
        return None
    title = getattr(GAMES.get(sim), "TITLE", sim)
    return {"error": f"{title} does not provide "
                     f"{CAPABILITY_TEXT[capability]}",
            "unsupported": capability, "sim": sim}
