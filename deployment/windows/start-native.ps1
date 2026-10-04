# Start the single-active native Windows ServerFS tunnel (ChatGPT connector).
# Layout contract: repo root holds serverfs.toml and its sibling .env;
# runtime artifacts live under %LOCALAPPDATA%\ServerFS; the Control Plane
# API key file lives outside every workdir (%USERPROFILE%\.config\serverfs).
# Run from anywhere; all paths are derived from this script's own location.

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$config = Join-Path $repoRoot 'serverfs.toml'
$envFile = Join-Path $repoRoot '.env'
$serverfs = Join-Path $env:LOCALAPPDATA 'ServerFS\connector-env\Scripts\serverfs.exe'
$apiKey = Join-Path $env:USERPROFILE '.config\serverfs\api-key'
$logDir = Join-Path $env:LOCALAPPDATA 'ServerFS\logs'

foreach ($required in @($config, $envFile, $serverfs, $apiKey)) {
    if (-not (Test-Path $required)) { throw "required file missing: $required" }
}

$tunnelId = $null
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*CONTROL_PLANE_TUNNEL_ID\s*=\s*(.+?)\s*$') {
        $tunnelId = $Matches[1].Trim('"').Trim("'")
        break
    }
}
if (-not $tunnelId) { throw "CONTROL_PLANE_TUNNEL_ID not found in $envFile" }

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# No --env-file: `serverfs tunnel` auto-discovers the sibling .env next to the
# config. No --tunnel-client: the pinned binary is resolved from the default
# data home (%LOCALAPPDATA%\ServerFS\bin), installed by
# `serverfs bootstrap tunnel-client`.
$launcher = Start-Process -FilePath $serverfs -ArgumentList @(
    'tunnel',
    '--config', $config,
    '--tunnel-id', $tunnelId,
    '--api-key-file', $apiKey
) -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logDir 'native-tunnel.out.log') `
    -RedirectStandardError (Join-Path $logDir 'native-tunnel.err.log')

Write-Output "launcher PID=$($launcher.Id)"
Write-Output "logs: $logDir"
