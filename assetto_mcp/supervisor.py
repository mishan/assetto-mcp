"""Keep one MCP connection open across server restarts.

`python -m assetto_mcp.server` lands here first. This process owns the
client's stdin and stdout for the whole session and runs the real server as
a child, passing newline-delimited JSON-RPC through in both directions. When
the child asks to be replaced -- the restart_server tool exits with
RESTART_EXIT_CODE -- a fresh one is started behind the same pipes, given the
client's own `initialize` again, and the client is told the tool list may
have changed. From the client's side the connection never dropped.

It has to be done this way because nothing else works:

* Claude Desktop does not respawn a stdio server that exits. The driver
  has to quit it from the tray and reopen it, mid-session, to pick up a
  fix.
* os.execv on Windows is not an exec. It spawns a new process and ends
  the old one, and the client's pipes end with it.
* importlib.reload of the server module re-runs its import-time work -- a
  second database connection, a second collector thread, a second bridge
  fighting the first for port 9666 -- while the old ones keep running.

Standard library only, and nothing from the server is imported here: the
whole point is that this process never needs replacing itself.
"""

import json
import os
import subprocess
import sys
import threading
import time

# EX_TEMPFAIL. Anything that is not this and not a clean exit on client EOF
# is a crash, and gets the backoff rather than an immediate replacement.
RESTART_EXIT_CODE = 75

CHILD_ENV = "ASSETTO_MCP_CHILD"
CHILD_CMD_ENV = "ASSETTO_MCP_CHILD_CMD"
NO_SUPERVISOR_ENV = "ASSETTO_MCP_NO_SUPERVISOR"

# A replacement imports the whole server, opens the database and waits for
# the collector to announce itself, which can sit behind SQLite's lock wait.
# Generous, because giving up here ends the client's connection.
INIT_TIMEOUT = 60.0

# Crashes, as opposed to requested restarts. Three inside a minute is a
# server that cannot start, and the client is better told so -- by this
# process exiting -- than kept on a connection that answers nothing.
MAX_CRASHES = 3
CRASH_WINDOW = 60.0
BACKOFF = (0.5, 1.0, 2.0)

# How long a child gets to finish on its own once the client has gone.
# It stops the collector on the way out, which hands the recorder claim to
# another instance at once instead of after it goes stale.
EOF_GRACE = 5.0

_INIT_ID_PREFIX = "__assetto_supervisor_init_"
_RESTARTED = "server restarted; retry"


def _log(text: str) -> None:
    # stderr only. stdout is the client's JSON-RPC channel, and one stray
    # line there ends the connection.
    try:
        sys.stderr.write(f"assetto-mcp supervisor: {text}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _child_cmd() -> list[str]:
    raw = os.environ.get(CHILD_CMD_ENV)
    if raw:
        cmd = json.loads(raw)
        if not (isinstance(cmd, list) and cmd
                and all(isinstance(a, str) for a in cmd)):
            raise ValueError(f"{CHILD_CMD_ENV} must be a JSON list of strings")
        return cmd
    return [sys.executable, "-m", "assetto_mcp.server"]


# --- Windows: no console window, and no orphans --------------------------

_job = None


def _windows_job():
    """A job object that kills its processes when this one goes away.

    Without it, a supervisor killed from outside -- Claude Desktop ending
    the process tree badly, Task Manager -- leaves the child running,
    holding the recorder claim and port 9666. That orphan is exactly what
    the bridge's exclusive bind was written to diagnose. With
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE the kernel closes our handle when we
    die, however we die, and takes the child with it.

    Best-effort: None if any call fails, and the child runs unjobbed.
    """
    global _job
    if _job is not None or sys.platform != "win32":
        return _job
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)

        class _Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class _Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _Basic),
                        ("IoInfo", _IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return None
        _job = (k32, job)
    except Exception as e:  # noqa: BLE001 - optional hardening
        _log(f"no job object, a crash may orphan the server: {e}")
        return None
    return _job


def _adopt(proc: subprocess.Popen) -> None:
    job = _windows_job()
    if job is None:
        return
    k32, handle = job
    try:
        from ctypes import wintypes
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE,
                                                 wintypes.HANDLE)
        if not k32.AssignProcessToJobObject(handle, int(proc._handle)):
            _log("could not put the server in a job object")
    except Exception as e:  # noqa: BLE001
        _log(f"could not put the server in a job object: {e}")


# --- the pump ------------------------------------------------------------


def _parse(line: bytes):
    try:
        return json.loads(line)
    except ValueError:
        return None


def _messages(msg) -> list[dict]:
    """The JSON-RPC messages in one line: a batch is a list of them."""
    if isinstance(msg, dict):
        return [msg]
    if isinstance(msg, list):
        return [m for m in msg if isinstance(m, dict)]
    return []


def _encode(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"


class Supervisor:
    def __init__(self, stdin, stdout, cmd: list[str] | None = None,
                 env: dict | None = None):
        self._in = stdin
        self._out = stdout
        self._cmd = cmd or _child_cmd()
        self._env = dict(os.environ if env is None else env)
        self._env[CHILD_ENV] = "1"

        self._out_lock = threading.Lock()
        # Guards everything below. The child's stdin has its own lock so a
        # slow write cannot block the bookkeeping.
        self._lock = threading.Lock()
        self._child_in_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._ready = False          # False while a replacement initializes
        self._queue: list[bytes] = []
        # Client requests the current child has not answered: id -> the
        # tool being called, if it is a tools/call.
        self._in_flight: dict = {}
        self._init_request: dict | None = None
        self._initialized: dict | None = None
        self._init_seq = 0
        self._init_done = threading.Event()
        self.eof = threading.Event()

    # -- writing ----------------------------------------------------------

    def _to_client(self, data: bytes) -> None:
        with self._out_lock:
            try:
                self._out.write(data)
                self._out.flush()
            except (OSError, ValueError):
                # The client is gone; its stdin closing will say so.
                pass

    def _to_child(self, proc, data: bytes) -> bool:
        with self._child_in_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
                return True
            except (OSError, ValueError, AttributeError):
                # Close it while we hold the error, or the unflushed bytes
                # raise BrokenPipeError again from the garbage collector.
                try:
                    proc.stdin.close()
                except (OSError, ValueError, AttributeError):
                    pass
                return False

    # -- client -> child --------------------------------------------------

    def _track(self, msg: dict) -> None:
        """Note what a client message means for a restart. Under _lock."""
        method = msg.get("method")
        if method is None:
            return                   # a response to a server request
        if "id" in msg:
            if method == "initialize":
                self._init_request = msg
            tool = None
            if method == "tools/call":
                tool = (msg.get("params") or {}).get("name")
            self._in_flight[_key(msg["id"])] = (msg["id"], tool)
        elif method == "notifications/initialized":
            self._initialized = msg
        elif method == "notifications/cancelled":
            # The server should not answer a cancelled request, so it must
            # not be answered on its behalf after a restart either.
            rid = (msg.get("params") or {}).get("requestId")
            self._in_flight.pop(_key(rid), None)

    def _client_reader(self) -> None:
        while True:
            try:
                line = self._in.readline()
            except (OSError, ValueError):
                line = b""
            if not line:
                break
            if not line.strip():
                continue
            with self._lock:
                for msg in _messages(_parse(line)):
                    self._track(msg)
                proc = self._proc if self._ready else None
                if proc is None:
                    self._queue.append(line)
                    continue
            if not self._to_child(proc, line):
                # The child died between the check and the write. Hold the
                # line for whichever child comes next; if none does, the
                # exit path answers it.
                with self._lock:
                    self._queue.append(line)
        self.eof.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            self._close_child_stdin(proc)

    def _close_child_stdin(self, proc) -> None:
        with self._child_in_lock:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    # -- child -> client --------------------------------------------------

    def _child_reader(self, proc, init_id) -> None:
        for line in iter(proc.stdout.readline, b""):
            msg = _parse(line)
            if (isinstance(msg, dict) and init_id is not None
                    and msg.get("id") == init_id and "method" not in msg):
                # Our replayed initialize. The client already has an answer
                # to its own, from the first child, and a second one for an
                # id it never sent would be a protocol error.
                if "error" in msg:
                    _log(f"replayed initialize failed: {msg['error']}")
                self._init_done.set()
                continue
            with self._lock:
                for m in _messages(msg):
                    if "method" not in m and "id" in m:
                        self._in_flight.pop(_key(m["id"]), None)
            self._to_client(line)
        try:
            proc.stdout.close()
        except OSError:
            pass

    # -- lifecycle --------------------------------------------------------

    def _spawn(self):
        kwargs = {}
        if sys.platform == "win32":
            # Claude Desktop starts us without a console; a child that made
            # one would flash a window up over the game.
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = subprocess.Popen(self._cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=None,
                                env=self._env, **kwargs)
        _adopt(proc)
        return proc

    def _answer_in_flight(self, restarted: bool) -> None:
        """Answer every request the dead child left unanswered.

        Left alone, each would hang in the client until its own timeout --
        minutes, in Claude Desktop -- for a reply that cannot come.
        """
        with self._lock:
            # Not the ones still queued: those never reached the dead child,
            # and the next one will answer them.
            queued = {_key(m["id"]) for line in self._queue
                      for m in _messages(_parse(line))
                      if "method" in m and "id" in m}
            pending = [v for k, v in self._in_flight.items()
                       if k not in queued]
            for k in list(self._in_flight):
                if k not in queued:
                    del self._in_flight[k]
        for rid, tool in pending:
            if restarted and tool == "restart_server":
                # The call did what it was asked; only its reply was lost.
                # Reporting it as an error with "retry" in it is an
                # invitation to restart again, forever.
                text = json.dumps({"restarting": True,
                                   "note": "the server restarted"})
                self._to_client(_encode({
                    "jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": text}],
                               "isError": False}}))
                continue
            self._to_client(_encode({
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": -32603,
                          "message": _RESTARTED if restarted else
                          "server exited before answering"}}))

    def _start_child(self) -> tuple:
        """Start a child and bring it to where the client thinks it is.

        Returns (proc, reader thread). Raises RuntimeError if the replayed
        handshake does not complete, which the caller counts as a crash.
        """
        with self._lock:
            self._ready = False
            init = self._init_request
            initialized = self._initialized
            init_id = None
            if init is not None:
                self._init_seq += 1
                init_id = f"{_INIT_ID_PREFIX}{self._init_seq}"
            self._init_done.clear()
        proc = self._spawn()
        reader = threading.Thread(target=self._child_reader,
                                  args=(proc, init_id), daemon=True)
        reader.start()
        with self._lock:
            self._proc = proc
        if init is not None:
            replay = dict(init)
            replay["id"] = init_id
            self._to_child(proc, _encode(replay))
            deadline = time.monotonic() + INIT_TIMEOUT
            while not self._init_done.wait(0.05):
                if proc.poll() is not None or self.eof.is_set():
                    break
                if time.monotonic() > deadline:
                    break
            if not self._init_done.is_set() and not self.eof.is_set():
                raise RuntimeError("replacement server did not initialize")
            if initialized is not None:
                self._to_child(proc, _encode(initialized))
        # Flush what arrived while there was nobody to send it to, in the
        # order it arrived, before letting the reader write directly again.
        while True:
            with self._lock:
                batch, self._queue = self._queue, []
                if not batch:
                    self._ready = True
                    break
            for line in batch:
                self._to_child(proc, line)
        if self.eof.is_set():
            self._close_child_stdin(proc)
        return proc, reader

    def _finish(self, proc) -> None:
        """The client has gone: let the child stop, then make sure of it."""
        self._close_child_stdin(proc)
        try:
            proc.wait(timeout=EOF_GRACE)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def run(self) -> int:
        threading.Thread(target=self._client_reader, daemon=True).start()
        crashes: list[float] = []
        first = True
        while True:
            try:
                proc, reader = self._start_child()
            except OSError as e:
                _log(f"could not start the server {self._cmd}: {e}")
                self._answer_in_flight(restarted=False)
                return 1
            except RuntimeError as e:
                _log(str(e))
                proc = self._proc
                code = _kill(proc)
                reader = None
            else:
                if not first and self._init_request is not None:
                    # So the client lists tools again, and sees any the
                    # new code added. Before initialize there is nothing
                    # to refresh, and nothing may be sent yet.
                    self._to_client(_encode({
                        "jsonrpc": "2.0",
                        "method": "notifications/tools/list_changed"}))
                first = False
                while True:
                    try:
                        code = proc.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        if self.eof.is_set():
                            self._finish(proc)
                            code = proc.wait()
                            break
            if reader is not None:
                # Everything the child wrote before exiting reaches the
                # client before anything is said on its behalf.
                reader.join(timeout=5.0)
            if proc is not None:
                self._close_child_stdin(proc)
            with self._lock:
                self._ready = False

            if self.eof.is_set():
                return 0

            if code == RESTART_EXIT_CODE:
                _log("restarting the server, as asked")
                self._answer_in_flight(restarted=True)
                continue

            now = time.monotonic()
            crashes = [t for t in crashes if now - t < CRASH_WINDOW]
            crashes.append(now)
            self._answer_in_flight(restarted=False)
            if len(crashes) > MAX_CRASHES:
                _log(f"server exited with {code}, {len(crashes)} times in "
                     f"{CRASH_WINDOW:.0f}s; giving up")
                return code if code else 1
            delay = BACKOFF[min(len(crashes), len(BACKOFF)) - 1]
            _log(f"server exited with {code}; starting another in {delay}s")
            if self.eof.wait(delay):
                return 0


def _key(rid):
    # 1 and "1" are different JSON-RPC ids; keep them apart.
    return (type(rid).__name__, rid)


def _kill(proc) -> int:
    if proc is None:
        return 1
    if proc.poll() is None:
        proc.kill()
    return proc.wait()


def run() -> int:
    return Supervisor(sys.stdin.buffer, sys.stdout.buffer).run()


def main() -> None:
    """Console-script entry point, so `assetto-mcp` is supervised too."""
    if os.environ.get(CHILD_ENV) or os.environ.get(NO_SUPERVISOR_ENV):
        from . import server
        server.main()
        return
    sys.exit(run())


if __name__ == "__main__":
    sys.exit(run())
