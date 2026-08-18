# Diagnostics for ac-race-engineer / Claude Desktop MCP.
#
# Read-only with respect to your installation: it changes no config, installs
# nothing, and does not touch the live bridge port. It writes one file,
# diagnose-report.txt, next to itself.
#
# PRIVACY: the report contains local paths and your Windows username, and it
# lists the *names* of every MCP server and environment variable in your Claude
# config. Values that look like credentials are redacted on a best-effort
# basis - skim the file before sending it to anyone.
#
# Targets Windows PowerShell 5.1 as well as PowerShell 7.

$ErrorActionPreference = 'Continue'
$root = if ($PSScriptRoot) { $PSScriptRoot }
        else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
$report = Join-Path $root 'diagnose-report.txt'
$lines = New-Object System.Collections.ArrayList

$ServerName = 'ac-race-engineer'
$BridgePort = 9666

function L ($t) { [void]$lines.Add([string]$t); Write-Host $t }
function H ($t) { L ""; L ("=" * 70); L $t; L ("=" * 70) }

# ---------------------------------------------------------------- redaction
#
# The old version of this script dumped claude_desktop_config.json verbatim
# into the report. Any other MCP server's env block (GitHub PAT, API keys,
# database URLs) went out in cleartext to whoever the file was handed to.
# Nothing raw reaches the report now without going through Protect-Secrets.

$script:SecretPatterns = @(
    'gh[pousr]_[A-Za-z0-9]{16,}',              # GitHub classic tokens
    'github_pat_[A-Za-z0-9_]{20,}',            # GitHub fine-grained PATs
    'sk-[A-Za-z0-9\-_]{16,}',                  # OpenAI / Anthropic style
    'xox[baprs]-[A-Za-z0-9\-]{8,}',            # Slack
    'AIza[0-9A-Za-z\-_]{20,}',                 # Google API keys
    '(?i)\bbearer\s+[A-Za-z0-9\-\._~\+/=]{10,}',
    '(?i)://[^/\s:@"]+:[^/\s:@"]+@',           # user:password@host in a URL
    # Long base64-ish run. The two lookaheads require it to mix letters AND
    # digits, so real blobs are caught while ordinary long words are not
    # (otherwise every '@modelcontextprotocol/...' package name is masked).
    '\b(?=[A-Za-z0-9+/]*[0-9])(?=[A-Za-z0-9+/]*[A-Za-z])[A-Za-z0-9+/]{20,}={0,2}',
    '\b[0-9a-fA-F]{20,}\b'                     # long hex run
)

# Best effort, deliberately over-eager: a redacted diagnostic is recoverable,
# a leaked token is not.
function Protect-Secrets {
    param([string] $Text)
    if (-not $Text) { return $Text }
    $t = $Text

    # 1. anything whose *key* smells like a credential: "GITHUB_TOKEN": "..."
    $t = [regex]::Replace($t,
        '(?i)("[^"]*(?:token|secret|key|password|passwd|pwd|credential|auth)[^"]*"\s*:\s*")([^"]*)(")',
        '${1}<redacted>${3}')

    # 2. the same idea outside JSON:  API_KEY=..., password: ...
    $t = [regex]::Replace($t,
        '(?im)^(\s*[A-Za-z0-9_\-\.]*(?:token|secret|key|password|passwd|pwd|credential|auth)[A-Za-z0-9_\-\.]*\s*[:=]\s*)(\S+)',
        '${1}<redacted>')

    # 3. things that simply look like secrets wherever they appear
    foreach ($p in $script:SecretPatterns) {
        $t = [regex]::Replace($t, $p, '<redacted>')
    }
    return $t
}

# ------------------------------------------------------- config enumeration
#
# Mirrors Get-ClaudeConfigCandidates in install-windows.ps1. Deliberately
# duplicated rather than dot-sourced so this script keeps working on its own,
# but THE TWO MUST STAY IN SYNC: if the installer learns about a new config
# location, teach this one too.
#
# The MSIX-packaged build (what Anthropic's own Windows download installs)
# does not read %APPDATA%\Claude at all; it reads
#   %LOCALAPPDATA%\Packages\<PackageFamilyName>\LocalCache\Roaming\Claude\
function Get-ClaudeConfigDirs {
    $dirs = @(Join-Path $env:APPDATA 'Claude')
    $packages = Join-Path $env:LOCALAPPDATA 'Packages'
    if (Test-Path -LiteralPath $packages) {
        foreach ($pkg in @(Get-ChildItem -LiteralPath $packages -Directory -ErrorAction SilentlyContinue)) {
            # Existence of the redirect dir, not the package name, is the test.
            $d = Join-Path $pkg.FullName 'LocalCache\Roaming\Claude'
            if (Test-Path -LiteralPath $d) { $dirs += $d }
        }
    }
    return @($dirs | Select-Object -Unique)
}

H "ENVIRONMENT"
L "date        : $(Get-Date -Format s)"
L "whoami      : $(whoami)"
L "elevated    : $(([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
L "USERPROFILE : $env:USERPROFILE"
L "APPDATA     : $env:APPDATA"
L "LOCALAPPDATA: $env:LOCALAPPDATA"
L "PSVersion   : $($PSVersionTable.PSVersion)"
L "repo root   : $root"

# ---------------------------------------------------------------- configs
H "CLAUDE CONFIG FILES FOUND"

$claudeDirs = Get-ClaudeConfigDirs

$candidates = @()
foreach ($d in $claudeDirs) { $candidates += (Join-Path $d 'claude_desktop_config.json') }
$candidates += (Join-Path $env:LOCALAPPDATA 'Claude\claude_desktop_config.json')
$candidates += (Join-Path $env:LOCALAPPDATA 'AnthropicClaude\claude_desktop_config.json')
$candidates += (Join-Path $env:USERPROFILE  '.claude.json')
$candidates += (Join-Path $env:USERPROFILE  '.claude\settings.json')
$candidates += (Join-Path $root             '.mcp.json')

# Any other user profile on the box (installer run as a different user?)
Get-ChildItem 'C:\Users' -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $candidates += (Join-Path $_.FullName 'AppData\Roaming\Claude\claude_desktop_config.json')
}

$configPaths = @()
foreach ($c in ($candidates | Select-Object -Unique)) {
    if (Test-Path -LiteralPath $c) { $configPaths += $c; L "FOUND   $c" }
    else                           { L "absent  $c" }
}

$withEntry = @()   # configs that actually define our server, newest first

foreach ($p in $configPaths) {
    H "CONFIG: $p"
    $fi = Get-Item -LiteralPath $p
    L "size $($fi.Length) bytes, modified $($fi.LastWriteTime)"

    $bytes = [System.IO.File]::ReadAllBytes($p)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        L "!! FILE STARTS WITH A UTF-8 BOM - Claude Desktop's parser rejects this."
    } else { L "BOM: none (good)" }

    $raw = Get-Content -Raw -LiteralPath $p
    $cfg = $null
    try { $cfg = $raw | ConvertFrom-Json } catch { $cfg = $null }

    if ($null -eq $cfg) {
        L "!! JSON DOES NOT PARSE - Claude Desktop will ignore this file entirely."
        # Only for the unparseable case, and only after redaction: seeing the
        # actual text is the whole point when the syntax is what's broken.
        $shown = [string]$raw
        if ($shown.Length -gt 8000) {
            $shown = $shown.Substring(0, 8000) + "`n... (truncated, $($shown.Length) chars total)"
        }
        L "--- raw contents (secrets redacted, best effort) ---"
        L (Protect-Secrets $shown)
        L "--- end ---"
        continue
    }

    L "JSON: parses OK"
    # Structural summary only. Values are never echoed except command/args,
    # and those go through the redactor too.
    if ($cfg.PSObject.Properties['mcpServers'] -and $cfg.mcpServers) {
        $names = @($cfg.mcpServers.PSObject.Properties.Name)
        L "mcpServers keys: $($names -join ', ')"
        foreach ($n in $names) {
            $e = $cfg.mcpServers.$n
            L "  [$n] command : $(Protect-Secrets ([string]$e.command))"
            $a = @()
            if ($e.PSObject.Properties['args'] -and $e.args) {
                $a = @($e.args | ForEach-Object { [string]$_ })
            }
            L "  [$n] args    : $($a.Count) arg(s): $(Protect-Secrets ($a -join ' '))"
            if ($e.PSObject.Properties['env'] -and $e.env) {
                $envNames = @($e.env.PSObject.Properties.Name)
                L "  [$n] env     : $($envNames.Count) variable(s), names only:"
                foreach ($k in $envNames) { L "      $k = <redacted>" }
            }
            if ($e.command) {
                if (Test-Path -LiteralPath ([string]$e.command)) { L "  [$n] command exists on disk: YES" }
                else { L "  [$n] command exists on disk: NO  <-- broken path" }
            }
            if ($n -eq $ServerName) {
                $withEntry += [pscustomobject]@{
                    Path = $p; LastWrite = $fi.LastWriteTime
                    Command = [string]$e.command; Args = $a
                }
            }
        }
    } else {
        L "!! no 'mcpServers' key at the top level"
    }

    $topKeys = @($cfg.PSObject.Properties.Name | Where-Object { $_ -ne 'mcpServers' })
    if ($topKeys.Count -gt 0) { L "other top-level keys (names only): $($topKeys -join ', ')" }
}

if ($withEntry.Count -gt 1) {
    L ""
    L "!! '$ServerName' is defined in $($withEntry.Count) config files:"
    foreach ($w in $withEntry) { L "     $($w.Path)" }
    L "   If more than one of these is live, each Claude surface launches its own"
    L "   copy of the server and they fight over port $BridgePort and the database."
    L "   Re-run install-windows.bat: it keeps exactly one and clears the rest."
}

# ---------------------------------------------------------------- python
H "PYTHON"
$pyExes = @()
# The 'py' launcher is not always installed (it is optional in the python.org
# installer and absent from most embedded/conda setups) - record that fact in
# the report rather than throwing CommandNotFound at the console.
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($tag in @('-3.14','-3.13','-3.12','-3.11','-3.10','-3')) {
        $o = & py $tag -c "import sys;print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $o) { $pyExes += ($o | Select-Object -Last 1).Trim() }
    }
} else {
    L "py launcher : NOT INSTALLED (not fatal; python.exe on PATH is enough)"
}
foreach ($n in @('python','python3')) {
    foreach ($c in @(Get-Command $n -All -ErrorAction SilentlyContinue)) { $pyExes += $c.Source }
}
$pyExes = @($pyExes | Where-Object { $_ } | Select-Object -Unique)
if (-not $pyExes) { L "!! no python interpreters found at all" }
foreach ($exe in $pyExes) {
    L ""
    L "interpreter: $exe"
    if ($exe -like '*\WindowsApps\*') { L "  (Microsoft Store stub - unusable for this server)" ; continue }
    $v = & $exe -c "import sys;print(sys.version.split()[0])" 2>&1
    L "  version: $v"
    Push-Location ([System.IO.Path]::GetTempPath())
    try {
        $imp = & $exe -c "import ac_race_engineer,sys;print('import OK ->',ac_race_engineer.__file__)" 2>&1
        $impCode = $LASTEXITCODE
        $srv = & $exe -c "import ac_race_engineer.server;print('server module OK')" 2>&1
        $srvCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    L "  import ac_race_engineer          : $(if ($impCode -eq 0) {'OK'} else {'FAILED'})"
    L "    $($imp -join "`n    ")"
    L "  import ac_race_engineer.server   : $(if ($srvCode -eq 0) {'OK'} else {'FAILED'})"
    L "    $($srv -join "`n    ")"
}

# ------------------------------------------------------------ bridge port
# ac_race_engineer/server.py starts the HTTP bridge at import time, so ANY
# process that imports it binds this port immediately.
H "BRIDGE PORT $BridgePort"

function Get-PortListeners ($port) {
    $result = @()
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        $conns = @()
        try { $conns = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop) } catch { }
        foreach ($c in $conns) {
            $pname = '?'
            try { $pname = (Get-Process -Id $c.OwningProcess -ErrorAction Stop).ProcessName } catch { }
            $result += "pid $($c.OwningProcess) ($pname) listening on $($c.LocalAddress):$($c.LocalPort)"
        }
    } else {
        # Windows 7 / Server 2008 R2 and PowerShell without the NetTCPIP module.
        $ns = @(netstat -ano 2>$null |
                ForEach-Object { $_.ToString().Trim() } |
                Where-Object { $_ -match ":$port\s" -and $_ -match 'LISTENING' })
        foreach ($line in $ns) { $result += "netstat: $line" }
    }
    return @($result)
}

$listeners = Get-PortListeners $BridgePort
if ($listeners.Count -gt 0) {
    L "port $BridgePort is ALREADY IN USE:"
    foreach ($l in $listeners) { L "  $l" }
    L ""
    L "That is expected if the MCP server is running right now (Claude Desktop"
    L "started it). It is a PROBLEM if you also see a second copy, or if the"
    L "owner is not a python.exe - the in-game Lua app talks to whoever holds"
    L "this port, and bridge.py uses SO_EXCLUSIVEADDRUSE so a second copy"
    L "cannot bind and will sit retrying forever."
} else {
    L "port $BridgePort is free (no server currently running)"
}

# --------------------------------------------------- does it actually run
H "COLD-START TEST (what Claude Desktop effectively does)"

# Never launch the child on $BridgePort: it would either steal the port from a
# running server, or grab-and-drop it and leave it in TIME_WAIT for minutes
# (fatal with SO_EXCLUSIVEADDRUSE). Give the child a private port instead.
function Get-FreeTcpPort ($preferred) {
    for ($p = $preferred; $p -lt ($preferred + 60); $p++) {
        $listener = $null
        try {
            $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $p)
            $listener.Start()
            $listener.Stop()
            return $p
        } catch {
            if ($listener) { try { $listener.Stop() } catch { } }
        }
    }
    return 0
}

$cmd = $null; $cargs = @(); $cfgUsed = $null
if ($withEntry.Count -gt 0) {
    $pick = @($withEntry | Sort-Object -Property @{Expression={$_.LastWrite};Descending=$true})[0]
    $cmd = $pick.Command; $cargs = @($pick.Args); $cfgUsed = $pick.Path
}

if (-not $cmd) {
    L "No usable '$ServerName' entry in any config file found above - skipping."
    L "Run install-windows.bat to create one."
} else {
    $testPort = Get-FreeTcpPort 39666
    L "config used : $cfgUsed"
    L "running     : `"$cmd`" $($cargs -join ' ')   (2s, then killed)"
    if ($testPort -gt 0) {
        L "bridge port : $testPort for this test (AC_ENGINEER_BRIDGE_PORT), so the"
        L "              real port $BridgePort is never touched"
    } else {
        L "!! could not find a free test port; skipping the cold-start test to"
        L "   avoid disturbing port $BridgePort."
    }

    if ($testPort -gt 0) {
        $stdout = [System.IO.Path]::GetTempFileName()
        $stderr = [System.IO.Path]::GetTempFileName()
        $prevPort = $env:AC_ENGINEER_BRIDGE_PORT
        $env:AC_ENGINEER_BRIDGE_PORT = "$testPort"
        try {
            # -ArgumentList rejects an empty array, so only pass it when there is one.
            if ($cargs.Count -gt 0) {
                $p = Start-Process -FilePath $cmd -ArgumentList $cargs -NoNewWindow -PassThru `
                                   -RedirectStandardOutput $stdout -RedirectStandardError $stderr
            } else {
                $p = Start-Process -FilePath $cmd -NoNewWindow -PassThru `
                                   -RedirectStandardOutput $stdout -RedirectStandardError $stderr
            }
            Start-Sleep -Seconds 2
            if (-not $p.HasExited) {
                L "still running after 2s -> the interpreter and imports are fine"
                try { $p.Kill() } catch { }
            } else {
                L "!! exited early with code $($p.ExitCode) - see stderr below"
            }
        } catch {
            L "!! could not launch: $($_.Exception.Message)"
        } finally {
            $env:AC_ENGINEER_BRIDGE_PORT = $prevPort
        }
        Start-Sleep -Milliseconds 300   # let the handles flush before reading

        $so = ''
        $se = ''
        try { $so = (Get-Content -Raw -LiteralPath $stdout -ErrorAction SilentlyContinue) } catch { }
        try { $se = (Get-Content -Raw -LiteralPath $stderr -ErrorAction SilentlyContinue) } catch { }
        L "--- stdout ---"; L (Protect-Secrets (([string]$so) -replace '\s+$',''))
        L "--- stderr ---"; L (Protect-Secrets (([string]$se) -replace '\s+$',''))
        if (([string]$se).Trim()) {
            L ""
            L "(stderr is not automatically a failure - the server logs there - but"
            L " a traceback or 'address already in use' here is the actual problem.)"
        }
        Remove-Item -LiteralPath $stdout,$stderr -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------- logs
H "CLAUDE MCP LOGS"
# Logs sit next to whichever config the app actually reads, so on the packaged
# build they are NOT under %APPDATA%\Claude. Check every location.
$anyLogs = $false
foreach ($d in $claudeDirs) {
    $logDir = Join-Path $d 'logs'
    if (-not (Test-Path -LiteralPath $logDir)) {
        L "no log directory at $logDir"
        continue
    }
    $anyLogs = $true
    L ""
    L "log directory: $logDir"
    $logFiles = @(Get-ChildItem -LiteralPath $logDir -Filter 'mcp*.log' -ErrorAction SilentlyContinue |
                  Sort-Object LastWriteTime -Descending | Select-Object -First 6)
    if ($logFiles.Count -eq 0) { L "  (no mcp*.log files here)" }
    foreach ($f in $logFiles) {
        L ""
        L "--- $($f.Name)  ($($f.LastWriteTime), $($f.Length) bytes) ---"
        L (Protect-Secrets ((Get-Content -LiteralPath $f.FullName -Tail 40) -join "`n"))
    }
}
if (-not $anyLogs) {
    L ""
    L "No Claude log directory anywhere - Claude Desktop has probably never run"
    L "since the config was written. Quit it from the tray and start it again."
}

# ---------------------------------------------------------------- app
H "CLAUDE DESKTOP PROCESSES"
$procs = Get-Process -Name 'Claude*' -ErrorAction SilentlyContinue
if ($procs) {
    foreach ($proc in $procs) {
        $started = '?'
        $path    = '(path not readable)'
        try { $started = $proc.StartTime } catch { }
        try { if ($proc.Path) { $path = $proc.Path } } catch { }
        L "$($proc.ProcessName) pid=$($proc.Id) started=$started"
        L "    $path"
    }
} else { L "Claude Desktop is not running" }

# ---------------------------------------------------------------- output
$header = @(
    "ac-race-engineer diagnostic report"
    "generated $(Get-Date -Format s) by diagnose.ps1"
    ""
    "PRIVACY NOTE"
    "  This file contains local file paths and your Windows username."
    "  Values that look like credentials (API keys, tokens, passwords) have"
    "  been redacted on a BEST-EFFORT basis and appear as <redacted>."
    "  Redaction is pattern based and cannot be perfect - skim the file"
    "  before sharing it with anyone, including Claude."
    ""
) -join "`r`n"

[System.IO.File]::WriteAllText($report, $header + ($lines -join "`r`n"),
                               (New-Object System.Text.UTF8Encoding($false)))
Write-Host ""
Write-Host "Report written to: $report" -ForegroundColor Green
Write-Host ""
Write-Host "This report includes local paths and your username, and lists the names" -ForegroundColor Yellow
Write-Host "of your other MCP servers and their environment variables. Secret-looking" -ForegroundColor Yellow
Write-Host "values were redacted automatically, but that is best effort only." -ForegroundColor Yellow
Write-Host ""
Write-Host "Open it, skim it, then tell Claude it's ready to read." -ForegroundColor Green
Write-Host "  notepad `"$report`"" -ForegroundColor Gray
Write-Host ""
