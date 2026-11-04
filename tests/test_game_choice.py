"""Which game the server reads: TELEMETRIST_SIM names it."""

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402

from telemetrist import sims  # noqa: E402


def test_the_game_is_named_in_any_case_and_an_unknown_one_refused():
    with mock.patch.dict(os.environ, {"TELEMETRIST_SIM": " AC "}):
        assert sims.chosen() == "ac"
    with mock.patch.dict(os.environ, {"TELEMETRIST_SIM": "gt7"}):
        try:
            sims.chosen()
        except RuntimeError as e:
            assert "'gt7'" in str(e), e
        else:
            raise AssertionError("accepted a game it cannot read")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
