"""Where the data lives, and what the environment may say about it.

Installs from when the project was named ac-race-engineer keep their laps in
``~/.ac-race-engineer`` and may set ``AC_ENGINEER_*`` variables. That
directory is migrated on first start and those names are still read, so an
upgrade never starts an empty database beside the full one.
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


def _must_be_a_directory(path: Path) -> Path:
    """Fail early and by name if the data path exists as something else.

    Otherwise ``mkdir(ranges)`` raises an oblique NotADirectoryError later
    that reads like a server bug rather than a file in the way.
    """
    if path.exists() and not path.is_dir():
        raise NotADirectoryError(
            f"{path} exists but is not a directory. The lap database and car "
            f"ranges live there, so move that file aside (or point "
            f"ASSETTO_MCP_DATA somewhere else) and start again.")
    return path


def data_dir() -> Path:
    """The DB + ranges directory, migrating a pre-rename install if it finds one.

    An explicit ``ASSETTO_MCP_DATA`` always wins. Otherwise, if only the
    pre-rename directory exists, it is renamed in place.

    The rename must survive three things. Several server instances start at
    once, so two can race: the loser's OSError leaves the destination
    present, and it uses that. A running instance holding the database open
    makes Windows refuse the rename; the old path is then used until the next
    clean start. And the destination may exist as a *file*, checked before
    the rename because ``Path.rename`` silently replaces a file on POSIX.
    """
    explicit = env("DATA")
    if explicit:
        return _must_be_a_directory(Path(explicit))

    if LEGACY_DATA_DIR.is_dir() and not DEFAULT_DATA_DIR.is_dir():
        _must_be_a_directory(DEFAULT_DATA_DIR)
        try:
            LEGACY_DATA_DIR.rename(DEFAULT_DATA_DIR)
        except OSError:
            # Either another instance renamed it first, or something still
            # holds a file inside it. Only the first case leaves a usable
            # destination behind.
            if not DEFAULT_DATA_DIR.is_dir():
                return LEGACY_DATA_DIR

    return _must_be_a_directory(DEFAULT_DATA_DIR)
