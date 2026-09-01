"""Finding the data directory across the ac-race-engineer rename.

The database holds laps that cannot be re-driven, so the rename had exactly
one hard requirement: nobody opens the tool afterwards and finds an empty
history. The old directory is therefore moved rather than abandoned -- and,
more importantly, a move that *cannot* happen has to leave the old directory
in use rather than silently start fresh beside it.

Two things make the move racy in real use. Several server instances start at
once, so two can attempt it simultaneously; and one instance may already hold
the database open, in which case Windows refuses to rename the directory at
all. Both are covered below, because both end in lost laps if handled wrong.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402

from assetto_mcp import config  # noqa: E402


class _Home:
    """Point config at a scratch home, and put the env back afterwards."""

    _VARS = ("ASSETTO_MCP_DATA", "AC_ENGINEER_DATA",
             "ASSETTO_MCP_BRIDGE_PORT", "AC_ENGINEER_BRIDGE_PORT")

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_default = config.DEFAULT_DATA_DIR
        self._saved_legacy = config.LEGACY_DATA_DIR
        self._saved_env = {k: os.environ.pop(k, None) for k in self._VARS}
        home = Path(self._tmp.name)
        config.DEFAULT_DATA_DIR = home / ".assetto-mcp"
        config.LEGACY_DATA_DIR = home / ".ac-race-engineer"
        return self

    def __exit__(self, *exc):
        config.DEFAULT_DATA_DIR = self._saved_default
        config.LEGACY_DATA_DIR = self._saved_legacy
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()
        return False

    @property
    def new(self):
        return config.DEFAULT_DATA_DIR

    @property
    def old(self):
        return config.LEGACY_DATA_DIR

    def seed_legacy(self, contents="laps"):
        self.old.mkdir()
        (self.old / "telemetry.db").write_text(contents)


def test_a_pre_rename_directory_is_moved_with_its_laps():
    with _Home() as h:
        h.seed_legacy()
        assert config.data_dir() == h.new
        assert (h.new / "telemetry.db").read_text() == "laps"
        assert not h.old.exists(), "the old directory should be gone, not copied"
        print("  moved, telemetry.db intact")


def test_an_existing_new_directory_is_left_alone():
    # Never merge two databases by moving one on top of the other.
    with _Home() as h:
        h.seed_legacy("old laps")
        h.new.mkdir()
        (h.new / "telemetry.db").write_text("current laps")
        assert config.data_dir() == h.new
        assert (h.new / "telemetry.db").read_text() == "current laps"
        assert h.old.exists(), "the old directory must be left for the human"


def test_a_fresh_install_just_uses_the_new_name():
    with _Home() as h:
        assert config.data_dir() == h.new


def test_an_explicit_data_dir_wins_and_moves_nothing():
    with _Home() as h:
        h.seed_legacy()
        os.environ["ASSETTO_MCP_DATA"] = str(h.new.parent / "elsewhere")
        assert config.data_dir() == h.new.parent / "elsewhere"
        assert h.old.exists(), "an explicit path must not trigger a migration"


def test_the_pre_rename_environment_variables_still_work():
    with _Home() as h:
        os.environ["AC_ENGINEER_DATA"] = str(h.new.parent / "old-style")
        assert config.data_dir() == h.new.parent / "old-style"


def test_the_new_environment_name_wins_over_the_old_one():
    with _Home():
        assert config.env("BRIDGE_PORT", "9666") == "9666"
        os.environ["AC_ENGINEER_BRIDGE_PORT"] = "7777"
        assert config.env("BRIDGE_PORT", "9666") == "7777"
        os.environ["ASSETTO_MCP_BRIDGE_PORT"] = "8888"
        assert config.env("BRIDGE_PORT", "9666") == "8888"


def test_a_file_where_the_data_directory_goes_is_named_not_stumbled_over():
    # Without the guard this surfaces several lines later as a
    # NotADirectoryError from mkdir(ranges), which reads like a server bug.
    with _Home() as h:
        h.new.write_text("not a directory")
        try:
            config.data_dir()
        except NotADirectoryError as e:
            assert str(h.new) in str(e)
        else:
            raise AssertionError("should have refused")


def test_a_file_at_the_destination_is_never_renamed_over():
    # Path.rename replaces an existing *file* destination silently on POSIX,
    # so a check for mere existence would have deleted it and moved the
    # database on top. Refuse instead, and leave both where they are.
    with _Home() as h:
        h.seed_legacy()
        h.new.write_text("someone's file")
        try:
            config.data_dir()
        except NotADirectoryError:
            pass
        else:
            raise AssertionError("should have refused")
        assert h.new.read_text() == "someone's file", "the file was destroyed"
        assert (h.old / "telemetry.db").exists(), "the laps were moved anyway"


def test_an_explicit_data_dir_pointing_at_a_file_is_refused_too():
    with _Home() as h:
        stray = h.new.parent / "stray"
        stray.write_text("x")
        os.environ["ASSETTO_MCP_DATA"] = str(stray)
        try:
            config.data_dir()
        except NotADirectoryError:
            pass
        else:
            raise AssertionError("should have refused")


def test_a_legacy_path_that_is_a_file_is_simply_ignored():
    with _Home() as h:
        h.old.write_text("not a directory either")
        assert config.data_dir() == h.new
        assert h.old.read_text() == "not a directory either"


def test_losing_the_race_adopts_what_the_winner_created():
    # Two servers start together. The loser's rename fails with the source
    # already gone; it must use the directory the winner just made, not
    # fall back to a path that no longer exists.
    with _Home() as h:
        h.seed_legacy()
        real = Path.rename

        def rename_that_lost(self, target):
            Path(target).mkdir()
            raise OSError("destination already exists")

        Path.rename = rename_that_lost
        try:
            assert config.data_dir() == h.new
        finally:
            Path.rename = real


def test_a_move_that_cannot_happen_keeps_using_the_old_directory():
    # The case that matters most: something holds the database open, so the
    # rename is impossible. Returning the new path here would present the
    # driver with an empty history and look exactly like data loss.
    with _Home() as h:
        h.seed_legacy()
        real = Path.rename

        def rename_that_failed(self, target):
            raise OSError("file in use")

        Path.rename = rename_that_failed
        try:
            assert config.data_dir() == h.old
            assert (h.old / "telemetry.db").read_text() == "laps"
        finally:
            Path.rename = real


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
