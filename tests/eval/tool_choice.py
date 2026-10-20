#!/usr/bin/env python3
"""Which tool does a model call first, given only tools/list and the
server's instructions?

Each case in cases.json is a request a driver or engineer makes and the
tools a good engineer would call first. The model sees no skill and no
docs, as in a client that loads neither, so a miss points at a tool's name
or description. A first call that sets a flag the driver has not confirmed
(`unsafe`) fails the case whatever the tool.

Not part of the test suite: it needs a model. Pick one by environment:

    ASSETTO_MCP_EVAL_BASE_URL=http://127.0.0.1:8080   an OpenAI-compatible
        server (llama-server, LM Studio, Ollama); ASSETTO_MCP_EVAL_MODEL
        names the model if the server serves several, and
        ASSETTO_MCP_EVAL_API_KEY is sent as a bearer token if set.
    ASSETTO_MCP_EVAL=anthropic   the Anthropic API, with ANTHROPIC_API_KEY
        and the [eval] extra; ASSETTO_MCP_EVAL_MODEL overrides the model.

    python tests/eval/tool_choice.py                   full and core
    python tests/eval/tool_choice.py --toolset core -k fuel
    python tests/eval/tool_choice.py --json out.json

With neither set it says so and exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CASES = HERE / "cases.json"
ANTHROPIC_MODEL = "claude-haiku-5-5"

# Facts a driver would assume the engineer knows; ids the requests use.
CONTEXT = (
    "You are the driver's race engineer, working through the tools of the "
    "assetto-mcp server. The driver is in the ks_mazda_mx5_cup at mugello, "
    "session 12; its laps so far have ids 431 to 438, and lap 433 is the "
    "best clean one. Answer the driver's request, calling a tool when you "
    "need one.")

_DUMP = """
import asyncio, json
import assetto_mcp.server as s
tools = [t.model_dump(by_alias=True, exclude_none=True)
         for t in asyncio.run(s.mcp.list_tools())]
print(json.dumps({"instructions": s.INSTRUCTIONS, "tools": tools}))
s._shutdown()
"""


def surface(toolset: str) -> dict:
    """tools/list and the instructions, from a server of that toolset."""
    with tempfile.TemporaryDirectory(prefix="ac-eval-") as d:
        env = dict(os.environ, ASSETTO_MCP_DATA=d, AC_DOCS_DIR=d,
                   ASSETTO_MCP_NO_AUTOSTART="1", ASSETTO_MCP_BRIDGE_PORT="0",
                   ASSETTO_MCP_TOOLSET=toolset)
        proc = subprocess.run([sys.executable, "-c", _DUMP], cwd=ROOT,
                              env=env, capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL)
    if proc.returncode:
        sys.exit(f"could not list the {toolset} tools:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _post(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


def openai_backend(base: str):
    """First tool call from an OpenAI-compatible chat-completions server."""
    base = base.rstrip("/")
    url = base + ("" if base.endswith("/v1") else "/v1") + "/chat/completions"
    model = os.environ.get("ASSETTO_MCP_EVAL_MODEL", "local")
    key = os.environ.get("ASSETTO_MCP_EVAL_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    def call(system: str, tools: list[dict], request: str):
        out = _post(url, {
            "model": model, "temperature": 0, "max_tokens": 2048,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": request}],
            "tools": [{"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t["inputSchema"]}} for t in tools],
            "tool_choice": "auto"}, headers)
        message = out["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            return None, None, message.get("content") or ""
        fn = calls[0]["function"]
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = fn.get("arguments")
        return fn["name"], args, message.get("content") or ""
    return f"openai-compatible {url} ({model})", call


def anthropic_backend():
    try:
        import anthropic
    except ImportError:
        sys.exit('the Anthropic backend needs the SDK: '
                 'pip install -e ".[eval]"')
    client = anthropic.Anthropic()
    model = os.environ.get("ASSETTO_MCP_EVAL_MODEL", ANTHROPIC_MODEL)

    def call(system: str, tools: list[dict], request: str):
        response = client.messages.create(
            model=model, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": request}],
            tools=[{"name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t["inputSchema"]} for t in tools],
            tool_choice={"type": "auto"})
        text = " ".join(b.text for b in response.content if b.type == "text")
        for block in response.content:
            if block.type == "tool_use":
                return block.name, block.input, text
        return None, None, text
    return f"anthropic {model}", call


def backend():
    if os.environ.get("ASSETTO_MCP_EVAL_BASE_URL"):
        return openai_backend(os.environ["ASSETTO_MCP_EVAL_BASE_URL"])
    if os.environ.get("ASSETTO_MCP_EVAL") == "anthropic":
        return anthropic_backend()
    return None


def grade(case: dict, tools: dict, name, args) -> tuple[str, str]:
    """(verdict, why): pass, fail, unsafe, or n/a when the toolset has none
    of the expected tools."""
    if not set(case["expect"]) & tools.keys():
        return "n/a", "no expected tool in this toolset"
    if name is None:
        return "fail", "no tool called"
    args = args if isinstance(args, dict) else {}
    unsafe = [k for k, v in case.get("unsafe", {}).items()
              if args.get(k) == v]
    if unsafe:
        return "unsafe", f"set {', '.join(unsafe)}"
    if name not in case["expect"]:
        return "fail", ""
    schema = tools[name]["inputSchema"]
    missing = [k for k in schema.get("required", []) if k not in args]
    return "pass", f"missing {', '.join(missing)}" if missing else ""


def run(toolset: str, cases: list[dict], call) -> list[dict]:
    s = surface(toolset)
    tools = {t["name"]: t for t in s["tools"]}
    system = s["instructions"] + "\n\n" + CONTEXT
    print(f"\n{toolset}: {len(tools)} tools")
    results = []
    for case in cases:
        started = time.monotonic()
        try:
            name, args, text = call(system, s["tools"], case["request"])
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            name, args, text = None, None, f"backend error: {e}"
        verdict, why = grade(case, tools, name, args)
        results.append({"toolset": toolset, "id": case["id"],
                        "area": case["area"], "expect": case["expect"],
                        "chose": name, "args": args, "verdict": verdict,
                        "why": why or (text[:120] if name is None else ""),
                        "seconds": round(time.monotonic() - started, 1)})
        r = results[-1]
        print(f"  {verdict.upper():6} {case['id']:17} "
              f"expect {'|'.join(case['expect'])}  chose {name}"
              f"{' ' + json.dumps(args) if args else ''}"
              f"{'  -- ' + r['why'] if r['why'] else ''}")
    scored = [r for r in results if r["verdict"] != "n/a"]
    passed = sum(r["verdict"] == "pass" for r in scored)
    unsafe = sum(r["verdict"] == "unsafe" for r in scored)
    print(f"{toolset}: {passed}/{len(scored)} first calls right"
          f" ({100 * passed / max(len(scored), 1):.0f}%), {unsafe} unsafe,"
          f" {len(results) - len(scored)} not scorable")
    return results


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--toolset", choices=("full", "core", "both"),
                   default="both")
    p.add_argument("-k", help="only cases whose id or area matches")
    p.add_argument("--json", type=Path, help="write every result here")
    args = p.parse_args(argv)
    chosen = backend()
    if chosen is None:
        print("no model configured: set ASSETTO_MCP_EVAL_BASE_URL, or "
              "ASSETTO_MCP_EVAL=anthropic (see docs/DEVELOPMENT.md); "
              "skipping")
        return 0
    label, call = chosen
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    if args.k:
        cases = [c for c in cases
                 if re.search(args.k, f"{c['id']} {c['area']}")]
    print(f"{len(cases)} cases against {label}")
    toolsets = ("full", "core") if args.toolset == "both" else (args.toolset,)
    results = [r for t in toolsets for r in run(t, cases, call)]
    if args.json:
        args.json.write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
