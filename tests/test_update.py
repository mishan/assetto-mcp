"""Updating an install: GitHub's view of it, and the fast-forward.

No network. "GitHub" is a bare repository in a temp directory, with a
local HTTP server answering the one compare endpoint the updater asks --
computed by git from that same bare repository, so its answers are the
ones the real API would give for the same history. The install is a clone
of it, the way install-windows.ps1 leaves one. Skips without git, which
the gaming PC may not have; CI does.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402

from telemetrist import update as U  # noqa: E402

REPO = "owner/telemetrist"


def _git(cwd, *args) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main",
         *args],
        cwd=cwd, capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def _no_git() -> bool:
    if U.find_git() is None:
        from lua_harness import skip
        skip("git is not installed")
        return True
    return False


class FakeGitHub:
    """The compare and latest-release endpoints, answered by git from the
    bare repository; its newest tag is the latest release."""

    def __init__(self, bare: Path):
        self.bare = bare
        self.rate_limited = False
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests += 1
                if outer.rate_limited:
                    return self._send(403, {"message": "rate limit"}, {
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(int(time.time()) + 1800)})
                if self.path == f"/repos/{REPO}/releases/latest":
                    return outer._release(self)
                prefix = f"/repos/{REPO}/compare/"
                if not self.path.startswith(prefix):
                    return self._send(404, {"message": "Not Found"})
                base, _, head = self.path[len(prefix):].partition("...")
                return outer._compare(self, base, head)

            def _send(self, code, obj, headers=None):
                body = json.dumps(obj).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _compare(self, h, base, head):
        def git(*a):
            return subprocess.run(["git", *a], cwd=self.bare,
                                  capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL)
        if git("cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
            return h._send(404, {"message": "No common ancestor"})
        behind, ahead = git("rev-list", "--left-right", "--count",
                            f"{base}...{head}").stdout.split()
        behind, ahead = int(behind), int(ahead)
        status = ("identical" if not behind and not ahead else
                  "ahead" if not behind else
                  "behind" if not ahead else "diverged")
        shas = git("rev-list", "--reverse", f"{base}..{head}").stdout.split()
        commits = [{"sha": s, "commit": {"message": git(
            "log", "-1", "--format=%B", s).stdout}} for s in shas]
        mb = git("merge-base", base, head).stdout.strip()
        files = git("diff", "--name-only", mb, head).stdout.split()
        h._send(200, {"status": status, "ahead_by": ahead,
                      "merge_base_commit": {"sha": mb},
                      "behind_by": behind, "total_commits": len(commits),
                      "commits": commits,
                      "files": [{"filename": f} for f in files]})

    def _release(self, h):
        tags = subprocess.run(
            ["git", "tag", "--sort=-creatordate"], cwd=self.bare,
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL).stdout.split()
        if not tags:
            return h._send(404, {"message": "Not Found"})
        h._send(200, {"tag_name": tags[0], "html_url": f"/r/{tags[0]}",
                      "body": f"What's new in {tags[0]}"})

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class World:
    """A remote, a developer pushing to it, and an install cloned from it."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bare = tmp / "remote.git"
        self.dev = tmp / "dev"
        self.install = tmp / "install"
        self.data = tmp / "data"
        self.data.mkdir()
        _git(tmp, "init", "--bare", "-q", "-b", "main", str(self.bare))
        _git(tmp, "clone", "-q", str(self.bare), str(self.dev))
        self.write("pyproject.toml", "[project]\nname = 'x'\n")
        self.write("lua_app/telemetrist/telemetrist.lua", "-- v1\n")
        self.write("lua_app/telemetrist/manifest.ini", "[ABOUT]\n")
        self.write("telemetrist/server.py", "# v1\n")
        self.push("First")
        _git(tmp, "clone", "-q", str(self.bare), str(self.install))
        self.github = FakeGitHub(self.bare)
        self._env = {k: os.environ.get(k) for k in (
            "TELEMETRIST_UPDATE_API", "TELEMETRIST_UPDATE_REPO",
            "TELEMETRIST_UPDATE_GIT_URL")}
        os.environ["TELEMETRIST_UPDATE_API"] = self.github.url
        os.environ["TELEMETRIST_UPDATE_REPO"] = REPO
        os.environ["TELEMETRIST_UPDATE_GIT_URL"] = self.bare.as_uri()
        U.set_channel("latest", self.data)
        self.pip_calls: list[Path] = []

    def write(self, name, text, where=None):
        path = (where or self.dev) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def push(self, message) -> str:
        _git(self.dev, "add", "-A")
        _git(self.dev, "commit", "-q", "-m", message)
        _git(self.dev, "push", "-q", "origin", "main")
        return _git(self.dev, "rev-parse", "HEAD")

    def release(self, tag) -> None:
        _git(self.dev, "tag", tag)
        _git(self.dev, "push", "-q", "origin", tag)

    def head(self) -> str:
        return _git(self.install, "rev-parse", "HEAD")

    def check(self, **kw):
        return U.check(root=self.install, data_dir=self.data, **kw)

    def pip(self, root):
        self.pip_calls.append(root)
        return True, "ok"

    def apply(self, sha, **kw):
        kw.setdefault("pip", self.pip)
        kw.setdefault("ac", self.tmp / "no-ac")
        kw.setdefault("is_game_running", lambda: False)
        return U.apply(sha, root=self.install, data_dir=self.data, **kw)

    def close(self):
        self.github.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _world(fn):
    if _no_git():
        return
    with tempfile.TemporaryDirectory(prefix="ac-upd-") as d:
        w = World(Path(d))
        try:
            fn(w)
        finally:
            w.close()


def test_an_install_on_mains_tip_is_identical():
    def go(w):
        out = w.check()
        assert out["status"] == "identical", out
        assert out["behind_by"] == 0 and out["commits"] == [], out
        assert out["branch"] == "main", out
        assert "restart_needed" not in out, out
        print("  identical, nothing to apply")
    _world(go)


def test_behind_is_listed_flagged_and_fast_forwarded():
    def go(w):
        w.write("pyproject.toml", "[project]\nname = 'x'\nversion = '2'\n")
        w.push("Bump the package")
        w.write("lua_app/telemetrist/telemetrist.lua", "-- v2\n")
        w.write("windows/install-windows.ps1", "# new\n")
        tip = w.push("Change the app and the installer\n\nWith a body.")

        out = w.check()
        assert out["status"] == "behind", out
        assert out["behind_by"] == 2 and out["latest"] == tip, out
        assert [c["message"] for c in out["commits"]] == [
            "Change the app and the installer", "Bump the package"], out
        assert out["needs_pip"] and out["needs_lua_copy"], out
        assert out["installer_changed"], out

        before = w.head()
        done = w.apply(tip)
        assert done["updated"] is True, done
        assert w.head() == tip, "the checkout did not move"
        assert done["updated_from"] == before[:7], done
        assert done["updated_to"] == tip[:7], done
        assert len(done["commits"]) == 2, done
        assert done["restart_required"] is True, done
        assert "restart_server" in done["next"], done
        assert done["pip"] == {"ran": True, "ok": True}, done
        assert w.pip_calls == [w.install], w.pip_calls
        assert "installer_note" in done, done
        assert w.check()["status"] == "identical"
        print("  2 behind, flags right, fast-forwarded, pip run once")
    _world(go)


def test_stable_offers_releases_and_latest_offers_every_change():
    def go(w):
        (w.data / U.CHANNEL_FILE).unlink()
        assert U.channel(w.data) == "stable"
        assert w.check()["error"] == "no_release"

        w.write("telemetrist/server.py", "# v2\n")
        released = w.push("Released")
        w.release("v0.2.0")
        w.write("telemetrist/server.py", "# v3\n")
        tip = w.push("Not released yet")

        out = w.check(force=True)
        assert out["status"] == "behind" and out["latest"] == released, out
        assert out["release"]["version"] == "v0.2.0", out
        assert out["release"]["notes"] == "What's new in v0.2.0", out
        assert w.apply(released)["updated"] and w.head() == released
        assert w.check()["status"] == "identical"

        U.set_channel("latest", w.data)
        out = w.check()
        assert out["latest"] == tip and "release" not in out, out
        assert w.apply(tip)["updated"] and w.head() == tip

        # Back on stable past the release: nothing to offer, nor to undo.
        U.set_channel("stable", w.data)
        out = w.check()
        assert out["status"] == "ahead" and "v0.2.0" in out["note"], out
        assert w.apply(released)["refused"] == "not_behind"
        print("  stable: no release, then the release; latest: the tip")
    _world(go)


def test_a_failed_reinstall_is_a_warning_not_a_failure():
    """The code is already updated; only the dependencies are in doubt."""
    def go(w):
        w.write("pyproject.toml", "[project]\nname = 'y'\n")
        tip = w.push("Bump")
        done = w.apply(tip, pip=lambda root: (False, "Access is denied"))
        assert done["updated"] is True and w.head() == tip, done
        assert done["pip"]["ok"] is False, done
        assert "install-windows.bat" in done["warning"], done
        print("  pip failure reported with what to do")
    _world(go)


def test_the_running_commit_is_compared_with_the_disk():
    def go(w):
        old = w.head()
        w.write("telemetrist/server.py", "# v2\n")
        tip = w.push("Server change")
        w.apply(tip)
        out = w.check(running_commit=old)
        assert out["status"] == "identical", out
        assert out["restart_needed"] is True, out
        assert out["running"] == old[:7], out
        print("  updated on disk, old code running: restart_needed")
    _world(go)


def test_ahead_and_diverged_are_refused():
    """A checkout with commits of its own is a developer's. Hands off."""
    def go(w):
        w.write("local.txt", "mine\n", where=w.install)
        _git(w.install, "add", "-A")
        _git(w.install, "commit", "-q", "-m", "Local work")
        # The local commit was never pushed, so GitHub has never seen it.
        out = w.check()
        assert out["error"] == "local_commit_not_on_github", out
        # And once it has been -- pushed to a branch, say -- it is ahead.
        _git(w.install, "push", "-q", "origin", "HEAD:refs/heads/work")
        out = w.check(force=True)
        assert out["status"] == "ahead", out
        refused = w.apply(out["latest"])
        assert refused["refused"] == "not_behind", refused

        w.write("other.txt", "theirs\n")
        tip = w.push("Upstream work")
        out = w.check(force=True)
        assert out["status"] == "diverged", out
        before = w.head()
        refused = w.apply(tip)
        assert refused["refused"] == "not_behind", refused
        assert w.head() == before
        print("  ahead: refused; diverged: refused; checkout untouched")
    _world(go)


def test_edited_files_are_refused():
    def go(w):
        w.write("telemetrist/server.py", "# v2\n")
        tip = w.push("Change")
        w.write("telemetrist/server.py", "# my edit\n", where=w.install)
        before = w.head()
        refused = w.apply(tip)
        assert refused["refused"] == "local_changes", refused
        assert w.head() == before
        print("  a dirty tree is refused")
    _world(go)


def test_another_branch_is_refused():
    def go(w):
        w.write("telemetrist/server.py", "# v2\n")
        tip = w.push("Change")
        _git(w.install, "checkout", "-q", "-b", "experiment")
        refused = w.apply(tip)
        assert refused["refused"] == "not_on_main", refused
        assert "experiment" in refused["detail"], refused
        print("  not on main: refused")
    _world(go)


def test_a_commit_that_is_no_longer_the_tip_is_refused():
    """The driver agreed to a list; a different list is a new question."""
    def go(w):
        w.write("telemetrist/server.py", "# v2\n")
        agreed = w.push("Change")
        w.write("telemetrist/server.py", "# v3\n")
        newer = w.push("Another change, pushed after the driver said yes")
        before = w.head()
        refused = w.apply(agreed)
        assert refused["refused"] == "commit_mismatch", refused
        assert refused["latest"] == newer, refused
        assert w.head() == before
        print("  to_commit no longer main's tip: refused")
    _world(go)


def test_offline_is_an_answer_not_an_exception():
    def go(w):
        os.environ["TELEMETRIST_UPDATE_API"] = "http://127.0.0.1:9"
        out = w.check()
        assert out["error"] == "offline", out
        assert out["status"] == "unknown", out
        print("  offline: error=offline, nothing raised")
    _world(go)


def test_rate_limited_falls_back_to_the_cached_answer():
    def go(w):
        w.write("telemetrist/server.py", "# v2\n")
        tip = w.push("Change")
        first = w.check()
        assert first["status"] == "behind" and first["cached"] is False

        again = w.check()
        assert again["cached"] is True, again
        assert w.github.requests == 1, "a fresh cache must not be refetched"

        # An hour later, and GitHub says no.
        cache = w.data / U.CACHE_FILE
        state = json.loads(cache.read_text())
        state["checked_at"] -= U.CACHE_SECONDS + 1
        cache.write_text(json.dumps(state))
        w.github.rate_limited = True
        out = w.check()
        assert out["error"] == "rate_limited", out
        assert out["stale"] is True and out["status"] == "behind", out
        assert out["latest"] == tip, out
        assert out["rate_limit_resets_at"], out
        print("  403 with the limit spent: last answer, marked stale")
    _world(go)


def test_lua_is_left_alone_when_the_app_is_not_installed():
    def go(w):
        w.write("lua_app/telemetrist/telemetrist.lua", "-- v2\n")
        tip = w.push("App change")
        ac = w.tmp / "ac"
        ac.mkdir()
        done = w.apply(tip, ac=ac)
        assert done["updated"] is True, done
        assert done["lua"]["copied"] is False, done
        assert "not installed" in done["lua"]["note"], done
        assert not (ac / "apps").exists()
        print("  no apps/lua/telemetrist: nothing created")
    _world(go)


def test_lua_is_not_copied_while_the_game_runs():
    def go(w):
        dest = w.tmp / "ac" / "apps" / "lua" / "telemetrist"
        dest.mkdir(parents=True)
        (dest / "telemetrist.lua").write_text("-- v1\n")
        w.write("lua_app/telemetrist/telemetrist.lua", "-- v2\n")
        tip = w.push("App change")
        done = w.apply(tip, ac=w.tmp / "ac", is_game_running=lambda: True)
        assert done["updated"] is True, done
        assert done["lua"]["copied"] is False, done
        assert "running" in done["lua"]["note"], done
        assert "--sync-lua" in done["lua"]["note"], done
        assert (dest / "telemetrist.lua").read_text() == "-- v1\n"
        print("  game running: app not touched, and how to finish later")
    _world(go)


def test_lua_files_that_differ_are_copied():
    def go(w):
        dest = w.tmp / "ac" / "apps" / "lua" / "telemetrist"
        dest.mkdir(parents=True)
        (dest / "telemetrist.lua").write_text("-- v1\n")
        (dest / "manifest.ini").write_bytes(
            (w.install / "lua_app/telemetrist/manifest.ini").read_bytes())
        (dest / "old_module.lua").write_text("-- gone upstream\n")
        w.write("lua_app/telemetrist/telemetrist.lua", "-- v2\n")
        tip = w.push("App change")
        done = w.apply(tip, ac=w.tmp / "ac")
        lua = done["lua"]
        assert lua["copied"] is True, lua
        assert lua["files"] == ["telemetrist.lua"], lua
        assert lua["removed"] == ["old_module.lua"], lua
        assert (dest / "telemetrist.lua").read_text() == "-- v2\n"
        assert not (dest / "old_module.lua").exists()
        print("  only the changed file copied; a stale one removed")
    _world(go)


def test_an_install_that_is_not_a_git_checkout_says_so():
    with tempfile.TemporaryDirectory(prefix="ac-upd-") as d:
        out = U.check(root=Path(d), data_dir=Path(d))
        assert out["error"] == "not_a_git_checkout", out
        assert "git clone" in out["advice"], out
        refused = U.apply("abc1234", root=Path(d), data_dir=Path(d))
        assert refused["refused"] == "not_a_git_checkout", refused
        assert U.installed_commit(Path(d)) is None
        print("  not a checkout: version unknown, reinstall advised")


def test_the_commit_is_read_without_git_too():
    """A PC with no git on PATH can still say what it is running."""
    def go(w):
        w.write("telemetrist/server.py", "# v2\n")
        tip = w.push("Change")
        w.apply(tip)
        assert U.installed_commit(w.install, use_git=False) == tip
        _git(w.install, "pack-refs", "--all")
        assert not (w.install / ".git/refs/heads/main").exists()
        assert U.installed_commit(w.install, use_git=False) == tip
        print("  HEAD read from loose and packed refs alike")
    _world(go)


def test_a_long_apply_answers_in_progress_and_is_collected_later():
    def go(w):
        w.write("pyproject.toml", "[project]\nname = 'z'\n")
        tip = w.push("Bump")
        release = threading.Event()

        def slow_pip(root):
            release.wait(30)
            return True, "ok"

        kw = dict(root=w.install, data_dir=w.data, ac=w.tmp / "no-ac",
                  is_game_running=lambda: False)
        out = U.apply_in_background(tip, wait=0.2, pip=slow_pip, **kw)
        assert out["in_progress"] is True, out
        # The fetch comes first, and on a slow runner takes a while.
        deadline = time.time() + 60
        while "pip" not in out["stage"] and time.time() < deadline:
            out = U.apply_in_background(tip, wait=0.2, **kw)
        assert out["in_progress"] is True and "pip" in out["stage"], out
        other = U.apply_in_background("0" * 40, wait=0.2, **kw)
        assert other["refused"] == "in_progress", other
        elsewhere = w.apply(tip)
        assert elsewhere["refused"] == "in_progress", elsewhere
        assert "check_for_updates" in elsewhere["do_this"], elsewhere

        release.set()
        done = U.apply_in_background(tip[:7], wait=30, **kw)
        assert done["updated"] is True and w.head() == tip, done
        assert not (w.data / U.LOCK_FILE).exists()
        print("  in progress, a second apply refused, result collected")
    _world(go)


def test_an_app_under_its_old_name_is_moved_and_never_doubled():
    # (new folder there too, game running, move fails) -> old folder left
    cases = [((False, False, False), False), ((True, False, False), False),
             ((False, True, False), True), ((False, False, True), True)]
    real_rename = Path.rename
    for (has_new, running, fails), old_left in cases:
        with tempfile.TemporaryDirectory() as d:
            root, ac = Path(d) / "install", Path(d) / "ac"
            (root / U.LUA_APP).mkdir(parents=True)
            (root / U.LUA_APP / "telemetrist.lua").write_text("-- v2\n")
            old = ac / "apps" / "lua" / U.OLD_APP_NAME
            new = old.with_name(U.APP_NAME)
            old.mkdir(parents=True)
            (old / "assetto_mcp.lua").write_text("-- v1\n")
            if has_new:
                new.mkdir()
            if fails:
                def no_rename(self, target):
                    raise OSError("in use")
                Path.rename = no_rename
            try:
                out = U.sync_lua(root, ac, is_game_running=lambda: running)
            finally:
                Path.rename = real_rename
            case = (has_new, running, fails)
            assert old.is_dir() == old_left == U.old_app_installed(ac), (
                case, out)
            if old_left:
                assert (old / "assetto_mcp.lua").read_text() == "-- v1\n"
                assert new.is_dir() == has_new, (case, out)
            else:
                assert [p.name for p in new.iterdir()] == ["telemetrist.lua"]
    print("  moved when it can be, else left whole and alone")


def test_a_lock_left_by_a_dead_apply_is_taken_over():
    def go(w):
        w.write("a.txt", "a\n")
        tip = w.push("Change")
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        lock = w.data / U.LOCK_FILE
        cases = [
            ({"pid": gone.pid, "started_at": time.time()}, False),
            ({"pid": os.getpid(),
              "started_at": time.time() - U.LOCK_STALE_SECONDS - 1}, False),
            ({"pid": os.getpid(), "started_at": time.time()}, True),
        ]
        for record, held in cases:
            lock.write_text(json.dumps(record), encoding="utf-8")
            out = w.apply(tip)
            assert (out.get("refused") == "in_progress") == held, \
                (record, out)
        assert w.head() == tip
        print("  dead or expired locks taken over; a live one refused")
    _world(go)


def test_an_apply_is_refused_while_the_driver_is_on_track():
    def go(w):
        w.write("a.txt", "a\n")
        tip = w.push("Change")
        out = U.apply_in_background(tip, live_session=7, root=w.install,
                                    data_dir=w.data)
        assert out["refused"] == "recording", out
        assert out["session_id"] == 7 and "menus" in out["do_this"], out
        assert w.head() != tip
        print("  live session: refused, checkout untouched")
    _world(go)


_SERVER = r"""
import asyncio, json
import telemetrist.server as s
from telemetrist import db

def apply_update():
    try:
        asyncio.run(s.mcp.call_tool("apply_update", {"to_commit": "abc1234"}))
    except s.ToolError as e:
        return json.loads(str(e))
    raise AssertionError("a refused apply was not a failed call")

out = {"check": json.loads(s.check_for_updates()),
       "beta": json.loads(s.check_for_updates(channel="beta")),
       "chose": json.loads(s.check_for_updates(channel="latest"))}
c = s._collector
c.holds_recorder, c.session_id, c.live = True, 42, True
out["here"] = apply_update()
c.holds_recorder, c.session_id, c.live = False, None, False
out["elsewhere_id"] = db.create_session(
    s._conn, car="c", track="t", track_config="", tyre_compound="",
    air_temp=20, road_temp=25)
s._game_now = lambda: {"status": "live"}
out["elsewhere"] = apply_update()
# The other instance keeps its session open in the menus; only the game
# can say whether the driver is on track.
s._game_now = lambda: {"status": "off"}
out["menus"] = s._live_session()
s._shutdown()
print(json.dumps(out))
"""


def test_the_server_tools_as_the_model_calls_them():
    """Offline is an answer, the driver's channel is kept, and a live
    session here or in another instance refuses an apply."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        from lua_harness import skip
        skip("mcp is not installed")
        return
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="ac-upd-") as d:
        env = dict(os.environ, TELEMETRIST_DATA=d,
                   TELEMETRIST_NO_AUTOSTART="1", TELEMETRIST_BRIDGE_PORT="0",
                   TELEMETRIST_UPDATE_API="http://127.0.0.1:9")
        proc = subprocess.run([sys.executable, "-c", _SERVER], cwd=root,
                              env=env, capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        check = out["check"]
        # A CI checkout may not be a clone at all; either answer is fine,
        # as long as it is an answer.
        assert check.get("error") in ("offline", "not_a_git_checkout"), out
        assert "stable, latest" in out["beta"]["error"], out
        assert U.channel(Path(d)) == "latest", out["chose"]
        for where, sid in (("here", 42), ("elsewhere", out["elsewhere_id"])):
            assert out[where]["refused"] == "recording", out
            assert out[where]["session_id"] == sid, out
        assert out["menus"] is None, out
        print(f"  check_for_updates: {check['error']}; apply_update "
              "refused in a live session, not in another chat's menus")



def test_a_finished_apply_tells_every_server_where_to_go():
    """Only the server that applied an update hears about it; the others,
    one per chat in Claude Desktop, read the commit it left behind."""
    def go(w):
        w.write("a.txt", "a\n")
        tip = w.push("Change")
        assert U.applied_commit(w.data) is None
        assert w.apply(tip)["updated"] is True
        assert U.applied_commit(w.data) == tip
        print("  the applied commit is left for the other servers")
    _world(go)


_FOLLOW = r"""
import json
import telemetrist.server as s
from telemetrist import update

restarts = []
s._restart_soon = lambda: restarts.append(1)
s._RUNNING_COMMIT = "old"
update.installed_commit = lambda root=None: "new"
update.apply_running = lambda data_dir=None: None
s._game_now = lambda: {"status": "off"}
out = {}
out["nothing_applied"] = s._follow_update_once()
def marker(commit):
    return lambda data_dir=None: {"commit": commit, "pid": -1,
                                  "at": s._STARTED_AT + 1}
update.applied_marker = marker("branch")
out["a_branch_checked_out"] = s._follow_update_once()
update.applied_marker = marker("new")
s._calls_in_flight = 1
out["mid_call"] = s._follow_update_once()
s._calls_in_flight = 0
c = s._collector
c.holds_recorder, c.session_id, c.live = True, 9, True
out["on_track"] = s._follow_update_once()
c.holds_recorder, c.session_id, c.live = False, None, False
out["idle"] = s._follow_update_once()
out["restarts"] = len(restarts)
s._shutdown()
print(json.dumps(out))
"""


def test_another_chats_server_follows_an_update_when_idle():
    """Applied in one chat, every other server restarts onto it itself --
    but never for a branch someone checked out, mid-call, or on track."""
    out = _run_server(_FOLLOW)
    if out is None:
        return
    assert out == {"nothing_applied": False, "a_branch_checked_out": False,
                   "mid_call": False, "on_track": False, "idle": True,
                   "restarts": 1}, out
    print("  restarts once, idle, onto the applied commit only")


_FAIL_SAFE = r"""
import json
import telemetrist.server as s
from telemetrist import db

c = s._collector
c.holds_recorder, c.session_id, c.live = False, None, False
sid = db.create_session(s._conn, car="c", track="t", track_config="",
                        tyre_compound="", air_temp=20, road_temp=25)
out = {"sid": sid}
for status in ("not running", "unknown (9)", "off", "replay", "live"):
    s._game_now = lambda status=status: {"status": status}
    out[status] = s._live_session()
s._shutdown()
print(json.dumps(out))
"""


def test_a_game_that_cannot_be_read_counts_as_on_track():
    """Only a game that says it is off track lets an update through.

    Shared memory unreadable -- the game mid-load, a server whose files
    changed under it -- with another chat recording must refuse, as it did
    before another instance's session needed the game's word.
    """
    out = _run_server(_FAIL_SAFE)
    if out is None:
        return
    sid = out["sid"]
    assert out["not running"] == sid and out["unknown (9)"] == sid, out
    assert out["live"] == sid, out
    assert out["off"] is None and out["replay"] is None, out
    print("  unreadable or unknown: on track; off and replay: not")


_SELF = r"""
import json, os, time
import telemetrist.server as s
from telemetrist import update

restarts = []
s._restart_soon = lambda: restarts.append(1)
s._RUNNING_COMMIT = "old"
update.installed_commit = lambda root=None: "new"
update.apply_running = lambda data_dir=None: None
s._game_now = lambda: {"status": "off"}
out = {}
# This process applied it: the driver is asked before it restarts.
update.mark_applied("new", s.DATA_DIR)
out["applied_here"] = s._follow_update_once()
# Another process applied it after this one started: follow.
update.mark_applied("new", s.DATA_DIR, pid=os.getpid() + 1)
out["applied_elsewhere"] = s._follow_update_once()
# Applied before this process started -- someone checked the applied
# commit out again after working on a branch: not an update.
s._STARTED_AT = time.time() + 60
out["applied_before_start"] = s._follow_update_once()
out["restarts"] = len(restarts)
s._shutdown()
print(json.dumps(out))
"""


def test_only_other_servers_follow_and_only_a_newer_apply():
    """The applying server waits for the driver's OK; a branch switched
    back to the applied commit is not an update."""
    out = _run_server(_SELF)
    if out is None:
        return
    assert out == {"applied_here": False, "applied_elsewhere": True,
                   "applied_before_start": False, "restarts": 1}, out
    print("  follows another process's apply, made after it started, only")


def test_an_interrupted_marker_write_keeps_the_last_marker():
    """A write that dies half way must not leave an empty marker that no
    server can follow."""
    import pathlib
    with tempfile.TemporaryDirectory() as d:
        U.mark_applied("aaa", Path(d))
        real = pathlib.Path.write_text

        def dies_half_way(self, text, *a, **kw):
            real(self, text[:3], *a, **kw)
            raise OSError("disk full")

        pathlib.Path.write_text = dies_half_way
        try:
            U.mark_applied("bbb", Path(d))
        finally:
            pathlib.Path.write_text = real
        assert U.applied_commit(Path(d)) == "aaa"
        print("  a failed write leaves the previous marker readable")


def _run_server(script: str) -> dict | None:
    try:
        import mcp  # noqa: F401
    except ImportError:
        from lua_harness import skip
        skip("mcp is not installed")
        return None
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="ac-upd-") as d:
        env = dict(os.environ, TELEMETRIST_DATA=d,
                   TELEMETRIST_NO_AUTOSTART="1", TELEMETRIST_BRIDGE_PORT="0",
                   TELEMETRIST_UPDATE_API="http://127.0.0.1:9")
        proc = subprocess.run([sys.executable, "-c", script], cwd=root,
                              env=env, capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])


def test_an_empty_commit_is_refused_rather_than_matched():
    out = U.apply_in_background("  ")
    assert out["refused"] == "no_commit", out

if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
