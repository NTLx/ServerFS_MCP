#requires -Version 5.1
<#
W2K native, DISPOSABLE Scheduler XML normalization test.
Normal current-user FullLanguage PowerShell only:
    & .\deployment\windows\v0250-w2k-scheduler-normalization-probe.ps1

Never mutates the real Core or old/new ServerFS tasks. Python registers
one unique task with a NONEXISTENT executable, disabled LogonTrigger and
a TimeTrigger beginning in 2099; enables the definition but never /Run.
Python disables and deletes ONLY its test-created task, fail closed on
unknown identity. A cleanup anomaly is reported, never hidden.
#>
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    throw 'W2K requires normal current-user FullLanguage PowerShell.'
}
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).ProviderPath
$python = Join-Path $repo '.venv\Scripts\python.exe'
$probe = Join-Path $repo 'deployment\windows\v0250-w2k-scheduler-normalization-probe.py'
$report = Join-Path $repo (
    'docs\diagnostics\v0250-windows-w2k-scheduler-host-' +
    [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '.md'
)
$old = @('ServerFS Native Tunnel','ServerFS Cloudflare WorkPC')
$new = @(
    'ServerFS MCP Core','ServerFS Agent Bridge',
    'ServerFS Cloudflare Connector','ServerFS OpenAI Connector'
)
$rows = New-Object System.Collections.Generic.List[string]
$ok = $false
$stage = 'initial'
$fixtureCalled = $false
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
    -not (Test-Path -LiteralPath $probe -PathType Leaf)) {
    throw 'W2K source Python or fixed probe is unavailable.'
}
function Assert-ActualTasks {
    $all = @(Get-ScheduledTask -ErrorAction Stop)
    foreach ($name in $script:old) {
        $items = @($all | Where-Object { $_.TaskName -eq $name -and $_.TaskPath -eq '\' })
        if ($items.Count -ne 1 -or [string]$items[0].State -ne 'Running' -or
            -not [bool]$items[0].Settings.Enabled) {
            throw 'A real legacy service is not Running/Enabled.'
        }
    }
    foreach ($name in $script:new) {
        $items = @($all | Where-Object { $_.TaskName -eq $name -and $_.TaskPath -eq '\' })
        if ($items.Count -ne 1 -or [string]$items[0].State -eq 'Running' -or
            [bool]$items[0].Settings.Enabled) {
            throw 'A real independent component is not registered/Disabled.'
        }
    }
    $leftover = @($all | Where-Object { $_.TaskName -like 'ServerFS W2K XML Probe *' })
    if ($leftover.Count -ne 0) {
        throw 'A W2K disposable task is already registered; refusing a new one.'
    }
}
Push-Location $repo
try {
    $stage = 'real-service-baseline-before'
    Assert-ActualTasks
    $rows.Add('- two legacy Running/Enabled; four new Disabled; no W2K fixture: PASS')

    $stage = 'disposable-nonrunnable-XML-transition'
    $fixtureCalled = $true
    $prior = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $python '-I' '-B' $probe 2>&1 | Out-Null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prior
    }
    if ($exitCode -ne 0) {
        throw 'Disposable Scheduler XML transition or fixture cleanup was rejected.'
    }
    $rows.Add('- nonrunnable disposable task create/enable/disable/delete: PASS')

    $stage = 'real-service-baseline-after'
    Assert-ActualTasks
    $rows.Add('- two legacy Running/Enabled, four new Disabled, no disposable task: PASS')
    $ok = $true
} catch {
    $rows.Add('- ' + $stage + ': FAILED (' +
        $_.Exception.GetType().Name + '/' + [string]$_.CategoryInfo.Category + ')')
} finally {
    try {
        $all = @(Get-ScheduledTask -ErrorAction Stop)
        foreach ($name in @($old) + @($new)) {
            $found = @($all | Where-Object { $_.TaskName -eq $name -and $_.TaskPath -eq '\' })
            if ($found.Count -eq 1) {
                $rows.Add('- actual-task/' + $name + ': ' + [string]$found[0].State +
                    '/enabled=' + [string][bool]$found[0].Settings.Enabled)
            } else {
                $rows.Add('- actual-task/' + $name + ': ABSENT_OR_AMBIGUOUS')
                $ok = $false
            }
        }
        $leftover = @($all | Where-Object { $_.TaskName -like 'ServerFS W2K XML Probe *' })
        $rows.Add('- disposable-tasks-left: ' + [string]$leftover.Count)
        if ($leftover.Count -ne 0) { $ok = $false }
    } catch {
        $rows.Add('- final-task-inventory: UNVERIFIED')
        $ok = $false
    }
    Pop-Location
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('# Windows v0.25.0 W2K — isolated Task Scheduler XML probe')
    $lines.Add('')
    $lines.Add('UTC: ' + [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))
    $lines.Add('Overall: ' + $(if ($ok) { 'PASS' } else { 'NOT_ACCEPTED' }))
    $lines.Add('Last stage: ' + $stage)
    $lines.Add('Disposable test attempted: ' + [string]$fixtureCalled)
    $lines.Add('')
    $lines.Add('## Fixed-name current OS observations')
    foreach ($row in $rows) { $lines.Add($row) }
    $lines.Add('')
    $lines.Add('Existing Core, Bridge, Connectors and two legacy Tunnel tasks')
    $lines.Add('were only inspected, not changed. The fixture action executable')
    $lines.Add('did not exist; time trigger is in 2099 and logon trigger disabled.')
    $lines.Add('No /Run or network request or credential access performed.')
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $stream = [System.IO.File]::Open(
        $report,[System.IO.FileMode]::CreateNew,[System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $writer = New-Object System.IO.StreamWriter($stream,$encoding)
        try {
            foreach ($line in $lines) { $writer.WriteLine($line) }
            $writer.Flush()
        } finally { $writer.Dispose() }
    } finally { $stream.Dispose() }
    Write-Output ('W2K host scheduler diagnosis: ' + $report)
}
if (-not $ok) { throw ('W2K Scheduler XML diagnosis NOT_ACCEPTED; gate: ' + $stage) }
