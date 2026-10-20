"""The tool surface as a client sees it: its shape, its cost, its names.

A model knows this server only from tools/list, the instructions and the
docs, so a schema change, a description that doubled or a tool the docs
still name after a rename should each be a decision someone made. The
checked-in tools/list snapshot makes a schema change show up as a diff; to
accept one on purpose:

    python tests/test_mcp_contract.py --write-snapshot
"""

import ast
import asyncio
import inspect
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402
from test_mcp_protocol import (  # noqa: E402
    _client, _server, _session_with_laps, _wire)

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
SNAPSHOT = TESTS / "golden" / "tools.json"

# Today's figures are 44.0k, 1.3k and 0.9k characters: these leave room to
# grow a little, and make growing a lot a decision.
TOOLS_LIST_BUDGET = 50_000
DESCRIPTION_BUDGET = 1_500
INSTRUCTIONS_BUDGET = 1_200

# Characters of reply on a ten-corner lap, read on every lap of a live
# session. Today: 2.8k, 7.6k, 1.3k and 0.2k.
PER_LAP_BUDGETS = {
    ("lap_summary", "summary"): 3_500,
    ("lap_summary", "full"): 9_500,
    ("compare_lap_corners", "summary"): 1_700,
    ("wait_for_lap", None): 400,
}
TEN_CORNERS = tuple((0.05 + i * 0.09, 0.025, (2.4, 1.2, 2.8, 1.6, 2.0)[i % 5])
                    for i in range(10))


def _surface() -> dict:
    """What a client gets: instructions, tools, and each prompt's text."""
    async def run():
        async with _client() as (session, init):
            tools = _wire(await session.list_tools())["tools"]
            prompts = {}
            for p in _wire(await session.list_prompts())["prompts"]:
                args = {a["name"]: "x" for a in p.get("arguments", [])
                        if a.get("required")}
                got = _wire(await session.get_prompt(p["name"], args))
                prompts[p["name"]] = got["messages"][0]["content"]["text"]
            return {"instructions": init["instructions"], "tools": tools,
                    "prompts": prompts}
    return asyncio.run(run())


def _normalized(tools: list[dict]) -> list[dict]:
    # Python 3.13 strips a docstring's indentation when it compiles it, and
    # earlier versions leave it in; the SDKs send the docstring as it is.
    return sorted(({**t, "description": inspect.cleandoc(t["description"])}
                   for t in tools), key=lambda t: t["name"])


def _snapshot_text(tools: list[dict]) -> str:
    return json.dumps(_normalized(tools), indent=1, sort_keys=True,
                      ensure_ascii=False) + "\n"


def test_the_tool_list_is_the_checked_in_one():
    """Names, input schemas, titles and hints, as they go on the wire."""
    tools = _normalized(_surface()["tools"])
    want = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if tools == want:
        return
    have = {t["name"]: t for t in tools}
    had = {t["name"]: t for t in want}
    changed = sorted(n for n in have.keys() & had.keys()
                     if have[n] != had[n])
    raise AssertionError(
        f"tools/list differs from {SNAPSHOT.relative_to(ROOT)}: "
        f"new {sorted(have.keys() - had.keys())}, "
        f"gone {sorted(had.keys() - have.keys())}, changed {changed}. "
        f"If that is intended: python tests/test_mcp_contract.py "
        f"--write-snapshot")


def test_the_tool_list_and_instructions_fit_their_budgets():
    surface = _surface()
    tools = _normalized(surface["tools"])
    size = len(json.dumps(tools, separators=(",", ":"), ensure_ascii=False))
    assert size <= TOOLS_LIST_BUDGET, size
    long = {t["name"]: len(t["description"]) for t in tools
            if len(t["description"]) > DESCRIPTION_BUDGET}
    assert not long, long
    assert len(surface["instructions"]) <= INSTRUCTIONS_BUDGET, \
        len(surface["instructions"])


def test_the_per_lap_reads_fit_their_budgets():
    sid, ids = _session_with_laps(4, samples=1200, corners=TEN_CORNERS)
    args = {"lap_summary": {"lap_id": ids[-1]},
            "compare_lap_corners": {"lap_id_a": ids[-1], "lap_id_b": ids[0]},
            "wait_for_lap": {"after_lap_id": ids[-2], "timeout_s": 1}}

    async def run():
        async with _client() as (session, _):
            out = {}
            for name, detail in PER_LAP_BUDGETS:
                a = {**args[name], **({"detail": detail} if detail else {})}
                r = _wire(await session.call_tool(name, a))
                assert not r.get("isError"), (name, r)
                out[name, detail] = r["content"][0]["text"]
            return out

    replies = asyncio.run(run())
    corners = json.loads(replies["lap_summary", "summary"])["corners"]
    assert len(corners) == len(TEN_CORNERS), corners
    over = {k: len(v) for k, v in replies.items()
            if len(v) > PER_LAP_BUDGETS[k]}
    assert not over, over


# A name a reader would take for a tool: a call, or a verb a tool would
# start with, or a noun tools end with. Not after a dot or slash
# (`db.get_lap`, `tools/list_changed`), nor before `.word` (a file name).
_VERBS = ("add|annotate|apply|check|clear|compare|create|delete|describe|"
          "export|fetch|find|forget|get|identify|label|list|measure|plan|"
          "read|record|remove|rename|rescore|restart|save|send|set|show|"
          "start|stop|tag|update|wait|write")
_TOOLISH = re.compile(
    r"(?<![\w./])("
    rf"(?:{_VERBS})_[a-z_]*[a-z]"
    r"|[a-z]+(?:_[a-z]+)*_(?:status|report|snapshot|summary|benchmark)"
    r"|[a-z]+(?:_[a-z]+)+(?=\())(?![\w/]|\.\w)")
# Said near an old name, it is being named as old.
_OLD = re.compile(r"renamed|formerly|deprecated|old name|\bwas\b", re.I)


def _package_identifiers() -> set[str]:
    """Function names and whole-identifier strings (payload keys) in the
    package: words that look like tools and are not."""
    found = set()
    for path in (ROOT / "assetto_mcp").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.add(node.name)
            elif (isinstance(node, ast.Constant)
                  and isinstance(node.value, str)
                  and re.fullmatch(r"[a-z]+(?:_[a-z]+)+", node.value)):
                found.add(node.value)
    return found


def _server_strings() -> list[tuple[int, str]]:
    """Every string in server.py but a bare identifier: descriptions,
    guidance (do_this, note, warning...) and errors a model reads."""
    tree = ast.parse((ROOT / "assetto_mcp" / "server.py")
                     .read_text(encoding="utf-8"))
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and not re.fullmatch(r"\w+", n.value)]


def test_every_tool_the_docs_and_replies_name_exists():
    """A model told to call a tool that is gone guesses, or gives up. An
    old name still answers, so the docs may say what a tool was called,
    as long as they say it is old; text the model reads may not."""
    s = _server()
    surface = _surface()
    tools = {t["name"] for t in surface["tools"]}
    model_reads = {"instructions": surface["instructions"].splitlines()}
    for t in surface["tools"]:
        model_reads[f"{t['name']} description"] = \
            t["description"].splitlines()
    for name, text in surface["prompts"].items():
        model_reads[f"prompt {name}"] = text.splitlines()
    docs = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")),
            *sorted((ROOT / ".claude" / "skills" / "race-engineer")
                    .rglob("*.md"))]
    sources = {
        **{k: (v, False) for k, v in model_reads.items()},
        **{str(p.relative_to(ROOT)): (
            p.read_text(encoding="utf-8").splitlines(), True) for p in docs},
        **{f"server.py:{n}": ([text], False)
           for n, text in _server_strings()},
    }
    not_tools = (_package_identifiers() | set(surface["prompts"])
                 | {p for t in surface["tools"]
                    for p in t["inputSchema"].get("properties", {})})
    old = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, s._RENAMED))
                     + r")(?!\w)")
    bad = []
    for where, (lines, may_name_old) in sources.items():
        for i, line in enumerate(lines, 1):
            for name in old.findall(line):
                if not (may_name_old and _OLD.search(line)):
                    bad.append((where, i, name, s._RENAMED[name]))
            for name in _TOOLISH.findall(line):
                if (name not in tools and name not in s._RENAMED
                        and name not in not_tools):
                    bad.append((where, i, name, None))
    assert not bad, bad


if __name__ == "__main__":
    if "--write-snapshot" in sys.argv:
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(_snapshot_text(_surface()["tools"]),
                            encoding="utf-8", newline="\n")
        print(f"wrote {SNAPSHOT}")
        sys.exit(0)
    sys.exit(1 if run_module(globals()) else 0)
