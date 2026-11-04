"""Telemetrist under its old name.

MCP client configs say ``python -m assetto_mcp.server``, and nothing
rewrites them; installs made before the rename have console scripts that
import from here until pip reinstalls them, and a supervisor started before
the rename keeps launching its child this way. Each module here hands over
to the one in ``telemetrist``.
"""

import importlib.util
import os
import sys
from pathlib import Path

# An editable install made before the rename maps only this folder, and pip
# may not have reinstalled since: on Windows it fails while the server holds
# files open. telemetrist then sits beside this folder in the checkout, and
# the supervisor's child process needs to find it as well as this one.
_root = Path(__file__).resolve().parent.parent
if (importlib.util.find_spec("telemetrist") is None
        and (_root / "telemetrist").is_dir()):
    sys.path.append(str(_root))
    os.environ["PYTHONPATH"] = os.pathsep.join(
        p for p in (os.environ.get("PYTHONPATH"), str(_root)) if p)
