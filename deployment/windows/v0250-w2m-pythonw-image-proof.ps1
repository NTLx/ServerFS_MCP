#requires -Version 5.1
<#
Windows v0.25 W2M: isolated, short-lived PythonW interpreter image probe.
Normal non-admin FullLanguage PowerShell:
    & .\deployment\windows\v0250-w2m-pythonw-image-proof.ps1

Does NOT start Core or touch Task Scheduler registration. The only new
process is a short-lived PythonW running a fixed file of boolean comparisons,
never a ServerFS service entrypoint. Old tasks must remain Running/Enabled,
four new component tasks must remain Disabled.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    throw 'W2M requires current-user FullLanguage PowerShell.'
}
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).ProviderPath
$driver = Join-Path $repo '.venv\Scripts\python.exe'
$script = Join-Path $repo 'deployment\windows\v0250-w2m-pythonw-image-proof.py'
$report = Join-Path $repo (
    'docs\diagnostics\v0250-windows-w2m-pythonw-host-' +
    [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '.md'
)
$legacy = @('ServerFS Native Tunnel', 'ServerFS Cloudflare WorkPC')
$new = @(
    'ServerFS MCP Core', 'ServerFS Agent Bridge',
    'ServerFS Cloudflare Connector', 'ServerFS OpenAI Connector'
)
$rows = New-Object System.Collections.Generic.List[string]
$ok = $false
$stage = 'initial'

function Assert-FixedState {
    $tasks = @(Get-ScheduledTask -ErrorAction Stop)
    foreach ($name in $script:legacy) {
        $found = @($tasks | Where-Object { $_.TaskName -ceq $name -and $_.TaskPath -eq '\' })
        if ($found.Count -ne 1 -or [string]$found[0].State -ne 'Running' -or
            -not [bool]$found[0].Settings.Enabled) {
            throw 'One existing production task is not Running/Enabled.'
        }
    }
    foreach ($name in $script:new) {
        $found = @($tasks | Where-Object { $_.TaskName -ceq $name -and $_.TaskPath -eq '\' })
        if ($found.Count -ne 1 -or [bool]$found[0].Settings.Enabled -or
            [string]$found[0].State -eq 'Running') {
            throw 'One new component task is not Disabled.'
        }
    }
}

if (-not (Test-Path -LiteralPath $driver -PathType Leaf) -or
    -not (Test-Path -LiteralPath $script -PathType Leaf)) {
    throw 'W2M fixed-source interpreter or one-shot probe is unavailable.'
}

Push-Location $repo
try {
    $stage = 'six-existing-task-baseline'
    Assert-FixedState
    $rows.Add('- initial task state: two old Running/Enabled, four new Disabled')

    $stage = 'short-lived-dedicated-pythonw-only'
    $prior = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $driver '-I' '-B' $script 2>&1 | Out-Null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prior
    }
    if ($exitCode -ne 0) {
        throw 'Nonservice PythonW identity proof did not return OBSERVED.'
    }
    $rows.Add('- short-lived dedicated PythonW probe: OBSERVED')

    $stage = 'six-existing-task-postflight'
    Assert-FixedState
    $rows.Add('- final task state: two old Running/Enabled, four new Disabled')
    $ok = $true
} catch {
    $rows.Add('- ' + $stage + ': FAILED (' +
        $_.Exception.GetType().Name + '/' + [string]$_.CategoryInfo.Category + ')')
} finally {
    # Even a failed PythonW probe never uses task mutation as cleanup.
    try {
        Assert-FixedState
        $rows.Add('- final fixed-task inventory: PASS')
    } catch {
        $rows.Add('- final fixed-task inventory: UNVERIFIED')
        $ok = $false
    }
    Pop-Location
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('# Windows v0.25.0 — nonservice PythonW image proof')
    $lines.Add('')
    $lines.Add('UTC: ' + [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))
    $lines.Add('Overall: ' + $(if ($ok) { 'PASS' } else { 'NOT_ACCEPTED' }))
    $lines.Add('Last stage: ' + $stage)
    $lines.Add('')
    $lines.Add('## Fixed-state host observations')
    foreach ($row in $rows) { $lines.Add($row) }
    $lines.Add('')
    $lines.Add('This does not enable/start/stop/modify any Task Scheduler task.')
    $lines.Add('No Core, Agent Bridge, Connector, provider or HTTP listener is run.')
    $lines.Add('The separate PythonW probe reports only fixed boolean comparisons.')
    $enc = New-Object System.Text.UTF8Encoding($false)
    $stream = [System.IO.File]::Open(
        $report, [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write, [System.IO.FileShare]::None
    )
    try {
        $writer = New-Object System.IO.StreamWriter($stream, $enc)
        try {
            foreach ($line in $lines) { $writer.WriteLine($line) }
            $writer.Flush()
        } finally { $writer.Dispose() }
    } finally { $stream.Dispose() }
    Write-Output ('W2M PythonW host report: ' + $report)
}
if (-not $ok) {
    throw ('W2M PythonW image evidence NOT_ACCEPTED; stage: ' + $stage)
}
