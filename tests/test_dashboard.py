"""The pit wall: what the bridge serves a browser at /dash."""

import http.client
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FakeCollector, make_session, run_module, temp_db  # noqa: E402
from test_line_map import _store  # noqa: E402

from assetto_mcp import bridge as B, db  # noqa: E402


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, resp.getheader("Content-Type"), body


def _bridge(path):
    br = B.Bridge(path, FakeCollector(), 0)
    br.start()
    time.sleep(0.2)
    assert br.error is None, br.error
    return br


def test_the_page_polls_without_passing_for_the_in_game_app():
    """session_check reports the app connected from its polls; a browser
    tab left open must not stand in for it."""
    with temp_db() as path:
        br = _bridge(path)
        try:
            code, ctype, page = _get(br.port, "/dash")
            assert code == 200 and ctype.startswith("text/html"), code
            assert "prefers-color-scheme: dark" in page
            code, _, body = _get(br.port, "/dash/live")
            assert code == 200, code
            assert json.loads(body)["live"] == {"error": "starting"}
            assert br.app_seen_at is None
            print("  /dash served; its polls leave the app link alone")
        finally:
            br.stop()


def test_the_game_closed_is_a_reading_not_a_failure():
    def closed():
        raise RuntimeError("the game does not appear to be running")

    with temp_db() as path:
        br = _bridge(path)
        br.live = closed
        try:
            code, _, body = _get(br.port, "/dash/live")
            assert code == 200, code
            assert "not appear to be running" in json.loads(body)["live"]["error"]
            print("  game closed reported in the live panel")
        finally:
            br.stop()


def test_the_map_draws_what_claude_put_on_screen():
    with temp_db() as path:
        conn = db.connect(path)
        br = _bridge(path)
        try:
            code, _, page = _get(br.port, "/dash/map")
            assert code == 200 and "no session" in page.lower(), page[:200]

            sid = make_session(conn)
            a = _store(conn, sid, 1, 100_000)
            b = _store(conn, sid, 2, 99_000)
            c = _store(conn, sid, 3, 98_000)
            focus = br.set_focus([a, b], "lap 1 vs 2: T2 entry")
            live = json.loads(_get(br.port, "/dash/live")[2])["focus"]
            assert live == {"id": focus, "lap_ids": [a, b],
                            "caption": "lap 1 vs 2: T2 entry"}, live

            data = _get(br.port, "/dash/map")[2]
            drawn = json.loads(data.split("const DATA = ", 1)[1]
                               .split(";\n", 1)[0])
            assert [l["id"] for l in drawn["laps"]] == [a, b], drawn["laps"]

            br.set_focus([], None)
            data = _get(br.port, "/dash/map")[2]
            drawn = json.loads(data.split("const DATA = ", 1)[1]
                               .split(";\n", 1)[0])
            assert {l["id"] for l in drawn["laps"]} == {a, b, c}
            print("  map follows the focus, and the session without one")
        finally:
            br.stop()
            conn.close()


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
