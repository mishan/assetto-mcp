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

from assetto_mcp import update as U  # noqa: E402

REPO = "owner/assetto-mcp"


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
    """The compare endpoint, answered by git from the bare repository."""

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
        self.write("lua_app/assetto_mcp/assetto_mcp.lua", "-- v1\n")
        self.write("lua_app/assetto_mcp/manifest.ini", "[ABOUT]\n")
        self.write("assetto_mcp/server.py", "# v1\n")
        self.push("First")
        _git(tmp, "clone", "-q", str(self.bare), str(self.install))
        self.github = FakeGitHub(self.bare)
        self._env = {k: os.environ.get(k) for k in (
            "ASSETTO_MCP_UPDATE_API", "ASSETTO_MCP_UPDATE_REPO",
            "ASSETTO_MCP_UPDATE_GIT_URL")}
        os.environ["ASSETTO_MCP_UPDATE_API"] = self.github.url
        os.environ["ASSETTO_MCP_UPDATE_REPO"] = REPO
        os.environ["ASSETTO_MCP_UPDATE_GIT_URL"] = self.bare.as_uri()
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
        w.write("lua_app/assetto_mcp/assetto_mcp.lua", "-- v2\n")
        w.write("install-windows.ps1", "# new\n")
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
        w.write("assetto_mcp/server.py", "# v2\n")
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
        w.write("assetto_mcp/server.py", "# v2\n")
        tip = w.push("Change")
        w.write("assetto_mcp/server.py", "# my edit\n", where=w.install)
        before = w.head()
        refused = w.apply(tip)
        assert refused["refused"] == "local_changes", refused
        assert w.head() == before
        print("  a dirty tree is refused")
    _world(go)


def test_another_branch_is_refused():
    def go(w):
        w.write("assetto_mcp/server.py", "# v2\n")
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
        w.write("assetto_mcp/server.py", "# v2\n")
        agreed = w.push("Change")
        w.write("assetto_mcp/server.py", "# v3\n")
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
        os.environ["ASSETTO_MCP_UPDATE_API"] = "http://127.0.0.1:9"
        out = w.check()
        assert out["error"] == "offline", out
        assert out["status"] == "unknown", out
        print("  offline: error=offline, nothing raised")
    _world(go)


def test_rate_limited_falls_back_to_the_cached_answer():
    def go(w):
        w.write("assetto_mcp/server.py", "# v2\n")
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
        w.write("lua_app/assetto_mcp/assetto_mcp.lua", "-- v2\n")
        tip = w.push("App change")
        ac = w.tmp / "ac"
        ac.mkdir()
        done = w.apply(tip, ac=ac)
        assert done["updated"] is True, done
        assert done["lua"]["copied"] is False, done
        assert "not installed" in done["lua"]["note"], done
        assert not (ac / "apps").exists()
        print("  no apps/lua/assetto_mcp: nothing created")
    _world(go)


def test_lua_is_not_copied_while_the_game_runs():
    def go(w):
        dest = w.tmp / "ac" / "apps" / "lua" / "assetto_mcp"
        dest.mkdir(parents=True)
        (dest / "assetto_mcp.lua").write_text("-- v1\n")
        w.write("lua_app/assetto_mcp/assetto_mcp.lua", "-- v2\n")
        tip = w.push("App change")
        done = w.apply(tip, ac=w.tmp / "ac", is_game_running=lambda: True)
        assert done["updated"] is True, done
        assert done["lua"]["copied"] is False, done
        assert "running" in done["lua"]["note"], done
        assert "--sync-lua" in done["lua"]["note"], done
        assert (dest / "assetto_mcp.lua").read_text() == "-- v1\n"
        print("  game running: app not touched, and how to finish later")
    _world(go)


def test_lua_files_that_differ_are_copied():
    def go(w):
        dest = w.tmp / "ac" / "apps" / "lua" / "assetto_mcp"
        dest.mkdir(parents=True)
        (dest / "assetto_mcp.lua").write_text("-- v1\n")
        (dest / "manifest.ini").write_bytes(
            (w.install / "lua_app/assetto_mcp/manifest.ini").read_bytes())
        (dest / "old_module.lua").write_text("-- gone upstream\n")
        w.write("lua_app/assetto_mcp/assetto_mcp.lua", "-- v2\n")
        tip = w.push("App change")
        done = w.apply(tip, ac=w.tmp / "ac")
        lua = done["lua"]
        assert lua["copied"] is True, lua
        assert lua["files"] == ["assetto_mcp.lua"], lua
        assert lua["removed"] == ["old_module.lua"], lua
        assert (dest / "assetto_mcp.lua").read_text() == "-- v2\n"
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
        w.write("assetto_mcp/server.py", "# v2\n")
        tip = w.push("Change")
        w.apply(tip)
        assert U.installed_commit(w.install, use_git=False) == tip
        _git(w.install, "pack-refs", "--all")
        assert not (w.install / ".git/refs/heads/main").exists()
        assert U.installed_commit(w.install, use_git=False) == tip
        print("  HEAD read from loose and packed refs alike")
    _world(go)


_SERVER = r"""
import json
import assetto_mcp.server as s
out = json.loads(s.check_for_updates())
s._shutdown()
print(json.dumps(out))
"""


def test_the_server_tool_answers_offline_without_raising():
    """The tool as the model calls it, inside a real server process."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        from lua_harness import skip
        skip("mcp is not installed")
        return
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="ac-upd-") as d:
        env = dict(os.environ, ASSETTO_MCP_DATA=d,
                   ASSETTO_MCP_NO_AUTOSTART="1", ASSETTO_MCP_BRIDGE_PORT="0",
                   ASSETTO_MCP_UPDATE_API="http://127.0.0.1:9")
        proc = subprocess.run([sys.executable, "-c", _SERVER], cwd=root,
                              env=env, capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        # A CI checkout may not be a clone at all; either answer is fine,
        # as long as it is an answer.
        assert out.get("error") in ("offline", "not_a_git_checkout"), out
        print(f"  check_for_updates: {out['error']}")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
