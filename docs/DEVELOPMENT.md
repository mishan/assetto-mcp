# Development

## Tests

```
python run_tests.py              everything, one line per module
python run_tests.py -v           one line per test, with each test's output
python run_tests.py -k damper    only tests matching a regex
python run_tests.py --isolate    each module in its own process
python run_tests.py --lua        syntax-check the in-game Lua app too
python run_tests.py --lua-only   only the Lua syntax check
python run_tests.py --list       show what would run
python run_tests.py --no-skip    treat any skip as a failure (what CI runs)
```

`run_tests.py` needs no test-only packages, so the suite runs on the gaming
PC without pytest. It does need the project itself installed
(`pip install -e .`), because the modules that exercise the MCP tools
import the server and with it the `mcp` package.

For everything, install the `[test]` extra:

```
pip install -e ".[test]"
```

That adds pytest, `lupa` (a Lua runtime, to run the in-game app against a
stubbed CSP API) and `luaparser` (for the syntax check). Without `lupa` or
`luaparser` the tests that need them skip; any other missing import is a
failure. `pytest tests/ -q` works as well and gives better assertion diffs.

Nothing needs Windows or Assetto Corsa: the collector is driven through a
fake `SimInfo`, and the bridge is exercised over real HTTP on an ephemeral
localhost port. `--isolate` runs each module through its own `__main__`
block, the path the gaming PC takes, and CI runs it so that path keeps
working.

### `AC_TESTS_STRICT`

Off by default, because on the gaming PC neither `lupa` nor `luaparser` is
installed and skipping those tests is correct there. Set it to `1`, as CI
does, and their absence becomes an import error instead of a quiet skip;
otherwise a suite that skipped the Lua tests exits 0 and looks the same as
one that ran them. `--no-skip` sets it for you.

### The Lua runtime matters

CSP runs LuaJIT 2.1, which is Lua 5.1. `lupa` ships 5.1 through 5.5 in one
wheel with the newest as its default, and testing on 5.5 is a false signal in
both directions: `7 // 2` runs there and is a syntax error in the game, and
`string.format('%d', 1.5)` raises there and works in the game. The harness asks
for LuaJIT by name.

### Recordings of the game

`python -m assetto_mcp.sims.recording record lap.simrec` on the gaming PC
records the game's shared memory until Ctrl+C. `replay lap.simrec --db out.db`
stores its laps into a database as fast as the collector can, on any OS, in
the recording's own time, so two replays store identical rows: replay before
and after a change to the collector and compare. `ASSETTO_MCP_REPLAY` runs the
whole server from one in real time instead.

### The tool surface

`tests/test_mcp_contract.py` compares `tools/list` (names, input schemas,
titles, hints and descriptions) with `tests/golden/tools.json`, so a
changed tool shows up as a failing test before it reaches a client. When
the change is meant, rewrite the snapshot and commit it with the change:

```
python tests/test_mcp_contract.py --write-snapshot
```

The same module holds the budgets: the size of `tools/list`, of each
description and of the instructions, and of the replies read on every lap.
A budget that fails is a question about context cost, not a number to
raise by reflex. It also checks that every tool the docs, the skill, the
prompts and the server's own replies name exists.

### Tool-selection eval

`tests/eval/tool_choice.py` asks a model which tool it would call first
for each request in `tests/eval/cases.json`, given only `tools/list` and
the server's instructions, and prints each case (expected tools against
the one chosen) and the score, for the full toolset and for
`ASSETTO_MCP_TOOLSET=core`. Run it after renaming a tool or rewriting a
description. It needs a model, so it is not part of the suite or CI;
without one configured it says so and exits.

Against a local model on an OpenAI-compatible server (llama-server, LM
Studio, Ollama):

```
llama-server -m Qwen3-30B-A3B-Instruct.gguf --jinja --port 8080
ASSETTO_MCP_EVAL_BASE_URL=http://127.0.0.1:8080 python tests/eval/tool_choice.py
```

`--jinja` is what makes llama-server accept tools. LM Studio and Ollama
want the model's name as well: `ASSETTO_MCP_EVAL_MODEL=<name>`.
`ASSETTO_MCP_EVAL_API_KEY`, if set, is sent as a bearer token.

Against the Anthropic API (`claude-haiku-5-5` unless
`ASSETTO_MCP_EVAL_MODEL` says otherwise):

```
pip install -e ".[eval]"
ASSETTO_MCP_EVAL=anthropic ANTHROPIC_API_KEY=... python tests/eval/tool_choice.py
```

`--toolset full|core` runs one toolset, `-k fuel` only the matching cases,
and `--json results.json` keeps every answer for comparing runs.

## CI

`.github/workflows/ci.yml` runs four jobs:

- **lint** — `ruff check .` with the rule set pinned in `pyproject.toml`.
- **tests** — Ubuntu and Windows, Python 3.10 and 3.13. Windows is the
  deployment target, so it is not an afterthought.
- **lua** — parses the in-game app and runs it against a stubbed CSP API.
  Syntax checking alone misses the bugs that matter here; they show up only
  when the app runs.
- **powershell** — parses the installer scripts under **Windows PowerShell
  5.1** (not pwsh 7), because that's what a double-click gets on the gaming PC.
  It also rejects PS7-only syntax and any `Set-Content -Encoding UTF8`, which
  writes a BOM that Claude Desktop's JSON parser rejects.
  All the Claude-Desktop-specific logic lives in `windows/install-claude-desktop.ps1`;
  `windows/install-windows.ps1` dot-sources it and is otherwise client-agnostic.

## Line endings

`.gitattributes` normalizes to LF in the repo and checks out CRLF only for
`.bat`, `.cmd`, `.ps1` and `.ini`. Don't let an editor convert the tree to
CRLF: every line shows as changed and `git blame` is lost for the whole
project.

## Schema changes

`db.py` migrates forward in `_migrate`. Adding a column to an existing table
with `CREATE TABLE IF NOT EXISTS` silently does nothing, so every column
addition goes through `_add_column`, which no-ops on a missing table. Bump the
schema version and add a test in `tests/test_schema_migration.py`.

Never make a migration destructive. The database holds laps that cannot be
re-driven.
