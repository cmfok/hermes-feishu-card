<#
.SYNOPSIS
    Install or update the feishu-card plugin for Hermes (card-mode Feishu outbound).

.DESCRIPTION
    Copies the plugin into a Hermes profile's plugins directory, enables it in that
    profile's config.yaml (idempotent, with backup + validation) and optionally
    restarts the gateway. All paths are derived from -HermesHome / $env:HERMES_HOME /
    $env:LOCALAPPDATA; nothing is hard coded.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install-plugin.ps1
    powershell -ExecutionPolicy Bypass -File install-plugin.ps1 -Profile feishu2 -Restart
#>
[CmdletBinding()]
param(
    [string]$Profile = "",
    [string]$HermesHome = "",
    [switch]$Restart
)

$ErrorActionPreference = "Continue"

function Say($m) { Write-Host $m }
function Ok($m) { Write-Host ("  [OK]   " + $m) }
function Warn($m) { Write-Host ("  [WARN] " + $m) }
function Bad($m) { Write-Host ("  [FAIL] " + $m) }

$pluginFiles = @("__init__.py", "card_plugin.py", "card_adapter.py", "card_session.py", "card_render.py", "plugin.yaml")

Say "feishu-card plugin installer"
Say "======================================================"

# --- 1. locate hermes home -------------------------------------------------
if (-not $HermesHome) {
    if ($env:HERMES_HOME) { $HermesHome = $env:HERMES_HOME }
    elseif ($env:LOCALAPPDATA) { $HermesHome = Join-Path $env:LOCALAPPDATA "hermes" }
}
if (-not $HermesHome -or -not (Test-Path $HermesHome)) {
    Bad "Hermes home not found (use -HermesHome <path>): $HermesHome"
    exit 2
}
Ok "hermes home: $HermesHome"

$targetRoot = if ($Profile) { Join-Path $HermesHome ("profiles\" + $Profile) } else { $HermesHome }
if (-not (Test-Path $targetRoot)) {
    Bad "profile directory not found: $targetRoot"
    exit 2
}
Ok ("target profile: " + $(if ($Profile) { $Profile } else { "default" }))

# --- 2. copy plugin files --------------------------------------------------
$src = Join-Path $PSScriptRoot "feishu-card"
if (-not (Test-Path $src)) {
    Bad "plugin source not found: $src"
    exit 2
}
$dst = Join-Path $targetRoot "plugins\feishu-card"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
foreach ($f in $pluginFiles) {
    $from = Join-Path $src $f
    if (-not (Test-Path $from)) { Bad "missing source file: $f"; exit 3 }
    Copy-Item -Force $from (Join-Path $dst $f)
}
Ok "plugin files installed: $dst"

# --- 3. enable in this profile's config.yaml -------------------------------
$config = Join-Path $targetRoot "config.yaml"
$python = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
$enabler = Join-Path $PSScriptRoot "enable-plugin.py"
if (Test-Path $enabler) {
    & $python $enabler $config
    if ($LASTEXITCODE -ne 0) {
        Warn "enable-plugin.py exit code $LASTEXITCODE - enable the plugin manually under plugins.enabled"
    } else {
        Ok "plugin enabled in config.yaml"
    }
} else {
    Warn "enable-plugin.py not found next to this script; add 'feishu-card' to plugins.enabled manually"
}

# --- 4. restart the gateway (optional) -------------------------------------
$cli = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"
if ($Restart) {
    if (Test-Path $cli) {
        $args = @("-m", "hermes_cli.main")
        if ($Profile) { $args += @("--profile", $Profile) }
        $args += @("gateway", "restart")
        Push-Location (Join-Path $HermesHome "hermes-agent")
        & $cli @args
        $code = $LASTEXITCODE
        Pop-Location
        if ($code -eq 0) { Ok "gateway restarted" } else { Warn "gateway restart exit code $code" }
    } else {
        Warn "hermes venv python not found; restart the gateway manually"
    }
} else {
    Say "  [SKIP] gateway not restarted (add -Restart to do it)"
}

# --- 5. verify -------------------------------------------------------------
$logsDir = Join-Path $targetRoot "logs"
if (Test-Path $logsDir) {
    $logFiles = Get-ChildItem -Path $logsDir -Filter "*.log" -ErrorAction SilentlyContinue
    $hit = $logFiles |
        Select-String -Pattern "[feishu-card] platform 'feishu' registered" -SimpleMatch -ErrorAction SilentlyContinue |
        Select-Object -Last 1
    if ($hit) {
        Ok ("verified in " + $hit.Filename + ": " + $hit.Line.Trim())
    } else {
        Warn "not verified yet - appears after the next gateway start (look for '[feishu-card] platform ...')"
    }
}

Say "======================================================"
Say "Done. Nothing else to do - this window can be closed."
exit 0
