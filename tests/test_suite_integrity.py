"""The suite checking itself: every module must run, and run all of itself.

Two ways a test module can stop testing without anyone noticing, both of
which had actually happened when this file was written:

  * A `if __name__ == "__main__": sys.exit(...)` block placed before the
    last few test functions. Under pytest all of them run; standalone, the
    interpreter reaches sys.exit while executing the module and the
    functions below it are never even defined. test_setup_attribution ran 5
    of its 7 tests that way and reported success.

  * A module that only works because something else already arranged
    sys.path. test_delta_and_corners inserted tests/ and never the repo
    root, so `python tests/test_delta_and_corners.py` was a
    ModuleNotFoundError -- invisible under pytest, which sets the path
    itself, and invisible in CI, which runs `pip install -e .` first and so
    makes ac_race_engineer importable from anywhere.

Both are properties of the suite rather than of the code under test, which
is why they belong here rather than in any module about the game.
"""

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent


def modules() -> list[Path]:
    found = sorted(TESTS.glob("test_*.py"))
    assert found, f"no test modules under {TESTS}"
    return found


def _main_guard(tree: ast.Module) -> ast.If | None:
    """The top-level `if __name__ == "__main__":` statement, if there is one."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
                and any(isinstance(c, ast.Constant) and c.value == "__main__"
                        for c in test.comparators)):
            return node
    return None


def test_no_test_is_defined_after_the_main_block():
    """Anything below `if __name__ == "__main__"` does not exist standalone.

    The module runs top to bottom before the guard is reached, so a test
    function defined after it is missing from globals() when the runner
    inside the guard collects them -- and if the guard calls sys.exit, the
    definition is never executed at all.
    """
    offenders = []
    for path in modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        guard = _main_guard(tree)
        assert guard is not None, (
            f"{path.name} has no `if __name__ == \"__main__\"` runner, so it "
            f"cannot be run on its own on the gaming PC")
        for node in tree.body:
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith("test_")
                    and node.lineno > guard.lineno):
                offenders.append(
                    f"{path.name}:{node.lineno} {node.name} is defined after "
                    f"the __main__ block on line {guard.lineno}")

    assert not offenders, (
        "these tests never run standalone -- move the __main__ block to the "
        "end of the file:\n  " + "\n  ".join(offenders))
    print(f"  {len(modules())} modules, __main__ block last in each")


# The subprocess below runs with -I (isolated: no PYTHONPATH, no user site,
# sys.path[0] is the wrapper's own directory) from a temporary working
# directory, and then removes every remaining way to import ac_race_engineer
# that the module did not arrange itself. That last part is the point: CI
# runs `pip install -e ".[test]"` before the suite, which puts the package
# on sys.path for every interpreter on the machine and so hides a module
# that never adds the repo root. Without this, the standalone path only
# looks tested.
_WRAPPER = '''
import importlib.util, runpy, sys

PKG = "ac_race_engineer"

def provides(entry):
    try:
        import importlib.machinery
        return importlib.machinery.PathFinder.find_spec(PKG, [entry]) is not None
    except Exception:
        return False

sys.path = [p for p in sys.path if not provides(p)]

def finds(finder):
    # An editable install can hook in as a meta path finder rather than as a
    # path entry, which the filter above would not touch. PathFinder itself
    # searches the sys.path we just cleaned, so it answers None and stays.
    find = getattr(finder, "find_spec", None)
    if find is None:
        return False
    try:
        return find(PKG, None) is not None
    except Exception:
        return False

sys.meta_path = [f for f in sys.meta_path if not finds(f)]

if importlib.util.find_spec(PKG) is not None:
    sys.exit("the guard failed: %s is still importable, so this proves "
             "nothing" % PKG)

runpy.run_path(sys.argv[1], run_name="__standalone_import_check__")
'''


def test_every_module_imports_standalone_without_the_package_installed():
    """`python tests/test_x.py` has to work on a machine with nothing on it.

    The gaming PC has Python because the server needs it, and nothing else:
    no pytest, no editable install, no PYTHONPATH. Each module therefore has
    to put the repo root on sys.path itself (directly, or via support, which
    does it). This runs each module's top level -- imports and definitions,
    not the tests, which run_tests.py --isolate covers -- in an interpreter
    where the installed package has been made unreachable.
    """
    with tempfile.TemporaryDirectory() as d:
        wrapper = Path(d) / "standalone_check.py"
        wrapper.write_text(_WRAPPER, encoding="utf-8")

        # -I already drops PYTHONPATH, but not our own variables. Whether a
        # missing lupa should skip or fail is a different question from
        # whether the module can find its imports, and mixing them makes
        # this report "sys.path is wrong" when it is not.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("AC_TESTS_")}

        broken = []
        for path in modules():
            proc = subprocess.run(
                [sys.executable, "-I", str(wrapper), str(path)],
                cwd=d, env=env, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout).strip().splitlines()[-6:]
                broken.append(f"{path.name}:\n      "
                              + "\n      ".join(tail))

        assert not broken, (
            "these modules only import because something else set sys.path "
            "for them:\n    " + "\n    ".join(broken))
        print(f"  {len(modules())} modules import with nothing installed")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
