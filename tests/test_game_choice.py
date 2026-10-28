"""Which game the server reads: ASSETTO_MCP_SIM names one, or "auto"."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402

from assetto_mcp import sims  # noqa: E402


def _game(title, reader=None):
    def live():
        if reader is None:
            raise RuntimeError("not running")
        return reader
    return SimpleNamespace(TITLE=title, live=live)


def test_auto_opens_whichever_game_is_running():
    real = dict(sims.GAMES)
    sims.GAMES.clear()
    sims.GAMES.update(off=_game("Off Racer"), on=_game("On Racer", "reader"))
    try:
        assert sims.open_reader("auto") == "reader"
        del sims.GAMES["on"]
        try:
            sims.open_reader("auto")
        except RuntimeError as e:
            assert "Off Racer: not running" in str(e), e
        else:
            raise AssertionError("opened a game that is not running")
    finally:
        sims.GAMES.clear()
        sims.GAMES.update(real)


def test_an_unknown_game_is_refused_by_name():
    old = os.environ.get("ASSETTO_MCP_SIM")
    os.environ["ASSETTO_MCP_SIM"] = "gt7"
    try:
        sims.chosen()
    except RuntimeError as e:
        assert "'gt7'" in str(e) and "auto, ac" in str(e), e
    else:
        raise AssertionError("accepted a game it cannot read")
    finally:
        if old is None:
            os.environ.pop("ASSETTO_MCP_SIM")
        else:
            os.environ["ASSETTO_MCP_SIM"] = old


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
