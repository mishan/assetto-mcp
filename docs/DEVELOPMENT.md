# Development

## Tests

```
python run_tests.py            everything, one line per module
python run_tests.py -v         one line per test, with each test's output
python run_tests.py -k damper  only tests matching a regex
python run_tests.py --isolate  each module in its own process
python run_tests.py --lua      syntax-check the in-game Lua app too
python run_tests.py --list     show what would run
```

No dependencies — it runs on the gaming PC, which has Python because the
server needs it and no reason to have anything else. `pytest tests/ -q` works
as well and gives better assertion diffs; `pip install -e ".[test]"` brings in
pytest plus the Lua tooling.

Everything runs without Windows or Assetto Corsa: the collector is driven
through a fake `SimInfo` and the bridge is exercised over real HTTP on an
ephemeral localhost port. `--isolate` is the mode CI uses to prove each module
still runs on its own, since that's the path the gaming PC takes.

### `AC_TESTS_STRICT`

Off by default, because on the gaming PC neither `lupa` nor `luaparser` is
installed and skipping those tests is correct there. Set it to `1` — as CI
does — and their absence becomes an import error instead of a quiet skip. A
suite that reports "9 skipped" and exit 0 is the same green as having run, and
that is exactly how the in-game app once went untested through three bugs.

### The Lua runtime matters

CSP runs LuaJIT 2.1, which is Lua 5.1. `lupa` ships 5.1 through 5.5 in one
wheel with the newest as its default, and testing on 5.5 is a false signal in
both directions: `7 // 2` runs there and is a syntax error in the game, and
`string.format('%d', 1.5)` raises there and works in the game. The harness asks
for LuaJIT by name.

## CI

`.github/workflows/ci.yml` runs three jobs:

- **tests** — Ubuntu and Windows, Python 3.10 and 3.13. Windows is the
  deployment target, so it is not an afterthought.
- **lua** — parses the in-game app and runs it against a stubbed CSP API.
  Syntax checking caught none of the bugs this app has shipped; all three
  needed the app to actually run.
- **powershell** — parses the installer scripts under **Windows PowerShell
  5.1** (not pwsh 7), because that's what a double-click gets on the gaming PC.
  It also rejects PS7-only syntax and any `Set-Content -Encoding UTF8`, which
  writes a BOM that Claude Desktop's JSON parser rejects.
  All the Claude-Desktop-specific logic lives in `install-claude-desktop.ps1`;
  `install-windows.ps1` dot-sources it and is otherwise client-agnostic.

## Line endings

`.gitattributes` normalises to LF in the repo and checks out CRLF only for
`.bat`, `.cmd`, `.ps1` and `.ini`. A single session that flipped the tree to
CRLF turned a 1,600 line change into a 3,900 line diff and reset `git blame`
for the whole project.

## Schema changes

`db.py` migrates forward in `_migrate`. Adding a column to an existing table
with `CREATE TABLE IF NOT EXISTS` silently does nothing, so every column
addition goes through `_add_column`, which no-ops on a missing table. Bump the
schema version and add a test in `tests/test_schema_migration.py`.

Never make a migration destructive. The database holds laps that cannot be
re-driven.
