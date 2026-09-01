# Troubleshooting

**Start here: `diagnose.bat`** in the repo root. It checks nearly everything on
this page — every Claude Desktop config location, JSON validity, BOM, every
Python on the box, who owns bridge port 9666, and a cold-start of the server on
a scratch port — and writes `diagnose-report.txt`. The config checks are
Claude-Desktop-specific; everything else applies whatever client you use.

Other MCP servers' secrets are redacted from that report on a best-effort
basis (`env` values become `<redacted>`, token-shaped strings are masked), but
it still contains your username and absolute paths, and redaction is pattern
matching rather than a guarantee. **Skim it before sharing it.**

---

## Your client doesn't list the tools

Work down this list; it's roughly in order of how often each one is the answer.

**1. Did the client fully restart?** Not just the window. Claude Desktop keeps
running in the system tray — right-click the tray icon (bottom right, possibly
hidden under the `^` arrow) and choose Quit. Other clients vary; kill it
properly.

**2. Does the command in the config actually work?** Run exactly what's in the
config, by hand:

```powershell
& "<the command path from your config>" -m assetto_mcp.server
```

It should start and sit there waiting for input on stdin. `Ctrl-C` to stop. If
it prints `ModuleNotFoundError`, go to the next item.

**3. Two Pythons.** `pip install -e .` records the pointer to this repo in
*one* interpreter's `site-packages`. If `command` in the config names a
different Python, `-m assetto_mcp.server` dies with `ModuleNotFoundError` and
most clients show nothing rather than an error:

```powershell
& "<the command path from your config>" -c "import assetto_mcp; print('ok')"
```

**4. Is it a bare `"python"` in the config?** Clients launch servers without
your shell's `PATH`, so that often resolves to nothing or to the Microsoft
Store stub. Use an absolute path — see
[INSTALL.md](INSTALL.md#use-the-absolute-path-to-pythonexe).

**5. Did you edit the config the client actually reads?** For Claude Desktop
this is the usual culprit and has its own section below. For other clients,
most have a UI that shows loaded servers and any startup error — look there
before editing files.

**6. Read the client's MCP server log.** Claude Desktop:

```powershell
notepad $env:APPDATA\Claude\logs\mcp-server-assetto-mcp.log
```

## Claude Desktop: there is no `logs` folder there

That means Claude Desktop is the MSIX-packaged build (the normal case,
whatever you downloaded) and is reading a different config entirely — see
[the MSIX note in INSTALL.md](INSTALL.md#where-claude-desktops-config-file-actually-lives).
Re-run `install-windows.bat`. Both the config and the logs live under:

```powershell
Get-ChildItem "$env:LOCALAPPDATA\Packages\Claude*\LocalCache\Roaming\Claude" -Recurse -Filter 'mcp-server-*.log'
```

## Typing `python` opens the Microsoft Store (or says "not recognized")

Windows ships an app-execution-alias stub at
`%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` that hijacks the name when no
real Python is on `PATH`. Install Python from python.org with "Add python.exe to
PATH" ticked, or use the `py` launcher (`py -3 -m pip install -e .`) — the
python.org installer sets that up by default. You can also kill the stub in
Settings → Apps → Advanced app settings → App execution aliases.

The installer already ignores anything under `WindowsApps`, so this only bites
you on a manual install.

## `.ps1 cannot be loaded because running scripts is disabled`

That's the execution policy. Use `install-windows.bat` instead — it bypasses
the policy for that one script without changing any system setting. Flags go on
the `.bat`, not the `.ps1`, for the same reason:

```
install-windows.bat -AcPath "D:\Games\steamapps\common\assettocorsa"
```

## The in-game app shows nothing, or "server not running"

The app talks to the server's HTTP bridge on `127.0.0.1:9666`. Things that
break that:

- **No server running at all.** The client only launches it when the client is
  open, so start the client first.
- **Something else owns the port.** `diagnose.bat` reports who holds 9666.
- **You changed `ASSETTO_MCP_BRIDGE_PORT`** but didn't change `BASE` in
  `lua_app/assetto_mcp/assetto_mcp.lua` to match.
- **Two clients open at once**, each with its own copy of the server. Only one
  can hold the port. See [INSTALL.md](INSTALL.md#only-run-one-copy).

## Setup values don't stick in-game

You're writing outside the car's legal range and AC is silently ignoring them.
That should not happen any more — values are clamped to what the car accepts —
unless the setup was written with `allow_unclamped`, which is what you get if
you told it to write anyway after it refused. See
[SETUP-RANGES.md](SETUP-RANGES.md).

## "No setup ranges are known for this car"

It's refusing to write a setup it can't check. Start Assetto Corsa with the
in-game app enabled and open the setup screen once — the app reports every
adjustable entry automatically — or install a ranges file. Both routes are in
[SETUP-RANGES.md](SETUP-RANGES.md#when-there-are-no-ranges).

## "already exists for &lt;car&gt; at &lt;track&gt;"

It won't replace a setup file you might have made yourself. Take the suggested
name, or say explicitly that it should overwrite — the old file is kept
alongside as `<name>.ini.bak-<timestamp>` either way.

## A dead `ac-race-engineer` server shows up

Left over from before the rename — it points at a module that no longer exists.
Re-run `install-windows.bat`; it removes the old entry from every Claude
Desktop config location. On any other client, delete the entry yourself. Same
for a duplicate in-game app in the AC sidebar.

## The overlay says NOT STORING LAPS

The in-game overlay cross-checks the server's claims against the game's own lap
counter, so this means laps really are finishing without landing in the
database. Ask for `recording_status` — the `state` field
(`recording`, `waiting`, `standby`, `never_started`, `died`,
`stopped_by_request`) comes with a note saying what it means.
`stopped_by_request` means someone called `stop_recording`, which persists
across restarts and across every instance; `start_recording` turns it back on.

## A lap I drove clean was marked invalid

Known and unfixed: validity is inferred from wheels-off-track rather than read
from the game, which vanilla AC doesn't expose. Wide flat kerbs trigger it.
It's the top item in [BACKLOG.md](../BACKLOG.md), and worth reporting with the
track and lap number.

## Damper histograms are missing or flagged as low-rate

Expected in multiplayer. See [the suspension section of
INTERNALS.md](INTERNALS.md#suspension).
