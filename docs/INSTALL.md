# Installing

The one-click route is `install-windows.bat` — see the [README](../README.md).
This page covers doing it by hand, and connecting clients other than Claude
Desktop.

## What you're actually configuring

`assetto-mcp` is a plain **stdio MCP server**. Whatever client you use, you are
telling it one thing: *run this command, and talk MCP over its stdin/stdout.*

```
command:  C:\path\to\python.exe
args:     -m assetto_mcp.server
```

That's the whole contract. Nothing in the server knows or cares which client
launched it.

---

## 1. Install the package

```powershell
python -m pip install -e .
```

If you used `install-windows.bat -SkipClientConfig`, this is already done, and
it printed the exact `python.exe` path to use below.

### Use the absolute path to `python.exe`

MCP clients launch servers as child processes **without your shell's `PATH`**,
so a bare `"python"` frequently fails silently — the client shows no tools and
no error. Get the right path with:

```powershell
py -c "import sys; print(sys.executable)"
```

Don't use `(Get-Command python).Source` — on stock Windows that often returns
`...\WindowsApps\python.exe`, the Microsoft Store alias stub, which is the
wrong answer. In JSON, remember to double the backslashes (`\\`).

## 2. Register it with your client

### Claude Desktop, Cursor, Windsurf, LM Studio, and most others

These all take the same `mcpServers` JSON object; only the file it lives in
differs.

```json
{
  "mcpServers": {
    "assetto-mcp": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": ["-m", "assetto_mcp.server"]
    }
  }
}
```

| Client | Config file |
|---|---|
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` — **but see [below](#where-claude-desktops-config-file-actually-lives)**, and prefer Settings → Developer → Edit Config |
| Cursor | `%USERPROFILE%\.cursor\mcp.json` |
| LM Studio | `%USERPROFILE%\.lmstudio\mcp.json`, or Program tab → Install → Edit mcp.json |
| Windsurf | `%USERPROFILE%\.codeium\windsurf\mcp_config.json` |

Client config formats do drift, so if yours isn't listed or the path has moved,
check its own MCP docs — you're looking for wherever it takes a `command` and
`args` for a local server.

### Claude Code

```
claude mcp add assetto-mcp -- python -m assetto_mcp.server
```

### VS Code with GitHub Copilot

VS Code uses its own `servers` key rather than `mcpServers`. Run
**MCP: Add Server** from the command palette and choose a local stdio server,
or edit `.vscode/mcp.json`:

```json
{
  "servers": {
    "assetto-mcp": {
      "type": "stdio",
      "command": "C:\\path\\to\\python.exe",
      "args": ["-m", "assetto_mcp.server"]
    }
  }
}
```

### A client that only speaks HTTP/SSE

Some clients can't launch a local process and only connect to a URL. Put a
stdio-to-HTTP bridge in front — `mcp-proxy` is the common one — and point the
client at the proxy. Nothing changes on this side.

### Then restart the client

Fully, not just the window. Claude Desktop in particular keeps running in the
system tray: right-click the tray icon and choose Quit.

## 3. Copy the in-game app

Copy `lua_app/assetto_mcp/` to `assettocorsa/apps/lua/assetto_mcp/`. It needs
Custom Shaders Patch, which you already have if you use Content Manager with
CSP enabled. Enable it from the in-game apps sidebar — move the mouse to the
right edge of the screen while in a session.

Not sure where Assetto Corsa is installed? In Steam, right-click Assetto Corsa
→ **Manage → Browse local files**.

---

## Only run one copy

The server holds two things that can only have one owner: the SQLite database,
and the HTTP bridge on `127.0.0.1:9666` that the in-game app talks to.

Instances coordinate for the database — several can run, and exactly one holds
the recording claim while the rest sit in standby. But if you register
`assetto-mcp` with **two different clients** and have both open, you get two
sets of processes, and only one can hold the bridge port. The in-game app will
then talk to whichever won.

This is fine as long as you use one client at a time. If you routinely run two,
give one of them `ASSETTO_MCP_NO_AUTOSTART=1` so it never tries to record.

---

## Where Claude Desktop's config file actually lives

<details>
<summary>Why <code>%APPDATA%</code> may not work for you</summary>

`%APPDATA%` is an *environment variable*, not a literal path. It expands to
`C:\Users\<you>\AppData\Roaming` — and `AppData` is a **hidden** folder, so
browsing to it in Explorer shows nothing unless you enable View → Hidden items.

The bigger gotcha: `%VAR%` is **cmd.exe syntax**. Windows Terminal defaults to
**PowerShell**, where the same variable is spelled `$env:VAR`. So:

| Where | What to type |
|---|---|
| PowerShell / Windows Terminal | `notepad $env:APPDATA\Claude\claude_desktop_config.json` |
| cmd.exe | `notepad %APPDATA%\Claude\claude_desktop_config.json` |
| Explorer address bar | `%APPDATA%\Claude` *(Explorer expands it too)* |
| Win+R (Run dialog) | `%APPDATA%\Claude` |

To open the folder rather than the file, use `explorer $env:APPDATA\Claude`
in PowerShell.
</details>

<details>
<summary><strong>On a packaged Claude Desktop install, that path is a lie</strong></summary>

Claude Desktop for Windows is commonly an **MSIX package** — including the
build you download straight from Anthropic's site, not just the Microsoft Store
one. MSIX *can* give a package a private, redirected view of `%APPDATA%`
(whether it does depends on how the package was built, so treat this as "check
both" rather than a rule). When redirection is in play the app writes and
reads:

```
%LOCALAPPDATA%\Packages\Claude_<hash>\LocalCache\Roaming\Claude\claude_desktop_config.json
```

Once a file exists in that redirect layer it **shadows** the real
`%APPDATA%\Claude` copy. So you can edit
`%APPDATA%\Claude\claude_desktop_config.json` all day, get valid JSON and a
correct Python path, and the app will still show no tools — it never opens that
file. The `logs` folder moves with it, too, so a missing
`%APPDATA%\Claude\logs` is the tell.

To find yours:

```powershell
Get-ChildItem "$env:LOCALAPPDATA\Packages\Claude*" -Directory |
  ForEach-Object { Join-Path $_.FullName 'LocalCache\Roaming\Claude' }
```

`install-windows.bat` finds every config location, picks the one the running
Claude Desktop actually reads, writes the entry **there only**, and removes the
entry from the others. All of that logic lives in `install-claude-desktop.ps1`,
separate from the rest of the installer.

The safest manual route is **Claude Desktop → Settings → Developer → Edit
Config**, which always opens the file the running app actually reads.
</details>

---

## Environment overrides

| Variable | Default | What it does |
|---|---|---|
| `AC_DOCS_DIR` | `~/Documents/Assetto Corsa` | Where AC keeps setups and logs |
| `ASSETTO_MCP_DATA` | `~/.assetto-mcp` | Database + car range files |
| `ASSETTO_MCP_BRIDGE_PORT` | `9666` | In-game app bridge port |
| `ASSETTO_MCP_NO_AUTOSTART` | unset | `1` stops *this* instance recording on startup. Rarely wanted: instances already coordinate so only one records. |

Set them in the `env` block of your client's server entry, if it has one, or in
your Windows environment.

If you change the bridge port, edit `BASE` in
`lua_app/assetto_mcp/assetto_mcp.lua` to match. The bridge binds localhost only.

---

## Upgrading from `ac-race-engineer`

This project was renamed from `ac-race-engineer`, and the old names are still
honoured so nothing is lost:

- **Your laps come with you.** `~/.ac-race-engineer` is renamed to
  `~/.assetto-mcp` — by the installer if you run it, otherwise by the server
  the next time it starts. Nothing is copied or rebuilt; it's the same
  directory under a new name. If something still has the database open the
  move is skipped, the old directory keeps being used, and it's retried on the
  next clean start — so the worst case is that nothing happens yet.
- **`AC_ENGINEER_*` still works.** `ASSETTO_MCP_DATA`, `_BRIDGE_PORT` and
  `_NO_AUTOSTART` are each read under the old spelling as a fallback.
- **The stale bits get cleaned up.** Re-running `install-windows.bat` removes
  the old `ac-race-engineer` entry from every Claude Desktop config and deletes
  the old `apps\lua\race_engineer` folder, so you don't end up with a dead
  server entry and the in-game app listed twice. On any other client, remove
  the old entry yourself.

One thing does not carry over: the in-game app's **wheel button bindings**. The
CSP control names changed with the rename, so re-bind the four complaint
buttons in the app's Settings window.
