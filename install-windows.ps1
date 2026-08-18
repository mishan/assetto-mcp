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
Write-Host "  ac-race-engineer installer" -ForegroundColor White
Write-Host "  $script:RepoRoot" -ForegroundColor DarkGray

# --- paths --------------------------------------------------------------

# $env:APPDATA is PowerShell's way of saying %APPDATA%; it resolves to
# C:\Users\<you>\AppData\Roaming. AppData is hidden in Explorer by default,
# which is why browsing to it by hand is miserable.
$ServerName            = 'ac-race-engineer'
$script:OwnerConfigDir = Join-Path $env:APPDATA 'Claude'   # refined in step 3

# ...except when Claude Desktop is the MSIX-packaged build - which is what
# Anthropic's own Windows download installs, not just the Store version. MSIX
# packages get a private, redirected view of %APPDATA%: the app writes and reads
#   %LOCALAPPDATA%\Packages\<PackageFamilyName>\LocalCache\Roaming\Claude\
# and once a file exists in that layer it *shadows* the real %APPDATA% copy.
# So writing only to %APPDATA%\Claude silently does nothing there - the entry
# lands in a file the app will never open.
#
# We enumerate every location, but we do NOT write the entry to all of them:
# see Select-OwnerConfig below for why exactly one config must own it.
function Get-ClaudeConfigCandidates {
    $out = @()

    $plain = Join-Path $env:APPDATA 'Claude'
    $out += [pscustomobject]@{
        Dir           = $plain
        Path          = Join-Path $plain 'claude_desktop_config.json'
        IsPackage     = $false
        PackageFamily = $null
        NameRank      = 0
    }

    $packages = Join-Path $env:LOCALAPPDATA 'Packages'
    if (Test-Path -LiteralPath $packages) {
        foreach ($pkg in @(Get-ChildItem -LiteralPath $packages -Directory -ErrorAction SilentlyContinue)) {
            # The redirect directory actually existing - not the folder name -
            # is what proves this package is a Claude Desktop build. The old
            # 'Claude*' -or '*Anthropic*' name filter was loose enough to catch
            # unrelated packages, so name matching is now only used to *rank*
            # candidates, never to exclude them: a differently named package
            # that has the redirect dir still works, it just ranks lower.
            $d = Join-Path $pkg.FullName 'LocalCache\Roaming\Claude'
            if (-not (Test-Path -LiteralPath $d)) { continue }

            $rank = 1
            if ($pkg.Name -like 'AnthropicClaude*' -or $pkg.Name -like 'Claude_*') {
                $rank = 3      # the shapes Anthropic's own installer produces
            } elseif ($pkg.Name -like 'Claude*' -or $pkg.Name -like '*Anthropic*') {
                $rank = 2
            }

            $out += [pscustomobject]@{
                Dir           = $d
                Path          = Join-Path $d 'claude_desktop_config.json'
                IsPackage     = $true
                PackageFamily = $pkg.Name
                NameRank      = $rank
            }
        }
    }

    # De-duplicate on directory, keeping the first occurrence.
    $seen = @{}
    $uniq = @()
    foreach ($c in $out) {
        $k = $c.Dir.ToLowerInvariant()
        if (-not $seen.ContainsKey($k)) { $seen[$k] = $true; $uniq += $c }
    }
    return @($uniq)
}

function Get-ClaudeConfigDirs {
    return @(Get-ClaudeConfigCandidates | ForEach-Object { $_.Dir })
}

function Get-ClaudeConfigPaths {
    return @(Get-ClaudeConfigCandidates | ForEach-Object { $_.Path })
}

# An MSIX package runs from
#   %PROGRAMFILES%\WindowsApps\<Name>_<version>_<arch>__<PublisherId>\claude.exe
# (the package *full* name) while its redirected AppData lives in
#   %LOCALAPPDATA%\Packages\<Name>_<PublisherId>
# (the package *family* name). Turn the former into the latter.
function Get-PackageFamilyFromExePath {
    param([string] $ExePath)
    if (-not $ExePath) { return $null }
    $m = [regex]::Match($ExePath, '(?i)\\WindowsApps\\([^\\]+)\\')
    if (-not $m.Success) { return $null }
    $parts = $m.Groups[1].Value -split '_'
    if ($parts.Count -lt 2) { return $null }
    return ($parts[0] + '_' + $parts[$parts.Count - 1])
}

# Exactly ONE config file may carry the ac-race-engineer entry.
#
# If several do, every Claude surface that reads one of them spawns its own
# copy of the MCP server, and those copies then fight over the single
# 127.0.0.1:9666 bridge socket and the same SQLite file. bridge.py has ~90
# lines of defensive code for precisely that situation; don't cause it here.
#
# Precedence (first match wins), preferring the config the *running* app reads:
#   1. The config belonging to a Claude process running right now. Reading
#      another process's .Path needs rights we may not have, so it's guarded.
#   2. Otherwise: an existing config that already has an mcpServers key, most
#      recently written - that is the file something is actively maintaining.
#   3. Otherwise: an existing MSIX config over the plain %APPDATA% one, because
#      the redirect layer shadows %APPDATA% whenever it has a copy.
#   4. Otherwise: %APPDATA%\Claude\claude_desktop_config.json.
function Select-OwnerConfig {
    param($Candidates)

    function New-Result ($cand, $reason) {
        return [pscustomobject]@{ Owner = $cand; Reason = $reason }
    }

    # --- 1. follow the running process --------------------------------
    $exes = @()
    try {
        foreach ($proc in @(Get-Process -Name 'Claude*' -ErrorAction SilentlyContinue)) {
            $p = $null
            try { $p = $proc.Path } catch { $p = $null }
            if ($p) { $exes += $p }
        }
    } catch { $exes = @() }

    foreach ($exe in @($exes | Select-Object -Unique)) {
        $fam = Get-PackageFamilyFromExePath $exe
        if ($fam) {
            $hit = @($Candidates | Where-Object { $_.IsPackage -and $_.PackageFamily -eq $fam })
            if ($hit.Count -eq 0) {
                $stem = ($fam -split '_')[0]
                $hit = @($Candidates | Where-Object { $_.IsPackage -and $_.PackageFamily -like "$stem*" })
            }
            if ($hit.Count -gt 0) {
                return (New-Result $hit[0] "Claude Desktop is running right now from MSIX package '$fam'")
            }
        } else {
            $hit = @($Candidates | Where-Object { -not $_.IsPackage })
            if ($hit.Count -gt 0) {
                return (New-Result $hit[0] "Claude Desktop is running right now as an unpackaged build ($exe)")
            }
        }
    }

    # --- gather facts about the files that exist ----------------------
    $existing = @()
    foreach ($c in $Candidates) {
        if (-not (Test-Path -LiteralPath $c.Path)) { continue }
        $hasServers = $false
        $lastWrite  = [datetime]::MinValue
        try {
            $lastWrite = (Get-Item -LiteralPath $c.Path).LastWriteTime
            $raw = Get-Content -Raw -LiteralPath $c.Path
            if ($raw -and $raw.Trim()) {
                $j = $raw | ConvertFrom-Json
                if ($j -and $j.PSObject.Properties['mcpServers']) { $hasServers = $true }
            }
        } catch { }
        $existing += [pscustomobject]@{
            Cand = $c; HasServers = $hasServers; LastWrite = $lastWrite
        }
    }

    # --- 2. the file already defining mcpServers, most recently written -
    $withServers = @($existing | Where-Object { $_.HasServers } |
                     Sort-Object -Property @{Expression={$_.LastWrite};Descending=$true})
    if ($withServers.Count -gt 0) {
        return (New-Result $withServers[0].Cand `
                "it already defines mcpServers and is the most recently written config (last written $($withServers[0].LastWrite))")
    }

    # --- 3. an existing MSIX config shadows %APPDATA% -------------------
    $pkg = @($existing | Where-Object { $_.Cand.IsPackage } |
             Sort-Object -Property @{Expression={$_.Cand.NameRank};Descending=$true},
                                   @{Expression={$_.LastWrite};Descending=$true})
    if ($pkg.Count -gt 0) {
        return (New-Result $pkg[0].Cand `
                "an MSIX redirect layer exists ($($pkg[0].Cand.PackageFamily)) and shadows %APPDATA%")
    }

    # --- 4. default -----------------------------------------------------
    $plain = @($Candidates | Where-Object { -not $_.IsPackage })
    if ($plain.Count -gt 0) {
        return (New-Result $plain[0] "default location; no packaged build and no existing config found")
    }
    return (New-Result $Candidates[0] "only candidate found")
}

# Backups are byte-for-byte copies of claude_desktop_config.json, so they can
# contain OTHER MCP servers' API keys and tokens. Keep the 5 most recent per
# config file and delete the rest rather than letting credential copies pile
# up forever in a folder nobody ever looks at.
function Remove-OldBackups ($configPath) {
    try {
        $dir  = Split-Path -Parent $configPath
        $leaf = Split-Path -Leaf   $configPath
        $baks = @(Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -like "$leaf.*.bak" } |
                  Sort-Object -Property LastWriteTime -Descending)
        if ($baks.Count -gt 5) {
            foreach ($old in $baks[5..($baks.Count - 1)]) {
                Remove-Item -LiteralPath $old.FullName -Force -ErrorAction SilentlyContinue
            }
        }
    } catch { }
}

function Backup-ConfigFile ($path) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $bak   = "$path.$stamp.bak"
    Copy-Item -LiteralPath $path -Destination $bak
    Remove-OldBackups $path
    return $bak
}

# Remove the ac-race-engineer entry from one config, preserving every other
# MCP server and every other top-level setting. Only rewrites the file if the
# entry was actually there. Returns 'removed', 'absent' or 'error'.
function Remove-ServerEntry ($path) {
    if (-not (Test-Path -LiteralPath $path)) { return 'absent' }

    $cfg = $null
    try {
        $raw = Get-Content -Raw -LiteralPath $path
        if ($raw -and $raw.Trim()) { $cfg = $raw | ConvertFrom-Json }
    } catch {
        Write-Warn2 "Not valid JSON, leaving it alone: $path"
        return 'error'
    }
    if (-not $cfg) { return 'absent' }
    if (-not $cfg.PSObject.Properties['mcpServers'] -or -not $cfg.mcpServers) { return 'absent' }
    if (-not $cfg.mcpServers.PSObject.Properties[$ServerName]) { return 'absent' }

    $others = @($cfg.mcpServers.PSObject.Properties.Name | Where-Object { $_ -ne $ServerName })
    try {
        Backup-ConfigFile $path | Out-Null
        $cfg.mcpServers.PSObject.Properties.Remove($ServerName)
        Write-JsonFile $path ($cfg | ConvertTo-Json -Depth 20)
    } catch {
        Write-Warn2 "Could not update $path - $($_.Exception.Message)"
        return 'error'
    }
    if ($others.Count -gt 0) {
        Write-Info "Kept the $($others.Count) other MCP server(s) there: $($others -join ', ')"
    }
    return 'removed'
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

    # Uninstall clears the entry from EVERY config location, not just the one
    # the installer would have chosen as owner.
    foreach ($ClaudeConfigPath in (Get-ClaudeConfigPaths)) {
        if (-not (Test-Path -LiteralPath $ClaudeConfigPath)) { continue }
        $status = Remove-ServerEntry $ClaudeConfigPath
        if     ($status -eq 'removed') { Write-Ok   "Removed '$ServerName' from $ClaudeConfigPath" }
        elseif ($status -eq 'absent')  { Write-Info "No '$ServerName' entry in $ClaudeConfigPath" }
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

$configCandidates = Get-ClaudeConfigCandidates
$selection        = Select-OwnerConfig $configCandidates
$OwnerConfigPath  = $selection.Owner.Path
$script:OwnerConfigDir = $selection.Owner.Dir

Write-Info "Claude config locations found: $($configCandidates.Count)"
foreach ($c in $configCandidates) {
    $tag = if ($c.IsPackage) { "MSIX $($c.PackageFamily)" } else { 'plain %APPDATA%' }
    Write-Info "  [$tag] $($c.Path)"
}
Write-Ok   "This entry will live in: $OwnerConfigPath"
Write-Info "Chosen because: $($selection.Reason)"
if ($configCandidates.Count -gt 1) {
    Write-Info "Any '$ServerName' entry in the other location(s) will be removed so"
    Write-Info "only one copy of the server can ever be launched."
}

# An unpackaged build reads %APPDATA%\Claude, so create the owner directory if
# it's missing. Package folders are never created here - if the redirect layer
# doesn't already exist, there's no packaged build to configure.
# ([IO.Directory]::CreateDirectory, not New-Item: New-Item has no -LiteralPath
# and its -Path globs, which mangles paths containing [ ].)
if (-not (Test-Path -LiteralPath $script:OwnerConfigDir)) {
    [void][System.IO.Directory]::CreateDirectory($script:OwnerConfigDir)
    Write-Info "Created $($script:OwnerConfigDir)"
}

# Absolute python path, not bare "python": Claude Desktop launches MCP
# servers without your shell's PATH, so a bare command often silently fails.
$entry = [pscustomobject]@{
    command = $python.Path
    args    = @('-m', 'ac_race_engineer.server')
}

$written  = @()
$failures = @()

# --- the owner config gets the entry ------------------------------------
$config  = $null
$rawOld  = $null
if (Test-Path -LiteralPath $OwnerConfigPath) {
    $rawOld = Get-Content -Raw -LiteralPath $OwnerConfigPath
    if ($rawOld -and $rawOld.Trim()) {
        try {
            $config = $rawOld | ConvertFrom-Json
        } catch {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            Copy-Item -LiteralPath $OwnerConfigPath `
                      -Destination "$OwnerConfigPath.corrupt-$stamp.bak"
            Remove-OldBackups $OwnerConfigPath
            Write-Warn2 "Not valid JSON; saved as .corrupt-$stamp.bak and starting fresh: $OwnerConfigPath"
            $config = $null
        }
    }
}

if (-not $config) { $config = [pscustomobject]@{} }

if (-not $config.PSObject.Properties['mcpServers']) {
    $config | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
}
if ($null -eq $config.mcpServers) {
    $config.mcpServers = [pscustomobject]@{}
}

$existingNames = @($config.mcpServers.PSObject.Properties.Name)
if ($existingNames -contains $ServerName) {
    $config.mcpServers.$ServerName = $entry
} else {
    $config.mcpServers | Add-Member -NotePropertyName $ServerName -NotePropertyValue $entry
}

$newText = $config | ConvertTo-Json -Depth 20

if ($null -ne $rawOld -and $rawOld -eq $newText) {
    Write-Ok "Already up to date, left unchanged: $OwnerConfigPath"
    $written += $OwnerConfigPath
} else {
    try {
        if ($rawOld -and $rawOld.Trim()) {
            $bak = Backup-ConfigFile $OwnerConfigPath
            Write-Info "Backed up to $(Split-Path -Leaf $bak)"
        }
        Write-JsonFile $OwnerConfigPath $newText
        Write-Ok "Wrote $OwnerConfigPath"
        $written += $OwnerConfigPath
    } catch {
        $failures += "$OwnerConfigPath - $($_.Exception.Message)"
        Write-Warn2 "Could not write $OwnerConfigPath"
    }
}

if ($written.Count -gt 0) {
    $others = @($existingNames | Where-Object { $_ -ne $ServerName })
    if ($others.Count -gt 0) {
        Write-Info "Preserved $($others.Count) other MCP server(s): $($others -join ', ')"
    }
    # Everything outside mcpServers (window prefs, cowork paths, ...) is
    # carried through by round-tripping the whole object, not rebuilt.
    $otherKeys = @($config.PSObject.Properties.Name | Where-Object { $_ -ne 'mcpServers' })
    if ($otherKeys.Count -gt 0) {
        Write-Info "Preserved other settings: $($otherKeys -join ', ')"
    }
}

# --- every other config must NOT have the entry -------------------------
# Two configs carrying it means two Claude surfaces each spawning their own
# server process, both racing for 127.0.0.1:9666 and the same database.
foreach ($c in $configCandidates) {
    if ($c.Path -eq $OwnerConfigPath) { continue }
    $status = Remove-ServerEntry $c.Path
    if     ($status -eq 'removed') { Write-Ok   "Removed the duplicate '$ServerName' entry from $($c.Path)" }
    elseif ($status -eq 'absent')  { Write-Info "No duplicate entry in $($c.Path) (unchanged)" }
}

if ($written.Count -eq 0) {
    Stop-WithError "Could not write the Claude Desktop config." @"
  $($failures -join "`n  ")

  Close Claude Desktop completely (system tray icon -> Quit) and try again.
  If a backup was made, your previous config is safe alongside it as a .bak.
"@
}

# ======================================================================
# 4. Data directory
# ======================================================================

Write-Step "Preparing data directory"

$dataDir = if ($env:AC_ENGINEER_DATA) { $env:AC_ENGINEER_DATA }
           else { Join-Path $env:USERPROFILE '.ac-race-engineer' }
$rangesDir = Join-Path $dataDir 'ranges'
# New-Item has no -LiteralPath and treats -Path as a wildcard pattern, so a
# folder like "D:\Games [SSD]" would be misread. .NET takes paths literally.
[void][System.IO.Directory]::CreateDirectory($rangesDir)
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
                # Literal, not wildcard: Steam libraries are often "D:\Games [SSD]".
                [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $dest))
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

# Logs live next to whichever config the app actually uses, so on a packaged
# install they are NOT under %APPDATA%\Claude. Put the owner config's log
# first, then any other location, since which build is running can change.
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
     You should see ac-race-engineer listed.

  3. Start Assetto Corsa, get on track, and tell Claude:
       "start recording and confirm you can see the session"

  4. In-game, open the apps sidebar (move mouse to the right edge) and
     enable "Race Engineer" to bind complaint tags to wheel buttons.

  If Claude does not see the server, read the log:
$logHint
"@ -ForegroundColor White

Write-Host ""
if ($script:PauseAtEnd) { Read-Host "Press Enter to close" }
