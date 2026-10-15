"""List arguments in every shape a client sends them.

A client may send "14" as the integer 14, "14,5" as the list [14, 5], and
"433,434" as text that looks like one number with a thousands separator.
These pin that a number, a list and the text all work, for ids and for
positions.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402


def _server():
    os.environ.setdefault("ASSETTO_MCP_NO_AUTOSTART", "1")
    os.environ.setdefault("ASSETTO_MCP_BRIDGE_PORT", "0")
    from assetto_mcp import server
    return server


def test_ids_read_from_text_numbers_and_lists():
    s = _server()
    for given in ("433,434", "433, 434", "433 434", [433, 434],
                  (433, 434), ["433", "434"], [433.0, 434.0]):
        assert s._whole_numbers(given) == [433, 434], given
    assert s._whole_numbers(14) == [14]
    assert s._whole_numbers(None) == []
    assert s._whole_numbers("1,234") == [1, 234]


def test_a_fractional_id_is_refused_in_words():
    s = _server()
    try:
        s._whole_numbers([14.5])
    except ValueError as e:
        assert "whole number" in str(e), e
    else:
        raise AssertionError("14.5 accepted as an id")


def test_positions_still_refuse_an_ambiguous_thousands_separator():
    s = _server()
    try:
        s._parse_numbers("3,335")
    except ValueError as e:
        assert "thousands" in str(e), e
    else:
        raise AssertionError('"3,335" read without complaint')
    assert s._parse_numbers([3335, 3520.5]) == [3335.0, 3520.5]
    assert s._parse_numbers(0.25) == [0.25]


def test_id_text_for_the_lap_id_parsers():
    s = _server()
    assert s._as_id_text([87, 88]) == "87,88"
    assert s._as_id_text(87) == "87"
    assert s._as_id_text(87.0) == "87"
    assert s._as_id_text("87-90") == "87-90"
    assert s._parse_lap_ids(s._as_id_text([87, 88]))[0] == [87, 88]


def _call(name, arguments):
    """The tool's reply, by way of the SDK's argument validation.

    A refusal comes back as the error result's text, so arguments that
    pass validation but name no lap still read as a reply.
    """
    s = _server()
    try:
        r = asyncio.run(s.mcp.call_tool(name, arguments))
    except s.ToolError as e:
        if not str(e).startswith("{"):
            raise
        return str(e)
    return _text(r)


def _text(r):
    content = getattr(r, "content", r)  # 2.x result, 1.x content list
    if isinstance(content, tuple):  # 1.x with structured output
        content = content[0]
    return content[0].text


def test_the_tools_take_a_number_or_a_list_through_mcp():
    """The client's coercion is refused before any parser runs unless the
    parameter's type admits it."""
    calls = [("field_benchmark", {"lap_ids": v, "car_indexes": v})
             for v in (14, [14, 5], "14,5")]
    calls += [("label_laps", {"lap_ids": v, "setup_name": "a"})
              for v in (87, [87, 88], "87-90")]
    calls += [("compare_runs", {"baseline_laps": v, "candidate_laps": v})
              for v in (87, [87, 88], "87,88")]
    calls += [("locate", {"meters": v, "fractions": v})
              for v in (0.25, [0.25, 0.5], "0.25, 0.5")]
    for name, arguments in calls:
        try:
            reply = json.loads(_call(name, arguments))
        except Exception as e:
            raise AssertionError(f"{name}({arguments}): {e}") from None
        assert isinstance(reply, dict), (name, arguments, reply)


def test_a_reply_with_a_null_error_field_is_not_an_error_result():
    reply = json.loads(_text(asyncio.run(
        _server().mcp.call_tool("recording_status", {}))))
    assert "error" in reply and reply["error"] is None, reply


def test_a_refusal_is_an_error_result_with_the_payload_as_its_text():
    s = _server()
    try:
        asyncio.run(s.mcp.call_tool("lap_summary", {"lap_id": -1}))
    except s.ToolError as e:
        assert json.loads(str(e))["error"], e
    else:
        raise AssertionError("lap_summary of a missing lap did not fail")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
