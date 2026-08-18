<#
.SYNOPSIS
    One-shot installer for ac-race-engineer on the Windows gaming PC.

.DESCRIPTION
    Does everything the README used to ask you to do by hand:

      1. Finds a real Python 3.10+ (ignoring the Microsoft Store stub)
      2. pip install -e . from this folder
      3. Writes/merges claude_desktop_config.json, preserving any MCP
         servers you already have, with a timestamped backup
      4. Finds your Assetto Corsa install and copies the CSP Lua app in
      5. Creates the data + ranges directories
      6. Prints exactly what to do next

    Everything is idempotent: run it again after a git pull and it will
    update in place rather than duplicating anything.

    Targets Windows PowerShell 5.1 (the one built into Windows) as well as
    PowerShell 7 - no PS7-only syntax is used.

.PARAMETER AcPath
    Path to your assettocorsa folder, if auto-detection fails.
    e.g. -AcPath "D:\Games\steamapps\common\assettocorsa"

.PARAMETER SkipLuaApp
    Don't install the in-game CSP Lua app.

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
    try {
        $out = & $Block 2>&1
        return [pscustomobject]@{ Output = $out; ExitCode = $LASTEXITCODE }
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
Write-Host "  ac-race-engineer installer" -ForegroundColor White
Write-Host "  $script:RepoRoot" -ForegroundColor DarkGray

# --- paths --------------------------------------------------------------

# $env:APPDATA is PowerShell's way of saying %APPDATA%; it resolves to
# C:\Users\<you>\AppData\Roaming. AppData is hidden in Explorer by default,
# which is why browsing to it by hand is miserable.
$ClaudeConfigDir  = Join-Path $env:APPDATA 'Claude'
$ClaudeConfigPath = Join-Path $ClaudeConfigDir 'claude_desktop_config.json'
$ServerName       = 'ac-race-engineer'

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

    if (Test-Path -LiteralPath $ClaudeConfigPath) {
        $cfg = $null
        try {
            $raw = Get-Content -Raw -LiteralPath $ClaudeConfigPath
            if ($raw.Trim()) { $cfg = $raw | ConvertFrom-Json }
        } catch {
            Write-Warn2 "claude_desktop_config.json is not valid JSON; leaving it alone."
        }

        if ($cfg -and $cfg.mcpServers -and
            $cfg.mcpServers.PSObject.Properties[$ServerName]) {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            Copy-Item -LiteralPath $ClaudeConfigPath `
                      -Destination "$ClaudeConfigPath.$stamp.bak"
            $cfg.mcpServers.PSObject.Properties.Remove($ServerName)
            Write-JsonFile $ClaudeConfigPath ($cfg | ConvertTo-Json -Depth 20)
            Write-Ok "Removed '$ServerName' from claude_desktop_config.json"
        } elseif ($cfg) {
            Write-Info "No '$ServerName' entry in claude_desktop_config.json"
        }
    }

    $ac = Resolve-AcPath
    if ($ac) {
        $dest = Join-Path $ac 'apps\lua\race_engineer'
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

Write-Step "Installing ac-race-engineer (editable)"

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
    try { & $py -c "import ac_race_engineer" } finally { Pop-Location }
}
if ($r.ExitCode -ne 0) {
    Stop-WithError "Package installed but 'import ac_race_engineer' failed." @"
  pip most likely installed into a different Python than the one detected.
  Try running this in a terminal to see the real error:
      "$($python.Path)" -c "import ac_race_engineer"
"@
}
Write-Ok "Import check passed"

# ======================================================================
# 3. Claude Desktop config
# ======================================================================

Write-Step "Configuring Claude Desktop"

if (-not (Test-Path -LiteralPath $ClaudeConfigDir)) {
    New-Item -ItemType Directory -Force -Path $ClaudeConfigDir | Out-Null
    Write-Info "Created $ClaudeConfigDir"
}

$config = $null
if (Test-Path -LiteralPath $ClaudeConfigPath) {
    $raw = Get-Content -Raw -LiteralPath $ClaudeConfigPath
    if ($raw.Trim()) {
        try {
            $config = $raw | ConvertFrom-Json
        } catch {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            Copy-Item -LiteralPath $ClaudeConfigPath `
                      -Destination "$ClaudeConfigPath.corrupt-$stamp.bak"
            Write-Warn2 "Existing config was not valid JSON; saved as .corrupt-$stamp.bak and starting fresh"
            $config = $null
        }
    }
    if ($config) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        Copy-Item -LiteralPath $ClaudeConfigPath `
                  -Destination "$ClaudeConfigPath.$stamp.bak"
        Write-Info "Backed up existing config to claude_desktop_config.json.$stamp.bak"
    }
}

if (-not $config) { $config = [pscustomobject]@{} }

if (-not $config.PSObject.Properties['mcpServers']) {
    $config | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
}
if ($null -eq $config.mcpServers) {
    $config.mcpServers = [pscustomobject]@{}
}

# Absolute python path, not bare "python": Claude Desktop launches MCP
# servers without your shell's PATH, so a bare command often silently fails.
$entry = [pscustomobject]@{
    command = $python.Path
    args    = @('-m', 'ac_race_engineer.server')
}

$existingNames = @($config.mcpServers.PSObject.Properties.Name)
if ($existingNames -contains $ServerName) {
    $config.mcpServers.$ServerName = $entry
    Write-Ok "Updated existing '$ServerName' entry"
} else {
    $config.mcpServers | Add-Member -NotePropertyName $ServerName -NotePropertyValue $entry
    Write-Ok "Added '$ServerName' entry"
}

$others = @($existingNames | Where-Object { $_ -ne $ServerName })
if ($others.Count -gt 0) {
    Write-Info "Preserved $($others.Count) other MCP server(s): $($others -join ', ')"
}

try {
    Write-JsonFile $ClaudeConfigPath ($config | ConvertTo-Json -Depth 20)
    Write-Ok "Wrote $ClaudeConfigPath"
} catch {
    Stop-WithError "Could not write $ClaudeConfigPath" @"
  $($_.Exception.Message)

  Close Claude Desktop completely and try again. If a backup was made,
  your previous config is safe alongside it as a .bak file.
"@
}

# ======================================================================
# 4. Data directory
# ======================================================================

Write-Step "Preparing data directory"

$dataDir = if ($env:AC_ENGINEER_DATA) { $env:AC_ENGINEER_DATA }
           else { Join-Path $env:USERPROFILE '.ac-race-engineer' }
$rangesDir = Join-Path $dataDir 'ranges'
New-Item -ItemType Directory -Force -Path $rangesDir | Out-Null
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
        $src  = Join-Path $script:RepoRoot 'lua_app\race_engineer'
        $dest = Join-Path $ac 'apps\lua\race_engineer'

        if (-not (Test-Path -LiteralPath $src)) {
            Write-Warn2 "lua_app\race_engineer missing from this repo - skipping"
        } else {
            try {
                New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
                # Remove first: Copy-Item -Recurse onto an existing directory
                # would nest it as race_engineer\race_engineer.
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

Write-Host @"
  Next steps
  ----------
  1. Fully quit Claude Desktop and start it again.
     Closing the window is NOT enough - right-click the Claude icon in the
     system tray (bottom-right, possibly under the '^' arrow) and choose Quit.

  2. In Claude Desktop, open a new chat and look for the tools icon.
     You should see ac-race-engineer listed.

  3. Start Assetto Corsa, get on track, and tell Claude:
       "start recording and confirm you can see the session"

  4. In-game, open the apps sidebar (move mouse to the right edge) and
     enable "Race Engineer" to bind complaint tags to wheel buttons.

  If Claude does not see the server, read the log. In PowerShell:
      notepad `$env:APPDATA\Claude\logs\mcp-server-$ServerName.log
  ...or in cmd.exe:
      notepad %APPDATA%\Claude\logs\mcp-server-$ServerName.log
"@ -ForegroundColor White

Write-Host ""
if ($script:PauseAtEnd) { Read-Host "Press Enter to close" }
