"""The supervisor: one client connection across server restarts.

Claude Desktop does not start a stdio server again once it exits, so
restart_server works by having a parent process keep the client's pipes
and swap the server behind them. Everything the client would notice about
the swap is tested here against a fake server -- a few lines of Python
that answer JSON-RPC and exit when told -- so the supervisor's own
behaviour is checked without the cost or the timing of the real one. Two
tests at the end run the real server, for the parts only it can show.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module, wait_for  # noqa: E402

from assetto_mcp import supervisor as S  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# The fake server. Each run of it is one "generation": it takes the next
# number from a counter file, logs every message it receives to
# gen<N>.log, and answers:
#   initialize      -> {"gen": N}
#   echo            -> {"gen": N, "echo": params}
#   restart         -> nothing; exits with 75, as restart_server does
#   restart_after   -> answers, then exits with 75
# FAKE_START_DELAY makes generations after the first slow to start, which
# is the window in which the client's messages have to be queued.
# FAKE_CRASH makes every generation exit with that code at once.
FAKE_CHILD = r'''
import json, os, sys, time
d = os.environ["FAKE_DIR"]
counter = os.path.join(d, "gen")
gen = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
open(counter, "w").write(str(gen))
if os.environ.get("FAKE_CRASH"):
    sys.exit(int(os.environ["FAKE_CRASH"]))
if gen > 1:
    time.sleep(float(os.environ.get("FAKE_START_DELAY", "0")))
log = open(os.path.join(d, "gen%d.log" % gen), "a")
def reply(rid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                 "result": result}) + "\n")
    sys.stdout.flush()
for line in sys.stdin:
    msg = json.loads(line)
    log.write(line if line.endswith("\n") else line + "\n"); log.flush()
    m = msg.get("method")
    if m == "initialize":
        reply(msg["id"], {"gen": gen})
    elif m == "echo":
        reply(msg["id"], {"gen": gen, "echo": msg.get("params")})
    elif m == "restart":
        os._exit(75)
    elif m == "restart_after":
        reply(msg["id"], {"gen": gen})
        os._exit(75)
sys.exit(0)
'''


class Client:
    """The MCP client's end of a Supervisor running in this process."""

    def __init__(self, tmp: Path, **env):
        script = tmp / "fake_child.py"
        script.write_text(FAKE_CHILD, encoding="utf-8")
        self.dir = tmp
        r_in, self._w_in = os.pipe()
        self._r_out, w_out = os.pipe()
        self._to_sup = os.fdopen(self._w_in, "wb")
        self._from_sup = os.fdopen(self._r_out, "rb")
        child_env = dict(os.environ, FAKE_DIR=str(tmp),
                         **{k: str(v) for k, v in env.items()})
        self.sup = S.Supervisor(os.fdopen(r_in, "rb"),
                                os.fdopen(w_out, "wb"),
                                cmd=[sys.executable, str(script)],
                                env=child_env)
        self.messages: list[dict] = []
        self._lock = threading.Lock()
        self.code = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        threading.Thread(target=self._read, daemon=True).start()

    def _run(self):
        self.code = self.sup.run()
        self.sup._out.close()
        self.sup._in.close()

    def _read(self):
        for line in self._from_sup:
            with self._lock:
                self.messages.append(json.loads(line))
        self._from_sup.close()

    def send(self, obj):
        self._to_sup.write((json.dumps(obj) + "\n").encode())
        self._to_sup.flush()

    def request(self, rid, method, params=None):
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def reply_to(self, rid, timeout=15.0):
        found = []

        def seen():
            with self._lock:
                found[:] = [m for m in self.messages
                            if m.get("id") == rid and "method" not in m]
            return bool(found)
        wait_for(seen, f"a reply to {rid!r}", timeout=timeout)
        return found[0]

    def notifications(self, method):
        with self._lock:
            return [m for m in self.messages if m.get("method") == method]

    def handshake(self):
        self.request(1, "initialize", {"protocolVersion": "2025-06-18",
                                       "capabilities": {},
                                       "clientInfo": {"name": "test"}})
        assert self.reply_to(1)["result"] == {"gen": 1}
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def generation_log(self, gen) -> list[dict]:
        path = self.dir / f"gen{gen}.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line]

    def close(self, timeout=15.0):
        try:
            self._to_sup.close()
        except OSError:
            pass
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "supervisor did not exit on EOF"
        return self.code


def _tmp():
    return tempfile.TemporaryDirectory(prefix="ac-sup-")


def test_a_restart_replays_the_handshake_and_hides_its_reply():
    """The new server is initialized with the client's own request.

    The client already has an initialize reply, from the first server. The
    second one must still be initialized -- an MCP server refuses tools
    before it is -- but its reply goes to the supervisor alone: a reply for
    an id the client never sent is a protocol error on the client's side.
    """
    with _tmp() as d:
        c = Client(Path(d))
        try:
            c.handshake()
            c.request(2, "restart_after")
            assert c.reply_to(2)["result"] == {"gen": 1}
            c.request(3, "echo", {"x": 1})
            assert c.reply_to(3)["result"] == {"gen": 2, "echo": {"x": 1}}

            log = c.generation_log(2)
            methods = [m.get("method") for m in log]
            assert methods[:3] == ["initialize", "notifications/initialized",
                                   "echo"], methods
            replay = log[0]
            assert replay["id"] != 1 and str(replay["id"]).startswith(
                "__assetto_supervisor_init_"), replay
            assert replay["params"]["clientInfo"] == {"name": "test"}, replay

            replies_to_init = [m for m in c.messages
                               if m.get("id") in (1, replay["id"])]
            assert len(replies_to_init) == 1, replies_to_init
            print("  gen 2 initialized with the client's request; "
                  "its reply never reached the client")
        finally:
            assert c.close() == 0


def test_the_client_is_told_the_tools_may_have_changed():
    """So it lists them again and sees whatever the new code added."""
    with _tmp() as d:
        c = Client(Path(d))
        try:
            c.handshake()
            assert not c.notifications("notifications/tools/list_changed")
            c.request(2, "restart_after")
            c.reply_to(2)
            wait_for(lambda: c.notifications(
                "notifications/tools/list_changed"), "list_changed")
            print("  notifications/tools/list_changed sent after the restart")
        finally:
            c.close()


def test_messages_sent_during_the_restart_reach_the_new_server_in_order():
    """Nothing the client says while no server is listening is lost."""
    with _tmp() as d:
        c = Client(Path(d), FAKE_START_DELAY=0.6)
        try:
            c.handshake()
            c.request(2, "restart_after")
            c.reply_to(2)
            wait_for(lambda: (Path(d) / "gen").read_text() == "2",
                     "the second generation to start")
            for rid in (10, 11, 12):
                c.request(rid, "echo", {"n": rid})
            for rid in (10, 11, 12):
                assert c.reply_to(rid)["result"]["gen"] == 2
            echoed = [m["params"]["n"] for m in c.generation_log(2)
                      if m.get("method") == "echo"]
            assert echoed == [10, 11, 12], echoed
            # And the handshake went first, ahead of the queued requests.
            assert c.generation_log(2)[0]["method"] == "initialize"
            print("  three queued requests answered by gen 2, in order")
        finally:
            c.close()


def test_a_request_the_old_server_never_answered_gets_an_error():
    """Otherwise it hangs in the client until the client's own timeout."""
    with _tmp() as d:
        c = Client(Path(d))
        try:
            c.handshake()
            c.request(7, "restart")         # exits without answering
            reply = c.reply_to(7)
            assert reply["error"]["code"] == -32603, reply
            assert "retry" in reply["error"]["message"], reply
            c.request(8, "echo")
            assert c.reply_to(8)["result"]["gen"] == 2
            print("  unanswered request 7 got -32603; gen 2 serves the next")
        finally:
            c.close()


def test_restart_server_lost_reply_is_not_an_invitation_to_retry():
    """The one in-flight call that must not say "retry" is the restart.

    Its reply is normally written before the process goes. If it is not,
    an error telling the model to retry would restart the server again,
    and the reply to that could be lost the same way.
    """
    with _tmp() as d:
        c = Client(Path(d))
        try:
            c.handshake()
            c.request(4, "tools/call", {"name": "restart_server",
                                        "arguments": {}})
            c.request(5, "restart")
            reply = c.reply_to(4)
            assert "result" in reply, reply
            text = json.loads(reply["result"]["content"][0]["text"])
            assert text["restarting"] is True, text
            assert "error" in c.reply_to(5), "other calls still get the error"
            print("  a lost restart_server reply is answered as done")
        finally:
            c.close()


def test_a_crashing_server_is_retried_then_given_up_on():
    """A server that cannot start must end the connection, visibly.

    Restarting it forever would leave the client on a connection that
    answers nothing, which looks like a hang rather than a failure.
    """
    saved = S.BACKOFF
    S.BACKOFF = (0.01, 0.01, 0.01)
    try:
        with _tmp() as d:
            c = Client(Path(d), FAKE_CRASH=3)
            wait_for(lambda: c.code is not None, "the supervisor to give up")
            assert c.code == 3, c.code
            starts = int((Path(d) / "gen").read_text())
            assert starts == S.MAX_CRASHES + 1, starts
            c.close()
            print(f"  {starts} starts, then exit 3")
    finally:
        S.BACKOFF = saved


def test_closing_the_client_ends_the_server_and_the_supervisor():
    with _tmp() as d:
        c = Client(Path(d))
        c.handshake()
        proc = c.sup._proc
        assert c.close() == 0
        assert proc.poll() is not None, "the server outlived its client"
        print("  stdin EOF: server exited, supervisor returned 0")


def test_the_supervisor_imports_neither_the_server_nor_mcp():
    """It is the process that is never replaced, so it must be cheap.

    Importing the server would open the database and start a collector in
    a process that never uses them, and mcp is most of the startup time.
    """
    code = ("import sys, assetto_mcp.supervisor; "
            "bad = [m for m in ('mcp', 'assetto_mcp.server') "
            "if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)")
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    print("  importing the supervisor pulls in neither")


def test_running_the_server_module_starts_the_supervisor():
    """Existing client configs run `-m assetto_mcp.server` and must get it.

    The child command is pointed at the fake server, so a reply from it
    proves the handoff happened before the real server was imported.
    """
    with _tmp() as d:
        script = Path(d) / "fake_child.py"
        script.write_text(FAKE_CHILD, encoding="utf-8")
        env = dict(os.environ, FAKE_DIR=d,
                   ASSETTO_MCP_CHILD_CMD=json.dumps(
                       [sys.executable, str(script)]))
        env.pop("ASSETTO_MCP_CHILD", None)
        env.pop("ASSETTO_MCP_NO_SUPERVISOR", None)
        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {}}
        proc = subprocess.run(
            [sys.executable, "-m", "assetto_mcp.server"], cwd=ROOT, env=env,
            input=(json.dumps(req) + "\n").encode(), capture_output=True,
            timeout=60)
        assert proc.returncode == 0, proc.stderr.decode()
        reply = json.loads(proc.stdout.decode().splitlines()[0])
        assert reply["result"] == {"gen": 1}, reply
        print("  -m assetto_mcp.server ran the child command, supervised")


# --- the real server --------------------------------------------------------


def _mcp_missing() -> bool:
    try:
        import mcp  # noqa: F401
        return False
    except ImportError:
        return True


def _server_env(data: str, **extra) -> dict:
    env = dict(os.environ, ASSETTO_MCP_DATA=data,
               ASSETTO_MCP_NO_AUTOSTART="1", ASSETTO_MCP_BRIDGE_PORT="0",
               **extra)
    for k in ("ASSETTO_MCP_CHILD", "ASSETTO_MCP_CHILD_CMD",
              "ASSETTO_MCP_NO_SUPERVISOR"):
        if k not in extra:
            env.pop(k, None)
    return env


# Runs inside a real server process with the collector's state set by hand,
# since there is no game to put it in a live session.
_REFUSALS = r'''
import json, os
import assetto_mcp.server as s
out = {"unsupervised": json.loads(s.restart_server())}
os.environ["ASSETTO_MCP_CHILD"] = "1"
c = s._collector
c.holds_recorder, c.session_id, c.live = True, 42, True
out["recording"] = json.loads(s.restart_server())
fired = []
s._restart_soon = lambda: fired.append(True)
out["forced"] = json.loads(s.restart_server(force=True))
out["fired"] = fired
c.holds_recorder, c.session_id, c.live = False, None, False
s._shutdown()
print(json.dumps(out))
'''


def test_restart_server_refuses_when_it_cannot_or_should_not():
    if _mcp_missing():
        from lua_harness import skip
        skip("mcp is not installed")
        return
    with _tmp() as d:
        proc = subprocess.run(
            [sys.executable, "-c", _REFUSALS], cwd=ROOT,
            env=_server_env(str(Path(d) / "data")), capture_output=True,
            text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout.strip().splitlines()[-1])

        unsup = out["unsupervised"]
        assert unsup["restarting"] is False, unsup
        assert unsup["refused"] == "not_supervised", unsup
        assert "quit" in unsup["ask_the_driver"], unsup

        rec = out["recording"]
        assert rec["refused"] == "recording", rec
        assert rec["session_id"] == 42, rec

        forced = out["forced"]
        assert forced["restarting"] is True, forced
        assert "forced" in forced and "session_note" in forced, forced
        assert out["fired"] == [True], out
        print("  unsupervised: refused; recording: refused; forced: restarts")


def test_the_real_server_restarts_behind_one_connection():
    """End to end: restart_server, then the same pipes keep working."""
    if _mcp_missing():
        from lua_harness import skip
        skip("mcp is not installed")
        return
    with _tmp() as d:
        proc = subprocess.Popen(
            [sys.executable, "-m", "assetto_mcp.server"], cwd=ROOT,
            env=_server_env(str(Path(d) / "data")),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        got: list[dict] = []
        threading.Thread(target=lambda: [got.append(json.loads(line))
                                         for line in proc.stdout],
                         daemon=True).start()

        def send(obj):
            proc.stdin.write((json.dumps(obj) + "\n").encode())
            proc.stdin.flush()

        def reply(rid):
            wait_for(lambda: any(m.get("id") == rid for m in got),
                     f"reply {rid}", timeout=60)
            return next(m for m in got if m.get("id") == rid)

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18",
                             "capabilities": {},
                             "clientInfo": {"name": "test", "version": "0"}}})
            assert "result" in reply(1), reply(1)
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "restart_server", "arguments": {}}})
            text = json.loads(reply(2)["result"]["content"][0]["text"])
            assert text["restarting"] is True, text
            wait_for(lambda: any(m.get("method") ==
                                 "notifications/tools/list_changed"
                                 for m in got), "list_changed", timeout=60)
            send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
            names = {t["name"] for t in reply(3)["result"]["tools"]}
            assert "restart_server" in names, sorted(names)
            proc.stdin.close()
            assert proc.wait(timeout=30) == 0
            print(f"  restarted; {len(names)} tools listed on the same pipes")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdin.close()
            proc.stdout.close()


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
