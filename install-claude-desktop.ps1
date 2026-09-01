<#
.SYNOPSIS
    Claude Desktop registration for assetto-mcp. A component of
    install-windows.ps1, not a standalone script.

.DESCRIPTION
    The server itself is a plain stdio MCP server and knows nothing about any
    particular client. Everything that is specific to *Claude Desktop* -- where
    its config file lives, how MSIX packaging redirects that path, how to merge
    into it without disturbing other MCP servers -- lives here, so that adding
    another client later means adding a file beside this one rather than
    untangling it from the installer.

    Dot-sourced by install-windows.ps1, which means it runs in that script's
    scope and uses its helpers (Write-Step / Write-Ok / Write-Info /
    Write-Warn2 / Write-JsonFile / Stop-WithError) and its variables
    ($ServerName, $LegacyServerName). Running this file on its own does
    nothing but define functions.

    Targets Windows PowerShell 5.1 as well as PowerShell 7.

    Entry points:
      Register-WithClaudeDesktop -PythonPath <path>
      Unregister-FromClaudeDesktop
      Get-ClaudeConfigDirs
#>

# --- locating the config file -------------------------------------------
#
# $env:APPDATA is PowerShell's way of saying %APPDATA%; it resolves to
# C:\Users\<you>\AppData\Roaming. AppData is hidden in Explorer by default,
# which is why browsing to it by hand is miserable.
#
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

# Exactly ONE config file may carry the assetto-mcp entry.
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

# --- editing the config file --------------------------------------------

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

# A single run writes the owner config more than once - the main write, then
# the sweep that removes the pre-rename entry - and a second-resolution stamp
# gives both the same filename. Copy-Item overwrites without complaint, so the
# second backup used to clobber the first and the user's actual pre-install
# state was the thing that got lost. Never overwrite an existing backup.
function Backup-ConfigFile ($path) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $bak   = "$path.$stamp.bak"
    $n = 1
    while (Test-Path -LiteralPath $bak) {
        $bak = "$path.$stamp-$n.bak"
        $n++
    }
    Copy-Item -LiteralPath $path -Destination $bak
    Remove-OldBackups $path
    return $bak
}

# Remove one named server entry from one config, preserving every other
# MCP server and every other top-level setting. Only rewrites the file if the
# entry was actually there. Returns 'removed', 'absent' or 'error'.
#
# The name is a parameter because this server used to be called
# ac-race-engineer, and an install that predates the rename leaves that entry
# behind pointing at a module that no longer exists. Claude Desktop would keep
# launching it and keep showing a dead server.
function Remove-ServerEntry ($path, $name = $ServerName) {
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
    if (-not $cfg.mcpServers.PSObject.Properties[$name]) { return 'absent' }

    # Both of our own names are excluded from the "kept" list, not just the one
    # being removed right now. Listing the pre-rename entry as kept, one line
    # before the next pass removes it, is a report that contradicts itself.
    # Where-Object { $_ } also drops the $null that an empty PSObject property
    # collection yields, which otherwise reported "1 other server: ".
    $others = @($cfg.mcpServers.PSObject.Properties.Name |
                Where-Object { $_ -and $_ -ne $ServerName -and $_ -ne $LegacyServerName })
    try {
        Backup-ConfigFile $path | Out-Null
        $cfg.mcpServers.PSObject.Properties.Remove($name)
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

# --- entry points --------------------------------------------------------

# Clear our entry, current and pre-rename, from EVERY config location rather
# than just the one an install would have chosen as owner.
function Unregister-FromClaudeDesktop {
    foreach ($path in (Get-ClaudeConfigPaths)) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $status = Remove-ServerEntry $path
        if     ($status -eq 'removed') { Write-Ok   "Removed '$ServerName' from $path" }
        elseif ($status -eq 'absent')  { Write-Info "No '$ServerName' entry in $path" }
        if ((Remove-ServerEntry $path $LegacyServerName) -eq 'removed') {
            Write-Ok "Removed the pre-rename '$LegacyServerName' entry from $path"
        }
    }
}

# Write the stdio server entry into the one config Claude Desktop actually
# reads, and make sure no other config carries it. Sets $script:OwnerConfigDir
# so the caller can point at the right log folder afterwards.
function Register-WithClaudeDesktop {
    param([Parameter(Mandatory=$true)][string] $PythonPath)

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

    # An unpackaged build reads %APPDATA%\Claude, so create the owner directory
    # if it's missing. Package folders are never created here - if the redirect
    # layer doesn't already exist, there's no packaged build to configure.
    # ([IO.Directory]::CreateDirectory, not New-Item: New-Item has no
    # -LiteralPath and its -Path globs, which mangles paths containing [ ].)
    if (-not (Test-Path -LiteralPath $script:OwnerConfigDir)) {
        [void][System.IO.Directory]::CreateDirectory($script:OwnerConfigDir)
        Write-Info "Created $($script:OwnerConfigDir)"
    }

    # Absolute python path, not bare "python": MCP clients launch servers
    # without your shell's PATH, so a bare command often silently fails.
    $entry = [pscustomobject]@{
        command = $PythonPath
        args    = @('-m', 'assetto_mcp.server')
    }

    $written  = @()
    $failures = @()

    # --- the owner config gets the entry --------------------------------
    $config  = $null
    $rawOld  = $null
    $alreadyBackedUp = $false
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
                # This copy IS the backup. Taking the ordinary one as well
                # produces two identical files, both of which count against
                # the keep-5 budget and push a real older backup out.
                $alreadyBackedUp = $true
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

    # Drop the pre-rename entry here, in the same object we are about to write,
    # rather than in a second pass over the same file afterwards. A second pass
    # meant a second write and a second backup in the same second, and since
    # backup filenames are stamped to the second, the second backup overwrote
    # the first -- so the file the user actually wanted preserved was the one
    # that got lost. It also read strangely: "preserved ac-race-engineer",
    # immediately followed by "removed ac-race-engineer".
    $droppedLegacy = $false
    if ($config.mcpServers.PSObject.Properties[$LegacyServerName]) {
        $config.mcpServers.PSObject.Properties.Remove($LegacyServerName)
        $droppedLegacy = $true
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
            if ($rawOld -and $rawOld.Trim() -and -not $alreadyBackedUp) {
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
        if ($droppedLegacy) {
            Write-Ok "Removed the pre-rename '$LegacyServerName' entry from $OwnerConfigPath"
        }
        # Where-Object { $_ }: an empty PSObject property collection yields a
        # single $null, which passes a bare -ne test and reported "1 other
        # server: " on every clean install.
        $others = @($existingNames | Where-Object { $_ -and $_ -ne $ServerName })
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

    # --- every other config must NOT have the entry ---------------------
    # Two configs carrying it means two Claude surfaces each spawning their own
    # server process, both racing for 127.0.0.1:9666 and the same database.
    foreach ($c in $configCandidates) {
        if ($c.Path -eq $OwnerConfigPath) { continue }
        $status = Remove-ServerEntry $c.Path
        if     ($status -eq 'removed') { Write-Ok   "Removed the duplicate '$ServerName' entry from $($c.Path)" }
        elseif ($status -eq 'absent')  { Write-Info "No duplicate entry in $($c.Path) (unchanged)" }
    }

    # --- and no config may keep the pre-rename entry --------------------
    # It names a module that no longer exists, so Claude Desktop launches it,
    # gets ModuleNotFoundError, and shows a failed server beside the working
    # one. The owner config is skipped: its copy went out with the write above.
    foreach ($c in $configCandidates) {
        if ($c.Path -eq $OwnerConfigPath) { continue }
        if ((Remove-ServerEntry $c.Path $LegacyServerName) -eq 'removed') {
            Write-Ok "Removed the pre-rename '$LegacyServerName' entry from $($c.Path)"
        }
    }

    if ($written.Count -eq 0) {
        Stop-WithError "Could not write the Claude Desktop config." @"
  $($failures -join "`n  ")

  Close Claude Desktop completely (system tray icon -> Quit) and try again.
  If a backup was made, your previous config is safe alongside it as a .bak.
"@
    }
}
