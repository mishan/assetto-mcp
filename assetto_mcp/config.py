"""Where the data lives, and what the environment may say about it.

This project used to be called ac-race-engineer, so an install that predates
the rename has its laps in ``~/.ac-race-engineer`` and spells the environment
variables ``AC_ENGINEER_*``. The old directory is migrated on first start and
the old variable names are still read. A lap database outlives the name of the
thing that wrote it, and a rename that silently starts an empty one beside the
full one is indistinguishable from losing a season.
"""

import os
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / ".assetto-mcp"
LEGACY_DATA_DIR = Path.home() / ".ac-race-engineer"

_PREFIX = "ASSETTO_MCP_"
_LEGACY_PREFIX = "AC_ENGINEER_"


def env(name: str, default: str | None = None) -> str | None:
    """Read ``ASSETTO_MCP_<name>``, falling back to the pre-rename spelling."""
    value = os.environ.get(_PREFIX + name)
    if value is None:
        value = os.environ.get(_LEGACY_PREFIX + name)
    return default if value is None else value


def data_dir() -> Path:
    """The DB + ranges directory, migrating a pre-rename install if it finds one.

    An explicit ``ASSETTO_MCP_DATA`` always wins. Otherwise, if the only
    directory present is the pre-rename one, it is renamed -- once, in place,
    keeping every lap -- so nobody has to be told to move their own database.

    The rename has to be safe against the two things that happen here. Several
    server instances start at once, so two can attempt it simultaneously: the
    loser gets an OSError with the source already gone, sees the destination
    now exists, and uses it. And one instance may already be running with the
    database open, in which case Windows refuses to rename the directory at
    all; that failure is caught and the old path is used exactly as before, so
    the worst case is that nothing changes until the next clean start.
    """
    explicit = env("DATA")
    if explicit:
        return Path(explicit)

    if LEGACY_DATA_DIR.is_dir() and not DEFAULT_DATA_DIR.exists():
        try:
            LEGACY_DATA_DIR.rename(DEFAULT_DATA_DIR)
        except OSError:
            # Either another instance renamed it first, or something still
            # holds a file inside it. Only the first case leaves a usable
            # destination behind.
            if not DEFAULT_DATA_DIR.is_dir():
                return LEGACY_DATA_DIR

    return DEFAULT_DATA_DIR
