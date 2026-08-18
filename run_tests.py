#!/usr/bin/env python3
"""Run the whole suite and summarise it. No dependencies, no shell loop.

    python run_tests.py                 everything, one line per module
    python run_tests.py -v              one line per test
    python run_tests.py -k damper       only tests whose name matches
    python run_tests.py --isolate       each module in its own process
    python run_tests.py --list          show what would run
    python run_tests.py --lua           Lua syntax check as well

pytest works too and gives nicer diffs -- `pytest tests/ -q`. This exists so
the suite is runnable on the gaming PC, which has Python because the server
needs it but has no reason to have pytest, and so CI can check the
standalone path without a `for` loop in YAML.

Exit code is 0 only if everything passed.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import contextlib
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"

# ANSI only when we're attached to something that can show it.
_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


GREEN = lambda s: _c(s, "32")      # noqa: E731
RED = lambda s: _c(s, "31")        # noqa: E731
YELLOW = lambda s: _c(s, "33")     # noqa: E731
DIM = lambda s: _c(s, "2")         # noqa: E731
BOLD = lambda s: _c(s, "1")        # noqa: E731


@dataclass
class Result:
    module: str
    name: str
    passed: bool
    seconds: float
    output: str = ""
    error: str = ""


@dataclass
class ModuleRun:
    name: str
    results: list[Result] = field(default_factory=list)
    load_error: str = ""

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if not r.passed]

    @property
    def seconds(self) -> float:
        return sum(r.seconds for r in self.results)

    @property
    def ok(self) -> bool:
        return not self.load_error and not self.failed


# --- discovery ----------------------------------------------------------


def discover() -> list[Path]:
    if not TESTS.is_dir():
        return []
    return sorted(p for p in TESTS.glob("test_*.py") if p.is_file())


def load(path: Path):
    """Import a test module by path, with tests/ and the repo root importable.

    The modules import their shared harness as `support`, so tests/ has to be
    on the path the same way pyproject's pytest `pythonpath` arranges it.
    """
    for p in (str(ROOT), str(TESTS)):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def tests_in(module) -> list[tuple[str, callable]]:
    return sorted((n, f) for n, f in vars(module).items()
                  if n.startswith("test_") and callable(f))


# --- running ------------------------------------------------------------


def run_module(path: Path, pattern: str | None, verbose: bool,
               failfast: bool) -> ModuleRun:
    run = ModuleRun(path.stem)
    try:
        module = load(path)
    except Exception:
        run.load_error = traceback.format_exc()
        return run

    for name, fn in tests_in(module):
        if pattern and not re.search(pattern, f"{path.stem}.{name}"):
            continue
        buf = io.StringIO()
        started = time.perf_counter()
        error = ""
        try:
            # Tests print their own findings; capture so the summary stays
            # readable and show it when something goes wrong.
            with contextlib.redirect_stdout(buf):
                fn()
            passed = True
        except AssertionError as e:
            passed = False
            error = f"AssertionError: {e}" if str(e) else "AssertionError"
            error += "\n" + _short_traceback()
        except Exception as e:  # noqa: BLE001
            passed = False
            error = f"{type(e).__name__}: {e}\n" + _short_traceback()
        elapsed = time.perf_counter() - started

        run.results.append(Result(path.stem, name, passed, elapsed,
                                  buf.getvalue(), error))
        if failfast and not passed:
            break
    return run


def _short_traceback() -> str:
    """Just the frames inside this project -- not the runner's own stack."""
    lines = traceback.format_exc().splitlines()
    keep = [l for l in lines
            if "run_tests.py" not in l and l.strip()]
    return "\n".join("      " + l for l in keep[-6:])


def _print_test(r: Result) -> None:
    mark = GREEN("PASS") if r.passed else RED("FAIL")
    print(f"  {mark}  {r.name} {DIM(f'({r.seconds:.2f}s)')}")
    if r.output.strip():
        for line in r.output.rstrip().splitlines():
            print(DIM(f"        {line.strip()}"))
    if not r.passed:
        print(RED(r.error))


# --- isolated mode ------------------------------------------------------


def run_isolated(paths: list[Path], pattern: str | None) -> int:
    """Each module in its own interpreter, via its __main__ block.

    This is what the shell loop used to do. Keeping it as a mode rather than
    the default means the standalone path stays exercised -- it is the one
    the gaming PC uses -- without anyone having to remember to check it.
    """
    width = max((len(p.stem) for p in paths), default=10) + 2
    failures = []
    total_start = time.perf_counter()
    for path in paths:
        started = time.perf_counter()
        proc = subprocess.run([sys.executable, str(path)],
                              capture_output=True, text=True)
        elapsed = time.perf_counter() - started
        ok = proc.returncode == 0
        mark = GREEN("ok  ") if ok else RED("FAIL")
        print(f"  {mark}  {path.stem:<{width}} {DIM(f'{elapsed:6.2f}s')}")
        if not ok:
            failures.append((path.stem, proc.stdout + proc.stderr))

    print()
    if failures:
        for name, output in failures:
            print(BOLD(RED(f"--- {name} ---")))
            print(output.rstrip()[-3000:])
            print()
        print(RED(f"{len(failures)} of {len(paths)} modules failed"))
        return 1
    print(GREEN(f"all {len(paths)} modules pass standalone "
                f"({time.perf_counter() - total_start:.1f}s)"))
    return 0


# --- extras -------------------------------------------------------------


def check_lua() -> bool | None:
    """Parse the in-game app. None if luaparser isn't installed.

    CSP loads these at runtime and reports failures in a debug window nobody
    has open, so a syntax error costs a session to notice.
    """
    try:
        from luaparser import ast
    except ImportError:
        return None
    ok = True
    for f in sorted((ROOT / "lua_app").rglob("*.lua")):
        try:
            ast.parse(f.read_text(encoding="utf-8"))
            print(f"  {GREEN('ok  ')}  {f.relative_to(ROOT)}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  {RED('FAIL')}  {f.relative_to(ROOT)}: {e}")
    return ok


# --- main ---------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="pytest tests/ -q works too, and gives better diffs.")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="one line per test, with each test's own output")
    ap.add_argument("-k", metavar="PATTERN", dest="pattern",
                    help="only run tests matching this regex "
                         "(matched against module.test_name)")
    ap.add_argument("--isolate", action="store_true",
                    help="run each module in its own process, the way the "
                         "gaming PC does")
    ap.add_argument("--lua", action="store_true",
                    help="also syntax-check the in-game Lua app")
    ap.add_argument("--lua-only", action="store_true",
                    help="only syntax-check the Lua app, skip the tests")
    ap.add_argument("--list", action="store_true",
                    help="list what would run and exit")
    ap.add_argument("-x", "--failfast", action="store_true",
                    help="stop a module at its first failure")
    args = ap.parse_args()

    if args.lua_only:
        print(BOLD("Lua syntax"))
        ok = check_lua()
        if ok is None:
            print(RED("  luaparser not installed: pip install luaparser"))
            return 1
        print()
        print(BOLD(GREEN("lua ok") if ok else RED("lua syntax errors")))
        return 0 if ok else 1

    paths = discover()
    if not paths:
        print(RED(f"no test modules found in {TESTS}"))
        return 1

    if args.list:
        for path in paths:
            try:
                names = [n for n, _ in tests_in(load(path))]
            except Exception as e:  # noqa: BLE001
                print(f"{path.stem}: {RED(f'failed to load: {e}')}")
                continue
            shown = [n for n in names
                     if not args.pattern
                     or re.search(args.pattern, f"{path.stem}.{n}")]
            if not shown:
                continue
            print(BOLD(f"{path.stem}  ({len(shown)})"))
            for n in shown:
                print(f"  {n}")
        return 0

    if args.isolate:
        print(BOLD("Running each module standalone\n"))
        return run_isolated(paths, args.pattern)

    if args.pattern:
        print(BOLD(f"Running tests matching {args.pattern!r}\n"))
    else:
        print(BOLD(f"Running {len(paths)} test modules\n"))

    runs: list[ModuleRun] = []
    started = time.perf_counter()
    for path in paths:
        run = run_module(path, args.pattern, args.verbose, args.failfast)
        # A filter usually selects from one or two modules; printing a
        # header for the eight it skipped buries the results.
        if not run.results and not run.load_error:
            continue
        if args.verbose:
            print(BOLD(path.stem))
            for r in run.results:
                _print_test(r)
            print()
        runs.append(run)
    total = time.perf_counter() - started

    if not runs:
        print(YELLOW(f"no tests matched {args.pattern!r}"))
        return 1

    return summarise(runs, total, args.lua, args.verbose)


def summarise(runs: list[ModuleRun], total: float, lua: bool,
              verbose: bool) -> int:
    if not verbose:
        width = max(len(r.name) for r in runs) + 2
        for run in runs:
            n = len(run.results)
            if run.load_error:
                print(f"  {RED('ERROR')} {run.name:<{width}} "
                      f"{RED('could not be imported')}")
            elif run.failed:
                print(f"  {RED('FAIL ')} {run.name:<{width}} "
                      f"{n - len(run.failed)}/{n} passed "
                      f"{DIM(f'{run.seconds:6.2f}s')}")
            else:
                print(f"  {GREEN('ok   ')} {run.name:<{width}} "
                      f"{n:>3} passed {DIM(f'{run.seconds:6.2f}s')}")

    lua_ok: bool | None = None
    if lua:
        print(f"\n{BOLD('Lua')}")
        lua_ok = check_lua()
        if lua_ok is None:
            print(f"  {YELLOW('skipped')} - pip install luaparser")

    passed = sum(len(r.results) - len(r.failed) for r in runs)
    failed = sum(len(r.failed) for r in runs)
    broken = [r for r in runs if r.load_error]

    print()
    for run in runs:
        if run.load_error:
            print(BOLD(RED(f"--- {run.name} failed to import ---")))
            print(run.load_error.rstrip())
            print()
        for r in run.failed:
            print(BOLD(RED(f"--- {r.module}.{r.name} ---")))
            if r.output.strip():
                for line in r.output.rstrip().splitlines():
                    print(DIM(f"    {line.strip()}"))
            print(r.error)
            print()

    bits = [f"{passed} passed"]
    if failed:
        bits.append(RED(f"{failed} failed"))
    if broken:
        bits.append(RED(f"{len(broken)} module"
                        f"{'s' if len(broken) != 1 else ''} broken"))
    if lua_ok is False:
        bits.append(RED("lua syntax errors"))
    elif lua_ok:
        bits.append("lua ok")

    line = f"{', '.join(bits)} in {total:.1f}s"
    print(BOLD(GREEN(line) if not (failed or broken or lua_ok is False)
               else RED(line)))
    return 0 if not (failed or broken or lua_ok is False) else 1


if __name__ == "__main__":
    sys.exit(main())
