"""The server as an MCP client meets it: handshake, tool list, results.

Everything goes through a real client session over the SDK's in-memory
streams, so the SDK's own dispatch -- worker threads on 2.x, the error
conversion, the initialize reply -- is what is under test, and results
are read as their wire form.
"""

import asyncio
import atexit
import contextlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402
from test_delta_and_corners import _lap  # noqa: E402

_SERVER = None


def _server():
    global _SERVER
    if _SERVER is None:
        d = tempfile.mkdtemp(prefix="ac-protocol-")
        os.environ.setdefault("ASSETTO_MCP_DATA", d)
        os.environ.setdefault("AC_DOCS_DIR", d)
        os.environ.setdefault("ASSETTO_MCP_BRIDGE_PORT", "0")
        os.environ.setdefault("ASSETTO_MCP_NO_AUTOSTART", "1")
        import importlib
        _SERVER = importlib.import_module("assetto_mcp.server")
        atexit.register(_SERVER._bridge.stop)
    return _SERVER


@contextlib.asynccontextmanager
async def _client(**session_options):
    """A client session on the server, and its initialize result."""
    import anyio
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    mcp = _server().mcp
    low = getattr(mcp, "_lowlevel_server", None) or mcp._mcp_server
    async with create_client_server_memory_streams() as (client, server):
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: low.run(
                server[0], server[1], low.create_initialization_options()))
            async with ClientSession(client[0], client[1],
                                     **session_options) as session:
                yield session, _wire(await session.initialize())
            tg.cancel_scope.cancel()


def _wire(model) -> dict:
    """What the client receives: field names as the spec spells them."""
    return model.model_dump(by_alias=True, exclude_none=True)


def _session_with_laps(n: int = 4) -> tuple[int, list[int]]:
    from assetto_mcp import db
    s = _server()
    sid = db.create_session(s._conn, car="ks_mazda_mx5_cup", track="mugello",
                            track_config="", tyre_compound="SM",
                            air_temp=24.0, road_temp=31.0)
    s._conn.execute("UPDATE sessions SET track_length_m = 5245 WHERE id = ?",
                    (sid,))
    ids = []
    for lap in range(1, n + 1):
        rows = [(r["t_ms"], r["norm_pos"], r["speed_kmh"], r["gas"],
                 r["brake"], r["steer"], r["gear"], 7000, r["acc_lat"],
                 r["acc_lon"], r["slip_fl"], r["slip_fr"], r["slip_rl"],
                 r["slip_rr"], r["press_fl"], r["press_fr"], r["press_rl"],
                 r["press_rr"], r["core_fl"], r["core_fr"], r["core_rl"],
                 r["core_rr"], r["ride_f"], r["ride_r"], 0)
                for r in _lap(n=600, lap_ms=113000 + 150 * lap)]
        ids.append(db.store_lap(s._conn, sid, lap, 113000 + 150 * lap, True,
                                rows))
    s._conn.commit()
    return sid, ids


def test_parallel_calls_on_shared_data_answer_as_sequential_ones_do():
    """2.x runs each sync tool on its own worker thread, all of them on one
    database connection. Interleaved, they read each other's rows: "no lap
    with id 3" for a lap that exists."""
    sid, ids = _session_with_laps()
    calls = []
    for lap_id in ids:
        calls += [("lap_summary", {"lap_id": lap_id}),
                  ("compare_lap_corners", {"lap_id_a": ids[0], "lap_id_b": lap_id}),
                  ("list_laps", {"session_id": sid}),
                  ("track_corners", {"session_id": sid})]
    calls *= 3

    async def run():
        async with _client() as (session, _):
            one_by_one = [_wire(await session.call_tool(n, a))
                          for n, a in calls]
            together = await asyncio.gather(
                *(session.call_tool(n, a) for n, a in calls))
            return one_by_one, [_wire(r) for r in together]

    one_by_one, together = asyncio.run(run())
    assert not any(r.get("isError") for r in one_by_one), one_by_one
    for call, a, b in zip(calls, one_by_one, together):
        assert a == b, (call, a["content"][0]["text"][:200],
                        b["content"][0]["text"][:200])


def test_a_result_is_compact_json_text_sent_once():
    sid, ids = _session_with_laps(2)

    async def run():
        async with _client() as (session, _):
            tools = _wire(await session.list_tools())["tools"]
            result = _wire(await session.call_tool(
                "lap_summary", {"lap_id": ids[0]}))
            return tools, result

    tools, result = asyncio.run(run())
    assert not [t["name"] for t in tools if "outputSchema" in t]
    assert "structuredContent" not in result, result.keys()
    text = result["content"][0]["text"]
    assert text == json.dumps(json.loads(text), separators=(",", ":")), \
        text[:200]


def test_a_status_report_is_not_a_failed_call_but_a_refusal_is():
    """A status tool asked "is it working?" answers, even when the answer
    is no: AC not open yet, GitHub unreachable. A refusal is a failed call
    whose text is the JSON payload."""
    s = _server()
    status = [("recording_status", {}), ("bridge_status", {}),
              ("suspension_capture_status", {}), ("check_for_updates", {})]
    refusals = [("lap_summary", {"lap_id": -1}),
                ("compare_lap_corners", {"lap_id_a": -1, "lap_id_b": -2}),
                ("read_setup", {"car": "nope", "track": "nope",
                                "name": "nope"})]

    async def run():
        async with _client() as (session, _):
            return ([_wire(await session.call_tool(n, a)) for n, a in status],
                    [_wire(await session.call_tool(n, a))
                     for n, a in refusals])

    saved = s._collector.last_error, s._bridge.error
    os.environ["ASSETTO_MCP_UPDATE_API"] = "http://127.0.0.1:9"
    s._collector.last_error = "shared memory not found: AC is not running"
    s._bridge.error = "port 9666 in use"
    try:
        answered, refused = asyncio.run(run())
    finally:
        s._collector.last_error, s._bridge.error = saved
        del os.environ["ASSETTO_MCP_UPDATE_API"]
    for (name, _), r in zip(status, answered):
        assert not r.get("isError"), (name, r)
    for name in ("recording_status", "bridge_status", "check_for_updates"):
        r = answered[[n for n, _ in status].index(name)]
        assert json.loads(r["content"][0]["text"])["error"], (name, r)
    for (name, _), r in zip(refusals, refused):
        assert r.get("isError"), (name, r)
        assert json.loads(r["content"][0]["text"])["error"], (name, r)


def test_every_tool_says_what_it_does_to_the_world():
    """An absent hint means destructive and open-world, so every tool sets
    all four; the tools that need the driver's OK are the destructive
    ones, and nothing read-only is."""
    async def run():
        async with _client() as (session, _):
            return _wire(await session.list_tools())["tools"]

    tools = asyncio.run(run())
    hints = ("readOnlyHint", "destructiveHint", "idempotentHint",
             "openWorldHint")
    for t in tools:
        a = t.get("annotations", {})
        assert all(h in a for h in hints), (t["name"], a)
        assert t.get("title") or a.get("title"), t["name"]
        assert not (a["readOnlyHint"] and a["destructiveHint"]), t["name"]
    destructive = {t["name"] for t in tools
                   if t["annotations"]["destructiveHint"]}
    assert {"write_setup", "restart_server", "apply_update",
            "forget_display_value"} <= destructive, destructive


def test_initialize_carries_instructions_version_and_list_changed():
    """The supervisor announces new tools after a restart; a client only
    listens for that when the capability is declared."""
    async def run():
        async with _client() as (session, init):
            return init, _wire(await session.list_tools())["tools"]

    init, tools = asyncio.run(run())
    assert init["capabilities"]["tools"]["listChanged"] is True, init
    assert init["serverInfo"]["version"], init["serverInfo"]
    named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", init["instructions"]))
    assert named, init["instructions"]
    assert named <= {t["name"] for t in tools}, named


def test_prompts_and_resources_serve_the_skill_and_the_data():
    """A client with no skill gets the routine as prompts, cut from the
    skill's own sections, and the skill and the data as resources."""
    from assetto_mcp import guide
    sid, ids = _session_with_laps(2)
    prompts = [("start_session", {"mode": "live"}), ("start_session", {}),
               ("debrief", {"session_id": str(sid)}), ("debrief", {}),
               ("ab_test", {"change": "rear bar +1"}),
               ("race_prep", {"race_laps": "20"}), ("race_prep", {})]

    async def run():
        async with _client() as (session, _):
            listed = _wire(await session.list_prompts())["prompts"]
            texts = [_wire(await session.get_prompt(n, a))
                     ["messages"][0]["content"]["text"] for n, a in prompts]
            skill = _wire(await session.read_resource(
                "assetto://guide/race-engineer"))["contents"][0]["text"]
            laps = _wire(await session.read_resource(
                f"assetto://session/{sid}/laps"))["contents"][0]["text"]
            return listed, texts, skill, laps

    listed, texts, skill, laps = asyncio.run(run())
    assert {p["name"] for p in listed} == {n for n, _ in prompts}, listed
    for (name, _), text in zip(prompts, texts):
        assert "\n## " in text, (name, text[:200])
    assert "## Session start" in texts[0] and "live mode" in texts[0]
    assert "debrief mode" in texts[1], texts[1][:100]
    assert f"session {sid}" in texts[2], texts[2][:100]
    assert skill == (guide.SKILL_DIR / "SKILL.md").read_text(
        encoding="utf-8")
    assert {lap["id"] for lap in json.loads(laps)} == set(ids), laps


def test_the_driver_confirms_where_the_client_can_ask():
    """With elicitation the driver's answer replaces the override flags, so
    a model that set one itself still has to get a yes. A client that
    cannot ask gets today's refusal, and the flag still works."""
    from mcp import types
    from assetto_mcp import supervisor, update
    s = _server()
    docs = Path(tempfile.mkdtemp(prefix="ac-confirm-"))
    write = {"car": "c", "track": "t", "name": "v1",
             "values_json": '{"PRESSURE_LF": 25}', "allow_unclamped": True}
    (docs / "setups" / "c" / "t").mkdir(parents=True)
    existing = docs / "setups" / "c" / "t" / "v1.ini"
    applied, restarted = [], []

    def answering(yes):
        async def callback(context, params):
            asked.append(params.message)
            return types.ElicitResult(action="accept",
                                      content={"confirm": yes})
        return None if yes is None else callback

    # (driver's answer, or None when the client cannot ask; tool, arguments;
    #  whether it went ahead)
    cases = [
        (None, "write_setup", write, False),
        (None, "write_setup", {**write, "overwrite": True}, True),
        (True, "write_setup", write, True),
        (False, "write_setup", {**write, "overwrite": True}, False),
        (None, "restart_server", {}, False),
        (True, "restart_server", {}, True),
        (False, "restart_server", {"force": True}, False),
        (True, "apply_update", {"to_commit": "abc1234"}, True),
        (False, "apply_update", {"to_commit": "abc1234"}, False),
    ]

    async def call(yes, name, arguments):
        async with _client(elicitation_callback=answering(yes)) as (c, _):
            return _wire(await c.call_tool(name, arguments))

    saved = (s.AC_DOCS_DIR, s._restart_soon, update.apply_in_background,
             s._live_session, s._collector.holds_recorder, s._collector.session_id,
             s._collector.live)
    s.AC_DOCS_DIR = docs
    s._restart_soon = lambda: restarted.append(True)
    update.apply_in_background = (
        lambda *a, **kw: applied.append(True) or {"applied": True})
    os.environ[supervisor.CHILD_ENV] = "1"
    s._collector.holds_recorder, s._collector.session_id = True, 42
    s._collector.live = True
    # Earlier tests leave sessions open, which reads as another instance on
    # track, and an update is never offered then.
    s._live_session = lambda: None
    try:
        for yes, name, arguments, went_ahead in cases:
            existing.write_text("[PRESSURE_LF]\nVALUE=30\n")
            asked, applied[:], restarted[:] = [], [], []
            r = asyncio.run(call(yes, name, arguments))
            out = json.loads(r["content"][0]["text"])
            case = (yes, name, arguments, out)
            did = {"write_setup": "VALUE=25" in existing.read_text(),
                   "restart_server": bool(restarted),
                   "apply_update": bool(applied)}[name]
            assert did == went_ahead, case
            assert bool(r.get("isError")) != went_ahead, case
            assert bool(asked) == (yes is not None), case
            if yes is False:
                assert out.get("declined") or out["refused"] == "declined", \
                    case
                assert "ask_the_driver" not in out, case
            if not went_ahead:
                assert not [k for k in out if "overwrite" in k
                            or "force" in k or "unclamped" in k], case
    finally:
        (s.AC_DOCS_DIR, s._restart_soon, update.apply_in_background,
         s._live_session, s._collector.holds_recorder, s._collector.session_id,
         s._collector.live) = saved
        del os.environ[supervisor.CHILD_ENV]


def test_the_core_toolset_serves_only_its_tools():
    script = (
        "import asyncio, json\n"
        "import assetto_mcp.server as s\n"
        "print(json.dumps([sorted(t.name for t in "
        "asyncio.run(s.mcp.list_tools())), list(s.CORE_TOOLS)]))\n"
        "s._shutdown()\n")
    import subprocess
    with tempfile.TemporaryDirectory(prefix="ac-core-") as d:
        env = dict(os.environ, ASSETTO_MCP_DATA=d, AC_DOCS_DIR=d,
                   ASSETTO_MCP_NO_AUTOSTART="1", ASSETTO_MCP_BRIDGE_PORT="0",
                   ASSETTO_MCP_TOOLSET="core")
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parent.parent, env=env,
            capture_output=True, text=True, timeout=120,
            stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    served, core = json.loads(proc.stdout.strip().splitlines()[-1])
    assert served == sorted(core), (served, core)


def test_a_renamed_tool_still_answers_under_its_old_name_unlisted():
    """A client holding an old tool list keeps working, and is told the
    new name; the list itself carries each tool once."""
    s = _server()
    calls = {"get_driver_notes": {},
             "delta_by_position": {"lap_id_a": -1, "lap_id_b": -2},
             "compare_laps": {"lap_id_a": -1, "lap_id_b": -2},
             "locate": {"meters": 100},
             "record_display_value": {"field": "X", "displayed": 1.0,
                                      "stored": 1.0, "car": "alias_car"},
             "record_display_range": {"field": "X", "displayed_at_min": 0,
                                      "displayed_at_max": 1,
                                      "car": "alias_car"},
             "record_display_note": {"field": "X", "note": "n",
                                     "car": "alias_car"}}
    assert set(calls) == set(s._RENAMED)

    async def run():
        async with _client() as (session, _):
            names = {t["name"]
                     for t in _wire(await session.list_tools())["tools"]}
            return names, {old: _wire(await session.call_tool(old, a))
                           for old, a in calls.items()}

    names, replies = asyncio.run(run())
    assert not names & set(s._RENAMED), names & set(s._RENAMED)
    assert set(s._RENAMED.values()) <= names
    for old, r in replies.items():
        reply = json.loads(r["content"][0]["text"])
        assert s._RENAMED[old] in reply.get("deprecated", ""), (old, reply)


def test_write_setup_takes_its_values_as_an_object_or_as_json_text():
    values = {"PRESSURE_LF": 25}

    async def run():
        async with _client() as (session, _):
            return [_wire(await session.call_tool("write_setup", {
                "car": "no_ranges_car", "track": "t", "name": "x",
                "values_json": v})) for v in (values, json.dumps(values))]

    as_object, as_text = asyncio.run(run())
    for r in (as_object, as_text):
        assert json.loads(r["content"][0]["text"])["refused"] == \
            "no_ranges", r
    assert as_object == as_text


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
