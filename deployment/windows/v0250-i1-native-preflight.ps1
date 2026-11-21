#requires -Version 5.1
<#
Windows v0.25.0 I1 file-only Core native preflight.
READ ONLY. Run from a normal (non-elevated) FullLanguage PowerShell session.
This script does not create files, install packages, register tasks, start
processes, read credentials or modify any running ServerFS service.
#>
[CmdletBinding()]
param(
    [ValidatePattern('^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$')]
    [string]$InstanceId = 'i1-smoke',
    [ValidateRange(1024, 65535)]
    [int]$Port = 18868
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    throw 'I1 preflight requires a normal FullLanguage host session.'
}
if ($InstanceId -ceq 'default') {
    throw 'Default is a production compatibility identity, not a smoke fixture.'
}
$principal = [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'I1 preflight requires a non-elevated current-user session.'
}

$localData = [Environment]::GetFolderPath('LocalApplicationData')
if ([string]::IsNullOrWhiteSpace($localData)) {
    throw 'OS-provided LocalApplicationData is unavailable.'
}
$base = Join-Path $localData 'ServerFS\v25'
$taskName = 'ServerFS v25 ' + $InstanceId + ' Core'
$target = Join-Path $base ('instances\' + $InstanceId)
$all = @(Get-ScheduledTask -ErrorAction Stop)

foreach ($required in @(
    'ServerFS Native Tunnel',
    'ServerFS v25 Core',
    'ServerFS v25 Cloudflare Connector'
)) {
    $found = @($all | Where-Object {
        $_.TaskPath -eq '\' -and $_.TaskName -ceq $required
    })
    if ($found.Count -ne 1 -or [string]$found[0].State -ne 'Running' -or
        -not [bool]$found[0].Settings.Enabled) {
        throw 'Existing production ingress/Core baseline differs: abort.'
    }
}
foreach ($required in @(
    'ServerFS v25 Agent Bridge',
    'ServerFS v25 OpenAI Connector'
)) {
    $found = @($all | Where-Object {
        $_.TaskPath -eq '\' -and $_.TaskName -ceq $required
    })
    if ($found.Count -ne 1 -or [bool]$found[0].Settings.Enabled -or
        [string]$found[0].State -eq 'Running') {
        throw 'Independent Bridge/OpenAI candidate baseline differs: abort.'
    }
}
$existing = @($all | Where-Object {
    $_.TaskPath -eq '\' -and $_.TaskName -ceq $taskName
})
if ($existing.Count -ne 0) {
    throw 'The proposed isolated Core task already exists; refuse replacement.'
}
if (Test-Path -LiteralPath $target) {
    throw 'The proposed isolated Core data directory already exists; review ownership first.'
}

$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop)
if (@($listeners | Where-Object { $_.LocalPort -eq $Port }).Count -ne 0) {
    throw 'The proposed Core TCP port is already occupied.'
}
$corePy = Join-Path $base 'envs\core\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $corePy -PathType Leaf)) {
    throw 'Current pinned production Core interpreter is missing.'
}
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).ProviderPath
if (-not (Test-Path -LiteralPath (Join-Path $repo '.venv\Scripts\python.exe') -PathType Leaf)) {
    throw 'Repository test Python is missing.'
}
Write-Output 'I1_NATIVE_PREFLIGHT=PASS'
Write-Output ('INSTANCE_ID=' + $InstanceId)
Write-Output ('PROPOSED_PORT=' + $Port)
Write-Output 'EXISTING_PRODUCTION_TASKS=UNCHANGED'
Write-Output 'NEW_INSTANCE_TASK=ABSENT'
Write-Output 'NEW_INSTANCE_DATA_DIR=ABSENT'
Write-Output 'NEW_LOOPBACK_PORT=UNBOUND'
Write-Output 'PRODUCTION_CREDENTIALS=NOT_READ'
