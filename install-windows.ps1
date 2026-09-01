<#
.SYNOPSIS
    One-shot installer for assetto-mcp on the Windows gaming PC.

.DESCRIPTION
    Does everything the README used to ask you to do by hand:

      1. Finds a real Python 3.10+ (ignoring the Microsoft Store stub)
      2. pip install -e . from this folder
      3. Registers the server with Claude Desktop (see
         install-claude-desktop.ps1), or prints what to paste into another
         MCP client with -SkipClientConfig
      4. Finds your Assetto Corsa install and copies the CSP Lua app in
      5. Creates the data + ranges directories
      6. Prints exactly what to do next

    Steps 1, 2, 4 and 5 are the same whatever client you use -- the server is
    a plain stdio MCP server. Only step 3 knows about Claude Desktop, and it
    lives in its own file so another client can be added beside it.

    Everything is idempotent: run it again after a git pull and it will
    update in place rather than duplicating anything.

    Targets Windows PowerShell 5.1 (the one built into Windows) as well as
    PowerShell 7 - no PS7-only syntax is used.

.PARAMETER AcPath
    Path to your assettocorsa folder, if auto-detection fails.
    e.g. -AcPath "D:\Games\steamapps\common\assettocorsa"

.PARAMETER SkipLuaApp
    Don't install the in-game CSP Lua app.

.PARAMETER SkipClientConfig
    Don't touch any MCP client's config. Installs everything else and prints
    the command and arguments to register by hand. Use this if your client is
    not Claude Desktop.

.PARAMETER Uninstall
    Remove the Claude Desktop config entry and the installed Lua app.
    Leaves your telemetry database alone.

.EXAMPLE
    Right-click install-windows.bat -> Open   (or just double-click it)

.EXAMPLE
    install-windows.bat -AcPath "D:\Games\steamapps\common\assettocorsa"
#>

[CmdletBinding()]
param(
    [string] $AcPath,
    [switch] $SkipLuaApp,
    [switch] $SkipClientConfig,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'
$script:RepoRoot = if ($PSScriptRoot) { $PSScriptRoot }
                   else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
$script:Problems = @()

# install-windows.bat pauses on its own; don't prompt twice.
$script:PauseAtEnd = -not $env:AC_INSTALLER_LAUNCHED_FROM_BAT `
                     -and $Host.Name -eq 'ConsoleHost' `
                     -and -not $env:AC_INSTALLER_NONINTERACTIVE

# --- pretty output ------------------------------------------------------

function Write-Step  ($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok    ($m) { Write-Host "    [ok] $m" -ForegroundColor Green }
function Write-Info  ($m) { Write-Host "    $m" -ForegroundColor Gray }
function Write-Warn2 ($m) { Write-Host "    [!]  $m" -ForegroundColor Yellow
                            $script:Problems += $m }
function Write-Err2  ($m) { Write-Host "    [X]  $m" -ForegroundColor Red }

function Stop-WithError ($message, $hint) {
    Write-Host ""
    Write-Err2 $message
    if ($hint) { Write-Host ""; Write-Host $hint -ForegroundColor Yellow }
    Write-Host ""
    if ($script:PauseAtEnd) { Read-Host "Press Enter to close" }
    exit 1
}

# Native commands that write to stderr raise a terminating NativeCommandError
# under $ErrorActionPreference='Stop' in PowerShell 5.1 - even on success.
# pip does this routinely ("WARNING: ... not on PATH"), so run externals with
# the preference relaxed and judge them by their exit code instead.
function Invoke-Native {
    param([scriptblock] $Block)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    # $LASTEXITCODE is global and sticky: if the command below never actually
    # starts (CommandNotFoundException, missing .exe) PowerShell leaves it at
    # whatever the *previous* command set, so a stale 0 reads as success. Seed
    # it with cmd.exe's "command not found" code so a failed launch fails.
    $global:LASTEXITCODE = 9009
    try {
        $out = & $Block 2>&1
        return [pscustomobject]@{ Output = $out; ExitCode = $LASTEXITCODE }
    } catch {
        # A genuinely terminating launch failure (bad path, access denied).
        return [pscustomobject]@{ Output = @([string]$_.Exception.Message); ExitCode = 9009 }
    } finally {
        $ErrorActionPreference = $prev
    }
}

# Claude Desktop's config parser rejects a UTF-8 BOM, which
# Set-Content -Encoding UTF8 adds in PowerShell 5.1. Always write via .NET.
function Write-JsonFile ($path, $text) {
    [System.IO.File]::WriteAllText(
        $path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

Write-Host ""
Write-Host "  assetto-mcp installer" -ForegroundColor White
Write-Host "  $script:RepoRoot" -ForegroundColor DarkGray

# --- paths --------------------------------------------------------------

$ServerName            = 'assetto-mcp'
$script:OwnerConfigDir = Join-Path $env:APPDATA 'Claude'   # refined in step 3

# This project was called ac-race-engineer until it was renamed, and the
# installer is the only thing that knows where the old install put itself.
# Anything left under these names is dead weight that still gets launched, so
# every run sweeps them up.
$LegacyServerName      = 'ac-race-engineer'
$LegacyLuaAppName      = 'race_engineer'
$LegacyDataDir         = Join-Path $env:USERPROFILE '.ac-race-engineer'

# --- MCP client support --------------------------------------------------
#
# The server is a plain stdio MCP server; nothing below this line knows or
# cares which client launches it. Everything Claude-Desktop-specific lives in
# its own file, so supporting another client means adding a file beside it.
# Dot-sourced, so it runs in this scope and can use the helpers above.
$clientScript = Join-Path $script:RepoRoot 'install-claude-desktop.ps1'
if (-not (Test-Path -LiteralPath $clientScript)) {
    Stop-WithError "install-claude-desktop.ps1 is missing from this folder." @"
  It sits next to install-windows.bat and holds the MCP client setup.
  If you copied only some files out of the repo, copy the whole folder.
  Otherwise, re-clone or re-download and run the installer again.
"@
}
. $clientScript


# Delete the in-game app directory this project installed under an older
# name. CSP loads every folder under apps\lua, so leaving it there means two
# copies of the app in the sidebar, both talking to the same bridge port.
function Remove-LegacyLuaApp ($ac) {
    if (-not $ac) { return }
    $old = Join-Path $ac "apps\lua\$LegacyLuaAppName"
    if (-not (Test-Path -LiteralPath $old)) { return }
    try {
        Remove-Item -LiteralPath $old -Recurse -Force
        Write-Ok "Removed the pre-rename in-game app from $old"
    } catch {
        Write-Warn2 "Could not remove the old in-game app at $old - is Assetto Corsa running?"
        Write-Info  "Delete that folder by hand, or you will see the app twice in the sidebar."
    }
}

# --- locating Assetto Corsa (needed by both install and uninstall) -------

function Find-AssettoCorsa {
    $candidates = @()

    # Ask Steam where its libraries live.
    $steamPath = $null
    foreach ($key in @('HKCU:\Software\Valve\Steam',
                       'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam')) {
        try {
            $p = Get-ItemProperty -Path $key -ErrorAction Stop
            if     ($p.SteamPath)   { $steamPath = $p.SteamPath }
            elseif ($p.InstallPath) { $steamPath = $p.InstallPath }
            if ($steamPath) { break }
        } catch { }
    }

    if ($steamPath) {
        $steamPath = $steamPath -replace '/', '\'
        $candidates += Join-Path $steamPath 'steamapps\common\assettocorsa'

        $vdf = Join-Path $steamPath 'steamapps\libraryfolders.vdf'
        if (Test-Path -LiteralPath $vdf) {
            foreach ($line in (Get-Content -LiteralPath $vdf)) {
                if ($line -match '"path"\s*"(.+?)"') {
                    $lib = $matches[1] -replace '\\\\', '\'
                    $candidates += Join-Path $lib 'steamapps\common\assettocorsa'
                }
            }
        }
    }

    $candidates += 'C:\Program Files (x86)\Steam\steamapps\common\assettocorsa'
    $candidates += 'C:\Program Files\Steam\steamapps\common\assettocorsa'

    foreach ($c in ($candidates | Select-Object -Unique)) {
        # -LiteralPath: Steam library folders like "D:\Games [SSD]" would
        # otherwise be read as wildcard character classes.
        if ($c -and (Test-Path -LiteralPath (Join-Path $c 'acs.exe'))) {
            return $c
        }
    }
    return $null
}

function Resolve-AcPath {
    if ($AcPath) {
        if (Test-Path -LiteralPath (Join-Path $AcPath 'acs.exe')) { return $AcPath }
        Write-Warn2 "-AcPath '$AcPath' does not contain acs.exe; falling back to auto-detection."
    }
    return Find-AssettoCorsa
}

# ======================================================================
# Uninstall path
# ======================================================================

if ($Uninstall) {
    Write-Step "Uninstalling"

    # Only Claude Desktop is registered automatically, so it is the only client
    # there is anything to unregister from. If you wired this into another MCP
    # client by hand, remove the 'assetto-mcp' entry from its config yourself.
    Unregister-FromClaudeDesktop

    $ac = Resolve-AcPath
    if ($ac) {
        Remove-LegacyLuaApp $ac
        $dest = Join-Path $ac 'apps\lua\assetto_mcp'
        if (Test-Path -LiteralPath $dest) {
            try {
                Remove-Item -LiteralPath $dest -Recurse -Force
                Write-Ok "Removed in-game Lua app from $dest"
            } catch {
                Write-Warn2 "Could not remove $dest - is Assetto Corsa running? ($($_.Exception.Message))"
            }
        } else {
            Write-Info "No in-game Lua app installed"
        }
    } else {
        Write-Info "Assetto Corsa folder not found; in-game app (if any) left in place."
        Write-Info "Re-run with -AcPath ""<assettocorsa folder>"" to remove it."
    }

    Write-Host "`n  Done. Your telemetry database was left untouched.`n" -ForegroundColor White
    if ($script:PauseAtEnd) { Read-Host "Press Enter to close" }
    exit 0
}

# ======================================================================
# 1. Find Python
# ======================================================================

Write-Step "Looking for Python 3.10 or newer"

function Test-PythonCandidate ($exe) {
    # The Microsoft Store ships an app-execution-alias stub at
    # %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe that opens the Store
    # instead of running Python. Its sandboxing also breaks the shared-memory
    # access this server needs, so reject that whole folder.
    if (-not $exe -or $exe -like "*\WindowsApps\*") { return $null }
    $r = Invoke-Native { & $exe -c "import sys,json;print(json.dumps([list(sys.version_info[:3]),sys.executable]))" }
    if ($r.ExitCode -ne 0) { return $null }
    # Ignore any stderr chatter; take the last real line of stdout.
    $line = ($r.Output | Where-Object { $_ -is [string] -and $_.Trim().StartsWith('[') } |
             Select-Object -Last 1)
    if (-not $line) { return $null }
    try { $parsed = $line | ConvertFrom-Json } catch { return $null }
    $ver = $parsed[0]
    if ($ver[0] -lt 3 -or ($ver[0] -eq 3 -and $ver[1] -lt 10)) { return $null }
    return [pscustomobject]@{
        Path    = $parsed[1]
        Version = [version]("{0}.{1}.{2}" -f $ver[0], $ver[1], $ver[2])
    }
}

$found = @()

# The py launcher is the most reliable entry point on Windows. Collect every
# interpreter it knows about rather than stopping at the first hit, so a box
# with both 3.10 and 3.14 installed gets the newer one.
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($tag in @('-3', '-3.14', '-3.13', '-3.12', '-3.11', '-3.10')) {
        $r = Invoke-Native { & py $tag -c "import sys;print(sys.executable)" }
        if ($r.ExitCode -eq 0) {
            $exe = ($r.Output | Where-Object { $_ -is [string] -and $_.Trim() } |
                    Select-Object -Last 1)
            if ($exe) {
                $cand = Test-PythonCandidate $exe.Trim()
                if ($cand) { $found += $cand }
            }
        }
    }
}

foreach ($name in @('python', 'python3')) {
    foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
        $cand = Test-PythonCandidate $cmd.Source
        if ($cand) { $found += $cand }
    }
}

$python = $found |
    Sort-Object -Property Version -Descending |
    Group-Object -Property Path |
    ForEach-Object { $_.Group[0] } |
    Sort-Object -Property Version -Descending |
    Select-Object -First 1

if (-not $python) {
    Stop-WithError "No usable Python 3.10+ found." @"
  Install Python from https://www.python.org/downloads/
  IMPORTANT: on the first screen of the installer, tick
      [x] Add python.exe to PATH
  Do NOT install Python from the Microsoft Store - its sandboxing
  breaks the shared-memory access this server needs.

  Then run this installer again.
"@
}

Write-Ok "Python $($python.Version)"
Write-Info $python.Path

# ======================================================================
# 2. Install the package
# ======================================================================

Write-Step "Installing assetto-mcp (editable)"

Push-Location -LiteralPath $script:RepoRoot
try {
    $py = $python.Path
    # Best-effort; a failure here (e.g. all-users install without elevation)
    # is not fatal to the actual install below.
    Invoke-Native { & $py -m pip install --upgrade pip --quiet } | Out-Null

    $r = Invoke-Native { & $py -m pip install -e . }
    if ($r.ExitCode -ne 0) {
        $r.Output | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        Stop-WithError "pip install failed (see output above)." @"
  Common causes:
    - No internet connection, or a corporate proxy blocking PyPI
    - Python installed for 'all users' and this window is not elevated;
      try right-click install-windows.bat -> Run as administrator
"@
    }
} finally {
    Pop-Location
}
Write-Ok "Package installed"

# Sanity check: can the interpreter import it from *outside* the repo?
# Running from the repo root would succeed regardless, since cwd lands on
# sys.path and the package directory is right there.
$py = $python.Path
$r = Invoke-Native {
    Push-Location -LiteralPath ([System.IO.Path]::GetTempPath())
    try { & $py -c "import assetto_mcp" } finally { Pop-Location }
}
if ($r.ExitCode -ne 0) {
    Stop-WithError "Package installed but 'import assetto_mcp' failed." @"
  pip most likely installed into a different Python than the one detected.
  Try running this in a terminal to see the real error:
      "$($python.Path)" -c "import assetto_mcp"
"@
}
Write-Ok "Import check passed"

# ======================================================================
# 3. Register with an MCP client
# ======================================================================
#
# Claude Desktop is the only client this script configures automatically,
# because it is the only one whose config location needs a script to find.
# Every other MCP client takes the same two facts -- a command and its
# arguments -- pasted into its own config; -SkipClientConfig prints them.

if ($SkipClientConfig) {
    Write-Step "Skipping MCP client configuration (-SkipClientConfig)"
    Write-Info "Register this server with your client using:"
    Write-Info ""
    Write-Info "    command : $($python.Path)"
    Write-Info "    args    : -m assetto_mcp.server"
    Write-Info ""
    Write-Info "Most clients (Claude Desktop, Claude Code, Cursor, LM Studio, Windsurf)"
    Write-Info "take an ""mcpServers"" JSON object. See docs/INSTALL.md."
} else {
    Register-WithClaudeDesktop -PythonPath $python.Path
}

# ======================================================================
# 4. Data directory
# ======================================================================

Write-Step "Preparing data directory"

# Same resolution order as assetto_mcp/config.py: an explicit variable wins,
# then the pre-rename directory, which is *moved* rather than left behind. The
# installer is the best place to do it -- it runs once, with the client quit,
# so nothing has the database open. config.py retries the same move at startup
# for anyone who installed by hand and never runs this.
$defaultDataDir = Join-Path $env:USERPROFILE '.assetto-mcp'
# -PathType Container throughout: if the old name happens to be a *file*, a
# bare Test-Path is true and Move-Item cheerfully renames it into the place a
# directory is about to be created, which fails several lines later with the
# file already moved. assetto_mcp/config.py uses is_dir() for the same reason.
$haveLegacyDir  = Test-Path -LiteralPath $LegacyDataDir  -PathType Container
$haveDefaultDir = Test-Path -LiteralPath $defaultDataDir -PathType Container

if ($env:ASSETTO_MCP_DATA) {
    $dataDir = $env:ASSETTO_MCP_DATA
} elseif ($env:AC_ENGINEER_DATA) {
    $dataDir = $env:AC_ENGINEER_DATA
} elseif ($haveLegacyDir -and -not $haveDefaultDir) {
    try {
        Move-Item -LiteralPath $LegacyDataDir -Destination $defaultDataDir
        $dataDir = $defaultDataDir
        Write-Ok "Moved your laps from $LegacyDataDir to $defaultDataDir"
    } catch {
        # Almost always the client still running with the database open.
        # Keep using the old directory: a failed move must not look like a
        # lost season, and re-running after a proper quit will finish the job.
        $dataDir = $LegacyDataDir
        Write-Warn2 "Could not move $LegacyDataDir to the new name - something has it open."
        Write-Info  "Your laps are fine and still being used from the old folder."
        Write-Info  "Fully quit your MCP client and re-run this installer to finish the move."
    }
} else {
    $dataDir = $defaultDataDir
    # Both names present. Merging two databases by moving one onto the other
    # would be worse than leaving them, but saying nothing is worse still:
    # the new directory is the one that gets used, so a driver whose history
    # is in the old one just sees an empty history and no explanation.
    if ($haveLegacyDir -and $haveDefaultDir) {
        Write-Warn2 "Two data directories exist, and only the new one will be used."
        Write-Info  "  in use : $defaultDataDir"
        Write-Info  "  older  : $LegacyDataDir  (from before the rename)"
        Write-Info  "If your laps are missing, they are in the older folder. Fully quit"
        Write-Info  "your MCP client, move telemetry.db and ranges\ across, and re-run."
    }
}

$rangesDir = Join-Path $dataDir 'ranges'
# New-Item has no -LiteralPath and treats -Path as a wildcard pattern, so a
# folder like "D:\Games [SSD]" would be misread. .NET takes paths literally.
try {
    [void][System.IO.Directory]::CreateDirectory($rangesDir)
} catch {
    Stop-WithError "Could not create the data directory at $dataDir" @"
  $($_.Exception.Message)

  Check that the path is writable, or point somewhere else by setting
  ASSETTO_MCP_DATA and re-running.
"@
}
Write-Ok "$dataDir"
Write-Info "Car setup ranges go in: $rangesDir"

# ======================================================================
# 5. In-game Lua app
# ======================================================================

if (-not $SkipLuaApp) {
    Write-Step "Installing the in-game app (CSP Lua)"

    $ac = Resolve-AcPath

    if (-not $ac) {
        Write-Warn2 "Could not find your Assetto Corsa folder - skipping the in-game app."
        Write-Info  "Find it via Steam: right-click Assetto Corsa -> Manage -> Browse local files,"
        Write-Info  "then re-run:  install-windows.bat -AcPath ""<that folder>"""
    } else {
        Write-Ok "Found Assetto Corsa at $ac"
        Remove-LegacyLuaApp $ac
        $src  = Join-Path $script:RepoRoot 'lua_app\assetto_mcp'
        $dest = Join-Path $ac 'apps\lua\assetto_mcp'

        if (-not (Test-Path -LiteralPath $src)) {
            Write-Warn2 "lua_app\assetto_mcp missing from this repo - skipping"
        } else {
            try {
                # Literal, not wildcard: Steam libraries are often "D:\Games [SSD]".
                [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $dest))
                # Remove first: Copy-Item -Recurse onto an existing directory
                # would nest it as assetto_mcp\assetto_mcp.
                if (Test-Path -LiteralPath $dest) {
                    Remove-Item -LiteralPath $dest -Recurse -Force
                }
                Copy-Item -LiteralPath $src -Destination $dest -Recurse -Force
                Write-Ok "Copied in-game app to $dest"

                if (-not (Test-Path -LiteralPath (Join-Path $ac 'extension'))) {
                    Write-Warn2 "No 'extension' folder found - Custom Shaders Patch may not be installed. The in-game app needs CSP."
                }
            } catch {
                Write-Warn2 "Could not install the in-game app: $($_.Exception.Message)"
                Write-Info  "Close Assetto Corsa / Content Manager and re-run, or copy"
                Write-Info  "  $src"
                Write-Info  "to $dest by hand. The MCP server itself is already installed."
            }
        }
    }
}

# ======================================================================
# 6. Done
# ======================================================================

Write-Host ""
Write-Host "  Installation complete." -ForegroundColor Green
Write-Host ""

if ($script:Problems.Count -gt 0) {
    Write-Host "  Warnings:" -ForegroundColor Yellow
    foreach ($p in $script:Problems) { Write-Host "    - $p" -ForegroundColor Yellow }
    Write-Host ""
}

if ($SkipClientConfig) {
    Write-Host @"
  Next steps
  ----------
  1. Add the server to your MCP client's config (see docs/INSTALL.md), then
     fully restart the client.

  2. Start Assetto Corsa, get on track, and ask your assistant:
       "confirm you can see the session"

  3. In-game, open the apps sidebar (move mouse to the right edge) and
     enable "Assetto MCP" to bind complaint tags to wheel buttons.
"@ -ForegroundColor White
} else {
    # Logs live next to whichever config the app actually uses, so on a
    # packaged install they are NOT under %APPDATA%\Claude. Put the owner
    # config's log first, then any other location, since which build is
    # running can change.
    $logDirs = @($script:OwnerConfigDir) +
               @(Get-ClaudeConfigDirs | Where-Object { $_ -ne $script:OwnerConfigDir })
    $logHint = ($logDirs | ForEach-Object {
        "      notepad ""$(Join-Path $_ "logs\mcp-server-$ServerName.log")"""
    }) -join "`n"

    Write-Host @"
  Next steps
  ----------
  1. Fully quit Claude Desktop and start it again.
     Closing the window is NOT enough - right-click the Claude icon in the
     system tray (bottom-right, possibly under the '^' arrow) and choose Quit.

  2. In Claude Desktop, open a new chat and look for the tools icon.
     You should see assetto-mcp listed.

  3. Start Assetto Corsa, get on track, and ask:
       "confirm you can see the session"

  4. In-game, open the apps sidebar (move mouse to the right edge) and
     enable "Assetto MCP" to bind complaint tags to wheel buttons.

  If Claude does not see the server, read the log:
$logHint
"@ -ForegroundColor White
}

Write-Host ""
if ($script:PauseAtEnd) { Read-Host "Press Enter to close" }
