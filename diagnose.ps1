# Diagnostics for ac-race-engineer / Claude Desktop MCP.
# Read-only: it changes nothing. Writes diagnose-report.txt next to itself.

$ErrorActionPreference = 'Continue'
$root = if ($PSScriptRoot) { $PSScriptRoot }
        else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
$report = Join-Path $root 'diagnose-report.txt'
$lines = New-Object System.Collections.ArrayList

function L ($t) { [void]$lines.Add([string]$t); Write-Host $t }
function H ($t) { L ""; L ("=" * 70); L $t; L ("=" * 70) }

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

$candidates = @(
    (Join-Path $env:APPDATA      'Claude\claude_desktop_config.json')
    (Join-Path $env:LOCALAPPDATA 'Claude\claude_desktop_config.json')
    (Join-Path $env:LOCALAPPDATA 'AnthropicClaude\claude_desktop_config.json')
    (Join-Path $env:USERPROFILE  '.claude.json')
    (Join-Path $env:USERPROFILE  '.claude\settings.json')
    (Join-Path $root             '.mcp.json')
)

# Any other user profile on the box (installer run as a different user?)
Get-ChildItem 'C:\Users' -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $candidates += (Join-Path $_.FullName 'AppData\Roaming\Claude\claude_desktop_config.json')
}
# Microsoft Store package virtualisation, if any
Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Packages') -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like '*Anthropic*' -or $_.Name -like '*Claude*' } | ForEach-Object {
        $candidates += (Join-Path $_.FullName 'LocalCache\Roaming\Claude\claude_desktop_config.json')
    }

$configPaths = @()
foreach ($c in ($candidates | Select-Object -Unique)) {
    if (Test-Path -LiteralPath $c) { $configPaths += $c; L "FOUND   $c" }
    else                           { L "absent  $c" }
}

foreach ($p in $configPaths) {
    H "CONFIG: $p"
    $fi = Get-Item -LiteralPath $p
    L "size $($fi.Length) bytes, modified $($fi.LastWriteTime)"

    $bytes = [System.IO.File]::ReadAllBytes($p)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        L "!! FILE STARTS WITH A UTF-8 BOM - Claude Desktop's parser rejects this."
    } else { L "BOM: none (good)" }

    $raw = Get-Content -Raw -LiteralPath $p
    try {
        $cfg = $raw | ConvertFrom-Json
        L "JSON: parses OK"
        if ($cfg.mcpServers) {
            $names = @($cfg.mcpServers.PSObject.Properties.Name)
            L "mcpServers keys: $($names -join ', ')"
            foreach ($n in $names) {
                $e = $cfg.mcpServers.$n
                L "  [$n] command = $($e.command)"
                L "  [$n] args    = $($e.args -join ' ')"
                if ($e.command) {
                    if (Test-Path -LiteralPath $e.command) { L "  [$n] command exists on disk: YES" }
                    else { L "  [$n] command exists on disk: NO  <-- broken path" }
                }
            }
        } else { L "!! no 'mcpServers' key at the top level" }
    } catch {
        L "!! JSON DOES NOT PARSE: $($_.Exception.Message)"
    }
    L "--- raw contents ---"
    L $raw
    L "--- end ---"
}

# ---------------------------------------------------------------- python
H "PYTHON"
$pyExes = @()
foreach ($tag in @('-3.14','-3.13','-3.12','-3.11','-3.10','-3')) {
    $o = & py $tag -c "import sys;print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $o) { $pyExes += ($o | Select-Object -Last 1).Trim() }
}
foreach ($n in @('python','python3')) {
    foreach ($c in @(Get-Command $n -All -ErrorAction SilentlyContinue)) { $pyExes += $c.Source }
}
$pyExes = $pyExes | Where-Object { $_ } | Select-Object -Unique
if (-not $pyExes) { L "!! no python interpreters found at all" }
foreach ($exe in $pyExes) {
    L ""
    L "interpreter: $exe"
    if ($exe -like '*\WindowsApps\*') { L "  (Microsoft Store stub - unusable for this server)" ; continue }
    $v = & $exe -c "import sys;print(sys.version.split()[0])" 2>&1
    L "  version: $v"
    Push-Location ([System.IO.Path]::GetTempPath())
    $imp = & $exe -c "import ac_race_engineer,sys;print('import OK ->',ac_race_engineer.__file__)" 2>&1
    $impCode = $LASTEXITCODE
    $srv = & $exe -c "import ac_race_engineer.server;print('server module OK')" 2>&1
    $srvCode = $LASTEXITCODE
    Pop-Location
    L "  import ac_race_engineer          : $(if ($impCode -eq 0) {'OK'} else {'FAILED'})"
    L "    $($imp -join "`n    ")"
    L "  import ac_race_engineer.server   : $(if ($srvCode -eq 0) {'OK'} else {'FAILED'})"
    L "    $($srv -join "`n    ")"
}

# --------------------------------------------------- does it actually run
H "COLD-START TEST (what Claude Desktop effectively does)"
$cfgMain = Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'
$cmd = $null; $cargs = $null
if (Test-Path -LiteralPath $cfgMain) {
    try {
        $c = (Get-Content -Raw -LiteralPath $cfgMain | ConvertFrom-Json)
        if ($c.mcpServers -and $c.mcpServers.'ac-race-engineer') {
            $cmd   = $c.mcpServers.'ac-race-engineer'.command
            $cargs = @($c.mcpServers.'ac-race-engineer'.args)
        }
    } catch { }
}
if (-not $cmd) {
    L "No usable ac-race-engineer entry in $cfgMain - skipping."
} else {
    L "running: `"$cmd`" $($cargs -join ' ')   (2s, then killed)"
    $stdout = [System.IO.Path]::GetTempFileName()
    $stderr = [System.IO.Path]::GetTempFileName()
    try {
        $p = Start-Process -FilePath $cmd -ArgumentList $cargs -NoNewWindow -PassThru `
                           -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        Start-Sleep -Seconds 2
        if (-not $p.HasExited) { L "still running after 2s -> server starts cleanly"; $p.Kill() }
        else { L "!! exited early with code $($p.ExitCode)" }
    } catch { L "!! could not launch: $($_.Exception.Message)" }
    L "--- stdout ---"; L ((Get-Content -Raw $stdout) -replace '\s+$','')
    L "--- stderr ---"; L ((Get-Content -Raw $stderr) -replace '\s+$','')
    Remove-Item $stdout,$stderr -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- logs
H "CLAUDE MCP LOGS"
$logDir = Join-Path $env:APPDATA 'Claude\logs'
if (Test-Path -LiteralPath $logDir) {
    Get-ChildItem $logDir -Filter 'mcp*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 6 | ForEach-Object {
            L ""
            L "--- $($_.Name)  ($($_.LastWriteTime), $($_.Length) bytes) ---"
            L ((Get-Content -LiteralPath $_.FullName -Tail 40) -join "`n")
        }
} else { L "no log directory at $logDir" }

# ---------------------------------------------------------------- app
H "CLAUDE DESKTOP PROCESSES"
$procs = Get-Process -Name 'Claude*' -ErrorAction SilentlyContinue
if ($procs) { $procs | ForEach-Object { L "$($_.ProcessName) pid=$($_.Id) started=$($_.StartTime)" } }
else { L "Claude Desktop is not running" }

[System.IO.File]::WriteAllText($report, ($lines -join "`r`n"),
                               (New-Object System.Text.UTF8Encoding($false)))
Write-Host ""
Write-Host "Report written to: $report" -ForegroundColor Green
Write-Host "Tell Claude it's ready." -ForegroundColor Green
Write-Host ""
