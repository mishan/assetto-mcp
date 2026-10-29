"""The pit wall: what the bridge serves a browser at /dash."""

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (FakeCollector, make_session, post, run_module,  # noqa: E402
                     temp_db)
from test_line_map import _store  # noqa: E402
from test_mcp_protocol import _server, _session_with_laps  # noqa: E402

from assetto_mcp import bridge as B, db  # noqa: E402


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, resp.getheader("Content-Type"), body


def _bridge(path, **kw):
    br = B.Bridge(path, FakeCollector(**kw), 0)
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
            code, ctype, _ = _get(br.port, "/dash")
            assert code == 200 and ctype.startswith("text/html"), code
            assert _get(br.port, "/favicon.ico")[0] == 404
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
            # Stored out of order, so lap ids are not the lap numbers.
            c = _store(conn, sid, 3, 98_000)
            a = _store(conn, sid, 1, 100_000)
            b = _store(conn, sid, 2, 99_000)
            focus = db.set_pit_wall_focus(conn, [a, b], "T2",
                                          "lap 1 vs 2: T2 entry")
            live = json.loads(_get(br.port, "/dash/live")[2])["focus"]
            assert live == {"id": focus, "lap_ids": [a, b], "laps": [1, 2],
                            "turn": "T2", "caption": "lap 1 vs 2: T2 entry"}, live

            data = _get(br.port, "/dash/map")[2]
            drawn = json.loads(data.split("const DATA = ", 1)[1]
                               .split(";\n", 1)[0])
            assert [l["id"] for l in drawn["laps"]] == [a, b], drawn["laps"]

            db.set_pit_wall_focus(conn, [], None, None)
            data = _get(br.port, "/dash/map")[2]
            drawn = json.loads(data.split("const DATA = ", 1)[1]
                               .split(";\n", 1)[0])
            assert {l["id"] for l in drawn["laps"]} == {a, b, c}
            print("  map follows the focus, and the session without one")
        finally:
            br.stop()
            conn.close()


def _post_json(port, path, obj, ctype="application/json"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, json.dumps(obj), {"Content-Type": ctype})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def test_a_question_reaches_claude_once_and_the_watcher_every_time():
    with temp_db() as path:
        conn = db.connect(path)
        br = _bridge(path)
        try:
            assert _post_json(br.port, "/dash/ask", {"text": "x"},
                              "text/plain")[0] == 403
            assert _post_json(br.port, "/dash/ask", {"text": "  "})[0] == 400
            code, _ = _post_json(br.port, "/dash/ask", {
                "text": "why so slow\u202e here?", "place": "lap 6, T4"})
            assert code == 200 and br.app_seen_at is None
            assert db.take_pit_wall_asks(conn) == [
                {"text": "why so slow here?", "place": "lap 6, T4"}]
            assert db.take_pit_wall_asks(conn) == []
            asks = json.loads(_get(br.port, "/dash/asks?after=0")[2])["asks"]
            assert [a["text"] for a in asks] == ["why so slow here?"]
            assert json.loads(_get(br.port, "/dash/asks?after=1")[2]) == {
                "asks": []}
            print("  asked once to Claude, listed for the watcher")
        finally:
            br.stop()
            conn.close()


def test_any_reply_carries_the_drivers_question():
    """Asked through whichever instance serves the page, answered by
    whichever one the chat calls: they share only the database."""
    s = _server()
    db.add_pit_wall_ask(s._conn, "what happened at T4?", "lap 2 (id 9), T4")
    first = json.loads(s.recording_status())
    assert first["driver_asks"] == [
        {"text": "what happened at T4?", "place": "lap 2 (id 9), T4"}]
    assert "driver_asks" not in json.loads(s.recording_status())
    print("  the question rode along on the next reply, once")


def test_show_on_dashboard_checks_what_it_points_at():
    s = _server()
    sid = make_session(s._conn)
    ids = [_store(s._conn, sid, n, 100_000 - n) for n in (1, 2)]
    active, s._active_session = s._active_session, lambda *_: sid
    try:
        assert "error" in json.loads(s.show_on_dashboard(lap_ids=999_999))
        turns = [t["turn"] for t in
                 s.line_map.build(s._conn, None, ids[:2])["turns"]]
        assert "no T99" in json.loads(s.show_on_dashboard(
            lap_ids=ids[:2], turn="99"))["error"]
        out = json.loads(s.show_on_dashboard(
            lap_ids=f"{ids[0]},{ids[1]}", turn=turns[0][1:],
            caption="brake later"))
        assert out["url"].endswith("/dash"), out
        assert db.pit_wall_focus(s._conn) == {
            "id": out["focus_id"], "lap_ids": ids[:2], "turn": turns[0],
            "caption": "brake later"}
        print(f"  bad lap and bad turn refused; {turns[0]} put on screen")
    finally:
        s._active_session = active


def test_a_message_from_any_chat_reaches_the_game():
    """The game polls whichever process holds the port; the chat sending
    the message is often another one, such as beside assetto-mcp-pit-wall."""
    with temp_db() as path:
        holder = _bridge(path)
        chat = B.Bridge(path, FakeCollector(), 0)
        try:
            mid = chat.set_message("box this lap")
            shown = json.loads(_get(holder.port, "/status")[2])["message"]
            assert shown == {"id": mid, "text": "box this lap"}, shown
            assert post(holder.port, "/ack", {"id": mid})[1]["ok"] is True
            assert chat.get_message() is None
            print("  sent from one instance, shown and dismissed via another")
        finally:
            holder.stop()
            chat.stop()


def test_the_lan_dashboard_needs_its_secret_and_serves_nothing_else():
    with temp_db() as path:
        B_port, B.DASH_LAN_PORT = B.DASH_LAN_PORT, 0
        br = B.Bridge(path, FakeCollector(), 0, lan=True)
        br.start()
        time.sleep(0.2)
        try:
            assert br.lan_error is None and br.lan_url, br.lan_error
            port = int(br.lan_url.split(":")[2].split("/")[0])
            token = br.lan_url.split("/")[3]
            assert _get(port, f"/{token}/dash/live")[0] == 200
            assert _get(port, f"/{token}/dash")[0] == 200
            assert token not in _get(br.port, "/dash/live")[2]
            assert json.loads(_get(br.port, "/dash/lan")[2]) == {
                "url": br.lan_url}
            assert _get(port, "/dash/live")[0] == 403
            assert _get(port, f"/{token[:-1]}x/dash/live")[0] == 403
            assert _get(port, f"/{token}/status")[0] == 403
            assert _post_json(port, f"/{token}/note", {"tag": "note"})[0] == 403
            assert _post_json(port, f"/{token}/dash/ask",
                              {"text": "from the tablet"})[0] == 200
            assert br.app_seen_at is None
            # The same link from another instance, and after a restart.
            other = B.Bridge(path, FakeCollector(), 0, lan=True)
            assert other.lan_url.split("/")[3] == token
            print("  LAN pages behind the token; app endpoints unreachable")
        finally:
            B.DASH_LAN_PORT = B_port
            br.stop()


def test_opponents_show_where_their_latest_sample_put_them():
    with temp_db() as path:
        conn = db.connect(path)
        sid = make_session(conn)
        br = _bridge(path, session_id=sid, running=True)
        try:
            cars = [{"car_index": 1, "lap_count": 2, "spline": s / 10,
                     "speed_kmh": 150, "pos_x": 10.0 * s, "pos_z": -5.0,
                     **({"driver_name": "Ann"} if s == 1 else {})}
                    for s in (1, 2, 3)]
            cars.append({"car_index": 2, "lap_count": 2, "spline": 0.5,
                         "speed_kmh": 150})
            pit = [{"car_index": 3, "pos_x": 4.0, "pos_z": 2.0},
                   {"car_index": "x"}, "junk"]
            assert post(br.port, "/rivals", {"cars": {}, "pit": pit})[0] == 200
            assert post(br.port, "/rivals", {"cars": cars})[0] == 200
            live = json.loads(_get(br.port, "/dash/live")[2])
            assert sorted(live["cars"], key=lambda c: c["pit"]) == [
                {"name": "Ann", "x": 30.0, "z": -5.0, "pit": False},
                {"name": None, "x": 4.0, "z": 2.0, "pit": True}], live["cars"]
            print("  each opponent at its last placed sample, named;"
                  " the pit lane marked")
        finally:
            br.stop()
            conn.close()


def test_a_new_session_draws_the_track_from_the_last_one_there():
    """Race night starts with no lap: the overlay map still shows the track.

    It used to stay blank until the out-lap was in, through the grid and
    the opening laps on stream. Another layout or circuit is never used.
    """
    with temp_db() as path:
        conn = db.connect(path)
        try:
            practice = make_session(conn, track="thomson")
            lap = _store(conn, practice, 1, 121000)
            elsewhere = make_session(conn, track="riverside")
            _store(conn, elsewhere, 1, 110000)
            race = make_session(conn, track="thomson")
            conn.commit()
            out = B._track_outline(conn, race)
            assert out["lap_id"] == lap and out["x"], out
            assert B._track_outline(conn, elsewhere)["lap_id"] != lap
            alone = make_session(conn, track="laguna_seca")
            assert B._track_outline(conn, alone) == {"lap_id": None}
            own = _store(conn, race, 1, 120500)
            assert B._track_outline(conn, race)["lap_id"] == own
        finally:
            conn.close()
    print("  an empty session borrows its layout's last outline, and only"
          " until it has a lap of its own")


def test_the_pit_wall_runs_without_a_chat():
    from test_supervisor import _server_env
    with temp_db() as path:
        env = _server_env(str(Path(path).parent))
        env.pop("ASSETTO_MCP_DASH_LAN", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "assetto_mcp.pit_wall"],
            cwd=Path(__file__).resolve().parent.parent, text=True,
            env=env, stdout=subprocess.PIPE)
        # A child that never prints would leave readline blocked forever.
        stuck = threading.Timer(60, proc.kill)
        stuck.start()
        try:
            lines = [proc.stdout.readline() for _ in range(3)]
            assert lines[-1].startswith("Ctrl+C"), lines
            port = int(lines[0].split(":")[3].split("/")[0])
            code, _, body = _get(port, "/dash/live")
            assert code == 200 and "recording" in json.loads(body)
            print(f"  serving the pit wall on its own, port {port}")
            proc.terminate()
            # Windows has no SIGTERM to catch; terminate() is a hard kill.
            code = proc.wait(10)
            assert os.name == "nt" or code == 0, "SIGTERM skipped the shutdown"
        finally:
            stuck.cancel()
            proc.kill()
            proc.wait(10)
            proc.stdout.close()


def test_delta_is_against_the_sessions_best_clean_lap_at_that_point():
    s = _server()
    sid, ids = _session_with_laps()
    from assetto_mcp import analysis
    t = analysis._time_at_positions(db.get_samples(s._conn, ids[0]), [0.5])[0]
    snap = {"sim": "ac", "car": "ks_mazda_mx5_cup", "track": "mugello",
            "track_config": ""}
    active, s._active_session = s._active_session, lambda *_: sid
    try:
        assert s._delta_to_best(snap, 0.5, round(t) + 400) == 0.4
        assert s._delta_to_best({**snap, "car": "other"}, 0.5, 1) is None
    finally:
        s._active_session = active
    print("  +0.4 s at half distance; another car gets no delta")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
