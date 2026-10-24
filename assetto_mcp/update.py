"""Check GitHub for a newer version, and fast-forward to it when told to.

Two channels, the driver's choice, kept in the data directory: "stable"
(the default) offers the latest GitHub release, and "latest" every change
merged to `main`. Either way the update is a commit on `main`.

The install is a git clone that pip points at in place (`pip install -e .`
from windows/install-windows.ps1), so an update is a fast-forward of that clone,
plus the two things a pull alone does not do: reinstalling when
pyproject.toml changed, and copying the in-game app into Assetto Corsa when
lua_app/ did. The running server then needs restart_server to load it.

Deliberately narrow. It only ever moves `main` forward to a commit GitHub
says is on `main`, and refuses everything else -- a local commit, a dirty
tree, another branch -- rather than resolving it. Those are a developer's
checkout, and a developer does not want a tool merging into it.

Standard library only, and it never raises to the caller: a failed check
is a reply that says why, because the tool calling it is talking to a
driver, not a terminal.

    python -m assetto_mcp.update --check
    python -m assetto_mcp.update --apply        asks before applying
    python -m assetto_mcp.update --sync-lua     copy the in-game app only
    python -m assetto_mcp.update --channel latest   follow every change
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import circuit, config

DEFAULT_REPO = "mishan/assetto-mcp"
DEFAULT_API = "https://api.github.com"
BRANCH = "main"
CHANNELS = ("stable", "latest")
CHANNEL_FILE = "update_channel"
MAX_NOTES = 3000

# GitHub allows sixty unauthenticated requests an hour per address, and a
# driver asking "anything new?" twice in a session should not spend two.
CACHE_SECONDS = 3600
CACHE_FILE = "update_check.json"
HTTP_TIMEOUT = 5.0
FETCH_TIMEOUT = 120
PIP_TIMEOUT = 900
MAX_COMMITS = 25

# Server instances share the checkout, so one apply at a time across all of
# them. Every step of an apply has its own timeout, and a lock older than
# all of them together belongs to an apply that died without removing it.
LOCK_FILE = "update_apply.lock"
LOCK_STALE_SECONDS = FETCH_TIMEOUT + PIP_TIMEOUT + 600
# How long apply_update waits for the result before answering "in
# progress": inside the minute an MCP client commonly allows a call.
APPLY_WAIT = 25.0

LUA_APP = Path("lua_app") / "assetto_mcp"
INSTALLERS = ("install-windows.bat", "windows/install-windows.ps1",
              "windows/install-claude-desktop.ps1")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _repo() -> str:
    return config.env("UPDATE_REPO") or DEFAULT_REPO


def _api() -> str:
    return (config.env("UPDATE_API") or DEFAULT_API).rstrip("/")


def _git_url() -> str:
    return config.env("UPDATE_GIT_URL") or f"https://github.com/{_repo()}.git"


# --- git ----------------------------------------------------------------


def find_git() -> str | None:
    """git on PATH, or where Git for Windows puts it when PATH lacks it.

    Claude Desktop starts the server with a thin environment, and Git for
    Windows installed "from Git Bash only" is not on PATH at all.
    """
    found = shutil.which("git")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA") and
                 os.path.join(os.environ["LOCALAPPDATA"], "Programs")):
        if base:
            candidate = Path(base) / "Git" / "cmd" / "git.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def _quiet() -> dict:
    """subprocess arguments for anything run from inside the server.

    stdin is the MCP client's pipe, and a child that inherited it could
    swallow a message. On Windows, no console window over the game.
    """
    kw = {"stdin": subprocess.DEVNULL, "capture_output": True, "text": True}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW",
                                      0x08000000)
    return kw


def _git(root: Path, *args, timeout: float = 30, git: str | None = None):
    exe = git or find_git()
    if not exe:
        raise FileNotFoundError("git was not found")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    return subprocess.run([exe, "-C", str(root), *args], env=env,
                          timeout=timeout, **_quiet())


def _git_dir(root: Path) -> Path | None:
    """The .git directory, following a worktree's `gitdir:` pointer."""
    dot = root / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        text = dot.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            path = Path(text[len("gitdir:"):].strip())
            return path if path.is_absolute() else (root / path).resolve()
    return None


def _read_ref(git_dir: Path, ref: str) -> str | None:
    common = git_dir
    marker = git_dir / "commondir"
    if marker.is_file():
        common = (git_dir / marker.read_text().strip()).resolve()
    for base in (git_dir, common):
        loose = base / ref
        if loose.is_file():
            return loose.read_text().strip() or None
    packed = common / "packed-refs"
    if packed.is_file():
        for line in packed.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return None


def is_checkout(root: Path | None = None) -> bool:
    return _git_dir(root or repo_root()) is not None


def installed_commit(root: Path | None = None,
                     use_git: bool = True) -> str | None:
    """The commit checked out on disk, or None if this is not a checkout.

    git when it can be found, and the files under .git when it cannot, so a
    machine with no git on PATH can still say which version it is running.
    """
    root = root or repo_root()
    git_dir = _git_dir(root)
    if git_dir is None:
        return None
    if use_git and find_git():
        try:
            r = _git(root, "rev-parse", "HEAD")
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        head = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        return _read_ref(git_dir, head[4:].strip())
    return head or None


def current_branch(root: Path | None = None) -> str | None:
    """The checked-out branch, or None when HEAD is detached."""
    root = root or repo_root()
    git_dir = _git_dir(root)
    if git_dir is None:
        return None
    try:
        head = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return None
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):]
    return None


# --- GitHub -------------------------------------------------------------


class _RateLimited(Exception):
    def __init__(self, reset: int | None):
        super().__init__("GitHub API rate limit exhausted")
        self.reset = reset


def _get(url: str, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(url, headers={
        "User-Agent": "assetto-mcp-updater", "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        # Closed here: the error carries the response body, and left open
        # it is a socket held until the garbage collector finds it.
        e.close()
        remaining = e.headers.get("X-RateLimit-Remaining") if e.headers else None
        if e.code in (403, 429) and remaining == "0":
            reset = e.headers.get("X-RateLimit-Reset")
            raise _RateLimited(int(reset) if reset and reset.isdigit()
                               else None) from None
        raise


def _first_line(message: str | None) -> str:
    text = (message or "").strip()
    return text.splitlines()[0] if text else ""


def _flags(files: list[str]) -> dict:
    return {
        "needs_pip": "pyproject.toml" in files,
        "needs_lua_copy": any(f.startswith("lua_app/") for f in files),
        "installer_changed": any(f in INSTALLERS for f in files),
    }


def _latest_release() -> dict | None:
    """The newest GitHub release, or None when none is published."""
    try:
        data = json.loads(_get(
            f"{_api()}/repos/{_repo()}/releases/latest"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return {"version": data["tag_name"], "url": data.get("html_url"),
            "notes": (data.get("body") or "")[:MAX_NOTES]}


def _compare(local: str, target: str = BRANCH) -> dict:
    """GitHub's view of the install against `target`, from the install's
    side.

    GitHub's compare is phrased from the head's side: `local...main` is
    "ahead" when main has commits the install lacks. Everything returned
    here is turned around to say what the install is.
    """
    url = f"{_api()}/repos/{_repo()}/compare/{local}...{target}"
    data = json.loads(_get(url))
    flipped = {"ahead": "behind", "behind": "ahead"}
    status = flipped.get(data.get("status"), data.get("status"))
    commits = data.get("commits") or []
    total = data.get("total_commits", len(commits))
    if status == "identical":
        latest = local
    elif total <= len(commits):
        # With no commits on main's side, main is the merge base: the
        # install is purely ahead of it.
        latest = (commits[-1]["sha"] if commits else
                  (data.get("merge_base_commit") or {}).get("sha", ""))
    else:
        # More commits than one page of compare carries: ask for the tip.
        latest = _get(f"{_api()}/repos/{_repo()}/commits/{target}",
                      accept="application/vnd.github.sha"
                      ).decode().strip()
    files = [f.get("filename", "") for f in data.get("files") or []]
    out = {
        "status": status,
        "installed": local,
        "latest": latest,
        "behind_by": data.get("ahead_by", 0),
        "ahead_by": data.get("behind_by", 0),
        "commits": [{"sha": c["sha"][:7],
                     "message": _first_line(c.get("commit", {})
                                            .get("message", ""))}
                    for c in reversed(commits[-MAX_COMMITS:])],
        **_flags(files),
    }
    if total > MAX_COMMITS:
        out["more_commits"] = total - MAX_COMMITS
    if len(files) >= 300:
        # GitHub stops listing files at 300; apply works the flags out
        # again from git, which has no such limit.
        out["files_truncated"] = True
    return out


def _cache_path(data_dir: Path | None,
                name: str = CACHE_FILE) -> Path | None:
    if data_dir is None:
        try:
            data_dir = config.data_dir()
        except OSError:
            return None
    return Path(data_dir) / name


def _read_cache(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def channel(data_dir: Path | None = None) -> str:
    """The channel the driver chose, "stable" until they choose."""
    path = _cache_path(data_dir, CHANNEL_FILE)
    try:
        name = path.read_text(encoding="utf-8").strip() if path else ""
    except OSError:
        name = ""
    return name if name in CHANNELS else CHANNELS[0]


def set_channel(name: str, data_dir: Path | None = None) -> None:
    if name not in CHANNELS:
        raise ValueError(f"channel is one of {', '.join(CHANNELS)}, "
                         f"not {name!r}")
    path = _cache_path(data_dir, CHANNEL_FILE)
    if path is None:
        raise OSError("no data directory to keep the choice in")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name, encoding="utf-8")


def _write_cache(path: Path | None, local: str, result: dict) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked_at": time.time(), "local": local,
                                    "repo": _repo(),
                                    "channel": result.get("channel"),
                                    "result": result}),
                        encoding="utf-8")
    except OSError:
        pass


def cached_status(local: str | None,
                  data_dir: Path | None = None) -> str | None:
    """The last check's `status` for commit `local`, without asking GitHub.

    None when that commit was never checked.
    """
    cached = _read_cache(_cache_path(data_dir))
    if (not local or not cached or cached.get("repo") != _repo()
            or cached.get("local") != local):
        return None
    return (cached.get("result") or {}).get("status")


def _when(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def check(root: Path | None = None, data_dir: Path | None = None,
          running_commit: str | None = None, force: bool = False) -> dict:
    """Where the install stands against its channel on GitHub. Never
    raises."""
    try:
        return _check(root or repo_root(), data_dir, running_commit, force)
    except Exception as e:  # noqa: BLE001 - this answers a driver, not a log
        return {"status": "unknown", "error": "check_failed",
                "detail": f"{type(e).__name__}: {e}"}


def _check(root: Path, data_dir, running_commit, force) -> dict:
    if not is_checkout(root):
        return {
            "status": "unknown", "error": "not_a_git_checkout",
            "detail": f"{root} is not a git clone, so the installed version "
                      "cannot be told apart from any other",
            "advice": "reinstall from a git clone: `git clone "
                      f"https://github.com/{_repo()}` and run "
                      "install-windows.bat in it. Your laps are kept; they "
                      "live in the data directory, not here."}
    local = installed_commit(root)
    if not local:
        return {"status": "unknown", "error": "no_commit",
                "detail": "could not read the checked-out commit"}

    out = _check_remote(local, data_dir, force)
    out["branch"] = current_branch(root)
    if running_commit and running_commit != local:
        # Updated on disk (by apply_update, a git pull, anything) and not
        # yet loaded. Reported whatever GitHub answered, since it is true
        # either way.
        out["running"] = running_commit[:7]
        out["restart_needed"] = True
    return out


def _check_remote(local: str, data_dir, force) -> dict:
    ch = channel(data_dir)
    cache_path = _cache_path(data_dir)
    cached = _read_cache(cache_path)
    usable = (cached and cached.get("local") == local
              and cached.get("repo") == _repo()
              and cached.get("channel") == ch)
    if usable and not force and \
            time.time() - cached.get("checked_at", 0) < CACHE_SECONDS:
        return dict(cached["result"], cached=True,
                    checked_at=_when(cached["checked_at"]))
    try:
        release = _latest_release() if ch == "stable" else None
        if ch == "stable" and release is None:
            return {"status": "unknown", "channel": ch, "installed": local,
                    "error": "no_release",
                    "detail": "no release has been published yet; the "
                              "\"latest\" channel offers every change"}
        result = _compare(local, release["version"] if release else BRANCH)
    except _RateLimited as e:
        out = {"error": "rate_limited",
               "detail": "GitHub allows 60 checks an hour from one address "
                         "without a login, and they are used up",
               "rate_limit_resets_at": _when(e.reset)}
        if usable:
            out.update(dict(cached["result"], cached=True, stale=True,
                            checked_at=_when(cached["checked_at"])))
        else:
            out["status"] = "unknown"
        return out
    except urllib.error.HTTPError as e:
        if e.code in (404, 422):
            return {"status": "unknown", "installed": local,
                    "error": "local_commit_not_on_github",
                    "detail": f"GitHub has no commit {local[:7]} in "
                              f"{_repo()}: this checkout has commits of "
                              "its own, or follows a fork. Nothing will be "
                              "applied over it."}
        return {"status": "unknown", "error": "github_error",
                "http_status": e.code, "detail": str(e)}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        out = {"error": "offline",
               "detail": f"could not reach {_api()}: "
                         f"{getattr(e, 'reason', e)}"}
        if usable:
            out.update(dict(cached["result"], cached=True, stale=True,
                            checked_at=_when(cached["checked_at"])))
        else:
            out["status"] = "unknown"
        return out
    result["channel"] = ch
    if release:
        result["release"] = release
        if result["status"] == "ahead":
            result["note"] = (f"newer than the latest release, "
                              f"{release['version']}; the next one will be "
                              "offered")
    _write_cache(cache_path, local, result)
    return dict(result, cached=False, checked_at=_when(time.time()))


# --- Assetto Corsa and the in-game app ----------------------------------


def game_running() -> bool:
    """Whether acs.exe is running. Windows only; elsewhere it cannot be."""
    if sys.platform != "win32":
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq acs.exe", "/NH"],
                           timeout=10, **_quiet())
    except (OSError, subprocess.SubprocessError):
        return False
    return "acs.exe" in (r.stdout or "").lower()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(base: Path) -> dict[str, Path]:
    return {p.relative_to(base).as_posix(): p
            for p in base.rglob("*") if p.is_file()}


def sync_lua(root: Path | None = None, ac: Path | None = None,
             is_game_running=game_running) -> dict:
    """Copy the in-game app into Assetto Corsa where it differs.

    Skipped, with the reason, when the app was never installed there --
    a driver who chose -SkipLuaApp does not get it by surprise -- and while
    the game is running, which has the files open and would be reading a
    half-copied app on its next reload.
    """
    root = root or repo_root()
    src = root / LUA_APP
    if not src.is_dir():
        return {"copied": False, "note": f"{LUA_APP} is missing from the "
                                         "checkout"}
    ac = ac if ac is not None else circuit.ac_root()
    if ac is None:
        return {"copied": False,
                "note": "Assetto Corsa's folder was not found; set "
                        "ASSETTO_MCP_AC_ROOT, or rerun install-windows.bat "
                        "with -AcPath"}
    dest = Path(ac) / "apps" / "lua" / "assetto_mcp"
    if not dest.is_dir():
        return {"copied": False, "destination": str(dest),
                "note": "the in-game app is not installed there, so it was "
                        "left that way"}
    have, want = _tree(dest), _tree(src)
    changed = sorted(n for n, p in want.items()
                     if n not in have or _digest(have[n]) != _digest(p))
    stale = sorted(n for n in have if n not in want)
    if not changed and not stale:
        return {"copied": False, "destination": str(dest),
                "note": "already up to date"}
    if is_game_running():
        return {"copied": False, "destination": str(dest),
                "would_change": changed + stale,
                "note": "Assetto Corsa is running. Quit it, then run "
                        "`python -m assetto_mcp.update --sync-lua` or "
                        "install-windows.bat to copy the in-game app"}
    for name in changed:
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(want[name], target)
    for name in stale:
        try:
            (dest / name).unlink()
        except OSError:
            pass
    return {"copied": True, "destination": str(dest), "files": changed,
            "removed": stale}


# --- applying -----------------------------------------------------------


def _pip_install(root: Path) -> tuple[bool, str]:
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-e",
                        str(root)], timeout=PIP_TIMEOUT, **_quiet())
    return r.returncode == 0, (r.stdout + r.stderr)[-2000:]


def _refuse(reason: str, detail: str, **extra) -> dict:
    return {"updated": False, "refused": reason, "detail": detail, **extra}


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        # Not os.kill(pid, 0): on Windows that terminates the process.
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)  # query limited info
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return not ok or code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _take_lock(path: Path | None, to_commit: str) -> dict | None:
    """Take the apply lock: None when taken, else the holder's record.

    A lock whose process has gone, or that has outlived every timeout, is
    taken over, so a crashed apply cannot block updates for good.
    """
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    held: dict = {}
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                # The file's own age stands in while the holder has created
                # it and not yet written its record.
                held = {"started_at": path.stat().st_mtime,
                        **(_read_cache(path) or {})}
            except OSError:
                continue
            pid = held.get("pid")
            if (time.time() - held["started_at"] < LOCK_STALE_SECONDS
                    and (pid is None or _pid_alive(pid))):
                return held
            try:
                path.unlink()
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "started_at": time.time(),
                       "to_commit": to_commit}, f)
        return None
    return held


def apply_running(data_dir: Path | None = None) -> dict | None:
    """The lock record of an apply running in any instance, or None."""
    path = _cache_path(data_dir, LOCK_FILE)
    try:
        held = {"started_at": path.stat().st_mtime,
                **(_read_cache(path) or {})}
    except (AttributeError, OSError):
        return None
    pid = held.get("pid")
    if (time.time() - held["started_at"] >= LOCK_STALE_SECONDS
            or (pid is not None and not _pid_alive(pid))):
        return None
    return held


def apply(to_commit: str, root: Path | None = None,
          data_dir: Path | None = None, ac: Path | None = None,
          pip=_pip_install, is_game_running=game_running) -> dict:
    """Fast-forward the checkout to `to_commit`, which must be main's tip.

    Every precondition is checked again here rather than trusted from an
    earlier check_for_updates, because time passes between the two: the
    driver reads the list, someone pushes, and the commit they agreed to
    is no longer the one that would be applied.
    """
    root = root or repo_root()
    to_commit = to_commit.strip()
    lock = _cache_path(data_dir, LOCK_FILE)
    try:
        held = _take_lock(lock, to_commit)
    except OSError:
        held, lock = None, None
    if held is not None:
        return _refuse(
            "in_progress", "another apply is running" + (
                f" (to {held['to_commit'][:7]}, started "
                f"{_when(held['started_at'])})" if held.get("to_commit")
                else ""),
            do_this="wait a few minutes, then call check_for_updates")
    try:
        return _apply(to_commit, root, data_dir, ac, pip, is_game_running)
    except subprocess.TimeoutExpired as e:
        return _refuse("timeout", f"{' '.join(map(str, e.cmd[:4]))} took "
                                  f"longer than {e.timeout:.0f}s")
    except Exception as e:  # noqa: BLE001
        return _refuse("failed", f"{type(e).__name__}: {e}")
    finally:
        if lock is not None:
            try:
                lock.unlink()
            except OSError:
                pass


# How long a finished apply's result waits for the poll that collects it.
UNCOLLECTED_RESULT_SECONDS = 600

_job_lock = threading.Lock()
_job: dict | None = None


def _same_commit(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    return a.startswith(b) or b.startswith(a)


def apply_in_background(to_commit: str, live_session: int | None = None,
                        wait: float = APPLY_WAIT, **kw) -> dict:
    """apply() on a thread, answering within `wait` seconds.

    A reinstall can take many minutes, far past what a client waits for a
    reply, and a client that gave up would retry into a second apply. One
    still running answers `in_progress`; calling again with the same commit
    waits again, and collects the result once it is done. `live_session`,
    the session the driver is on track in, refuses starting one.
    """
    global _job
    to_commit = to_commit.strip()
    if not to_commit:
        return _refuse("no_commit", "to_commit is empty",
                       do_this="pass `latest` from check_for_updates")
    with _job_lock:
        job = _job
        if (job and not job["thread"].is_alive()
                and time.time() - job.get("finished_at", 0)
                > UNCOLLECTED_RESULT_SECONDS):
            # Nobody came back for it; a call this much later is a new
            # attempt, not the poll that result was waiting for.
            job = _job = None
        if job and not _same_commit(job["to_commit"], to_commit):
            if job["thread"].is_alive():
                return _refuse(
                    "in_progress",
                    f"an apply to {job['to_commit'][:7]} is running",
                    do_this="wait a few minutes, then call "
                            "check_for_updates")
            job = None
        if job is None:
            if live_session is not None:
                return _refuse(
                    "recording",
                    f"session {live_session} is live: the driver is on "
                    "track, and an update is never applied mid-session",
                    session_id=live_session,
                    do_this="wait until the car is back in the menus or "
                            "the session has ended, then call again")
            job = _job = _start_job(to_commit, kw)
    job["thread"].join(wait)
    if job["thread"].is_alive():
        return {"in_progress": True, "to_commit": to_commit,
                "stage": job["stage"],
                "running_for_s": round(time.time() - job["started_at"]),
                "do_this": "call apply_update again with the same "
                           "to_commit; it waits for the result again"}
    with _job_lock:
        if _job is job:
            _job = None
    return job["result"]


def job_started(to_commit: str) -> bool:
    """Whether a call now would only wait for or collect this apply."""
    with _job_lock:
        return bool(_job and _same_commit(_job["to_commit"],
                                          to_commit.strip()))


def _start_job(to_commit: str, kw: dict) -> dict:
    job = {"to_commit": to_commit, "started_at": time.time(),
           "stage": "fetching and fast-forwarding", "result": None}
    pip = kw.pop("pip", _pip_install)

    def staged_pip(root):
        job["stage"] = "reinstalling the package; pip can take minutes"
        return pip(root)

    def run():
        job["result"] = apply(to_commit, pip=staged_pip, **kw)
        job["finished_at"] = time.time()

    job["thread"] = threading.Thread(target=run, name="apply-update",
                                     daemon=True)
    job["thread"].start()
    return job


def _apply(to_commit, root, data_dir, ac, pip, is_game_running) -> dict:
    if not is_checkout(root):
        return _refuse("not_a_git_checkout",
                       "this install is not a git clone and cannot be "
                       "updated in place; reinstall from a git clone")
    git = find_git()
    if not git:
        return _refuse("git_not_found",
                       "git is needed to update; install Git for Windows, "
                       "or update by hand with a fresh clone")
    branch = current_branch(root)
    if branch != BRANCH:
        return _refuse("not_on_main",
                       f"the checkout is on {branch or 'a detached HEAD'}, "
                       f"not {BRANCH}; it is someone's working copy and is "
                       "left alone")
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no",
                 git=git)
    if dirty.returncode != 0 or dirty.stdout.strip():
        return _refuse("local_changes",
                       "tracked files have been edited in the checkout; "
                       "commit or discard them first",
                       files=dirty.stdout.splitlines()[:20])

    now = check(root=root, data_dir=data_dir, force=True)
    if now.get("status") != "behind":
        return _refuse(
            "not_behind",
            f"the install is {now.get('status')} against its "
            f"{now.get('channel')} channel on GitHub"
            + (f" ({now['error']})" if now.get("error") else "")
            + "; only a plain fast-forward is applied",
            check=now)
    latest = now["latest"]
    if len(to_commit) < 7 or not latest.startswith(to_commit.lower()):
        return _refuse("commit_mismatch",
                       f"the update is to {latest[:7]} now, not {to_commit}. "
                       "Show the driver the new list from check_for_updates "
                       "and ask again.", latest=latest)

    before = installed_commit(root)
    fetch = _git(root, "fetch", "--quiet", "--no-tags", _git_url(), BRANCH,
                 timeout=FETCH_TIMEOUT, git=git)
    if fetch.returncode != 0:
        return _refuse("fetch_failed", fetch.stderr.strip()[-500:])
    have = _git(root, "cat-file", "-e", f"{latest}^{{commit}}", git=git)
    if have.returncode != 0:
        return _refuse("fetch_failed", f"{latest[:7]} did not arrive with "
                                       f"the fetch")
    merge = _git(root, "merge", "--ff-only", "--quiet", latest, git=git)
    if merge.returncode != 0:
        return _refuse("merge_failed", merge.stderr.strip()[-500:])

    files = _git(root, "diff", "--name-only", before, latest,
                 git=git).stdout.split()
    log = _git(root, "log", "--format=%h %s", f"{before}..{latest}",
               git=git).stdout.splitlines()
    flags = _flags(files)
    out = {
        "updated": True,
        "updated_from": before[:7],
        "updated_to": latest[:7],
        "commits": [{"sha": line[:line.find(" ")],
                     "message": line[line.find(" ") + 1:]}
                    for line in log[:MAX_COMMITS]],
        **flags,
    }
    if len(log) > MAX_COMMITS:
        out["more_commits"] = len(log) - MAX_COMMITS

    if flags["needs_pip"]:
        ok, tail = pip(root)
        out["pip"] = {"ran": True, "ok": ok}
        if not ok:
            # Most often Windows refusing to replace a file this very
            # process has loaded. The code itself is already updated --
            # the install is editable -- so this is about dependencies.
            out["pip"]["output"] = tail
            out["warning"] = (
                "the package reinstall failed, probably because Windows "
                "holds files the running server has open. Quit the client "
                "fully and run install-windows.bat, then reopen it.")
    else:
        out["pip"] = {"ran": False, "note": "pyproject.toml did not change"}

    out["lua"] = sync_lua(root, ac, is_game_running)
    if flags["installer_changed"]:
        out["installer_note"] = (
            "the installer changed too. Nothing needs it now, but rerun "
            "install-windows.bat at some quiet moment to pick up whatever "
            "it does differently.")
    try:
        path = _cache_path(data_dir)
        if path:
            path.unlink()
    except OSError:
        pass
    out["restart_required"] = True
    out["next"] = "call restart_server, with the driver's OK"
    return out


# --- command line -------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="assetto-mcp-update",
        description="Check for a newer assetto-mcp and fast-forward to it.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true",
                   help="say whether an update is available (default)")
    g.add_argument("--apply", nargs="?", const="", metavar="COMMIT",
                   help="apply the update; asks first unless COMMIT is "
                        "given or --yes is set")
    g.add_argument("--sync-lua", action="store_true",
                   help="copy the in-game app into Assetto Corsa")
    g.add_argument("--channel", choices=CHANNELS,
                   help="follow releases (stable) or every change (latest), "
                        "then check")
    ap.add_argument("--yes", action="store_true",
                    help="with --apply, do not ask")
    args = ap.parse_args(argv)

    def show(obj):
        print(json.dumps(obj, indent=2))

    if args.sync_lua:
        out = sync_lua()
        show(out)
        return 0
    if args.channel:
        set_channel(args.channel)
    if args.apply is None:
        out = check(force=True)
        show(out)
        return 0 if not out.get("error") else 1

    target = args.apply
    if not target:
        now = check(force=True)
        if now.get("status") != "behind":
            show(now)
            return 0 if now.get("status") == "identical" else 1
        if now.get("release"):
            print(f"Release {now['release']['version']}:\n"
                  f"{now['release']['notes']}\n")
        print(f"{now['behind_by']} new commit(s):")
        for c in now["commits"]:
            print(f"  {c['sha']} {c['message']}")
        target = now["latest"]
        if not args.yes:
            try:
                answer = input(f"Update to {target[:7]}? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("Left as it was.")
                return 1
    out = apply(target)
    show(out)
    if out.get("updated"):
        print("\nRestart the server to load it: ask the assistant to call "
              "restart_server, or fully quit and reopen the client.")
    return 0 if out.get("updated") else 1


if __name__ == "__main__":
    sys.exit(main())
