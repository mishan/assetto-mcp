"""The installer's Claude Desktop step, run for real against temp configs.

install-claude-desktop.ps1 is dot-sourced with its console helpers stubbed
and the config discovery pointed at a temporary directory, so what it
writes can be read back. Skipped where there is no PowerShell.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import run_module  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "windows" / "install-claude-desktop.ps1"

_HARNESS = r"""
$ErrorActionPreference = 'Stop'
function Write-Step($m) {}; function Write-Info($m) {}
function Write-Ok($m) { "OK: $m" }; function Write-Warn2($m) { "WARN: $m" }
function Write-JsonFile($p, $t) { [IO.File]::WriteAllText($p, $t) }
$ServerName = 'telemetrist'
$LegacyServerNames = @('assetto-mcp', 'ac-race-engineer')
. '__SCRIPT__'
$owner = '__OWNER__'; $other = '__OTHER__'
function Get-ClaudeConfigCandidates {
    ,@([pscustomobject]@{Path=$owner; Dir=(Split-Path $owner); IsPackage=$true; PackageFamily='t'},
       [pscustomobject]@{Path=$other; Dir=(Split-Path $other); IsPackage=$false})
}
function Select-OwnerConfig($c) { [pscustomobject]@{Owner=$c[0]; Reason='test'} }
function Backup-ConfigFile($p) { "$p.bak" }
Register-WithClaudeDesktop -PythonPath 'C:\py.exe'
"""


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def _install(owner_cfg: dict | None, other_cfg: dict | None) -> tuple | None:
    """Run the step; return (owner config, other config, output), or None
    where there is no PowerShell to run it."""
    shell = _powershell()
    if not shell:
        from lua_harness import skip, strict
        if strict():
            raise RuntimeError("PowerShell is not installed, and a skip here "
                               "would read as a pass")
        skip("PowerShell is not installed")
        return None
    with tempfile.TemporaryDirectory() as d:
        owner, other = Path(d) / "msix.json", Path(d) / "plain.json"
        for path, cfg in ((owner, owner_cfg), (other, other_cfg)):
            if cfg is not None:
                path.write_text(json.dumps(cfg, ensure_ascii=False),
                                encoding="utf-8")
        script = (_HARNESS.replace("__SCRIPT__", str(SCRIPT))
                  .replace("__OWNER__", str(owner))
                  .replace("__OTHER__", str(other)))
        proc = subprocess.run([shell, "-NoProfile", "-NonInteractive",
                               "-ExecutionPolicy", "Bypass",
                               "-Command", script],
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr

        def read(p):
            return (json.loads(p.read_text(encoding="utf-8-sig"))
                    if p.exists() else None)
        return read(owner), read(other), proc.stdout


def test_settings_on_the_old_entry_are_kept():
    old = {"mcpServers": {"elgato": {"command": "x"}, "assetto-mcp": {
        "command": "py", "args": ["-m", "assetto_mcp.server"],
        "env": {"ASSETTO_MCP_DASH_LAN": "1"}}}}
    if not (got := _install(old, None)):
        return
    owner, _, out = got
    entry = owner["mcpServers"]["telemetrist"]
    assert entry["env"] == {"ASSETTO_MCP_DASH_LAN": "1"}, owner
    assert "assetto-mcp" not in owner["mcpServers"], owner
    assert "elgato" in owner["mcpServers"], owner
    print("  the old entry's settings ride over to the new one")


def test_settings_in_the_other_config_are_kept_too():
    """The entry the driver set up can be in the config the installer does
    not choose; that one's entry is removed, and its settings must not go
    with it."""
    other = {"mcpServers": {"assetto-mcp": {
        "command": "py", "args": ["-m", "assetto_mcp.server"],
        "env": {"ASSETTO_MCP_DASH_LAN": "1"}}}}
    if not (got := _install(None, other)):
        return
    owner, plain, _ = got
    assert owner["mcpServers"]["telemetrist"].get("env") == {
        "ASSETTO_MCP_DASH_LAN": "1"}, owner
    assert "assetto-mcp" not in plain["mcpServers"], plain
    print("  settings found in the other config land on the new entry")


def test_other_fields_the_driver_added_are_kept():
    """Only command and args are the installer's to write."""
    old = {"mcpServers": {"assetto-mcp": {
        "command": "py", "args": ["-m", "assetto_mcp.server"],
        "cwd": "D:\\racing", "disabled": False}}}
    if not (got := _install(old, None)):
        return
    owner, _, out = got
    entry = owner["mcpServers"]["telemetrist"]
    assert entry["command"] == "C:\\py.exe", entry
    assert entry["args"] == ["-m", "telemetrist.server"], entry
    assert entry.get("cwd") == "D:\\racing" and entry.get("disabled") is False, \
        entry
    print("  fields beyond command and args are carried over")


def test_settings_outside_ascii_come_through_whole():
    """Claude Desktop writes UTF-8 with no BOM, which Windows PowerShell
    5.1 reads as the ANSI code page unless told otherwise."""
    env = {"TELEMETRIST_DATA": "D:\\Rennen\\Köln"}
    old = {"mcpServers": {"assetto-mcp": {
        "command": "py", "args": ["-m", "assetto_mcp.server"], "env": env}}}
    for owner_cfg, other_cfg in ((old, None), (None, old)):
        if not (got := _install(owner_cfg, other_cfg)):
            return
        assert got[0]["mcpServers"]["telemetrist"]["env"] == env, got[0]
    print("  non-ASCII settings survive, from either config")


if __name__ == "__main__":
    sys.exit(1 if run_module(globals()) else 0)
