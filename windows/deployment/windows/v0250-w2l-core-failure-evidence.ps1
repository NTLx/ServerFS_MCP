#requires -Version 5.1
<#
Read-only W2L failure forensics for the new DISABLED Core task.
Run in normal current-user FullLanguage PowerShell:
  & .\deployment\windows\v0250-w2l-core-failure-evidence.ps1

Queries only fixed Task Scheduler identity/status, the second W2i attempt's
narrow historical Scheduler Operational event window, and the installed Core
component's read-only status/state. Never enables, starts, stops or replaces
a task or an Agent; no HTTP, credentials or raw EventLog messages are output.
#>
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    throw 'W2L requires ordinary user FullLanguage PowerShell.'
}

$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).ProviderPath
$python = Join-Path $env:LOCALAPPDATA 'ServerFS\core-owner-env\Scripts\python.exe'
$probe = Join-Path $repo 'deployment\windows\v0250-w2l-core-readonly-state.py'
$report = Join-Path $repo (
    'docs\diagnostics\v0250-windows-w2l-core-evidence-' +
    [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '.md'
)
$old = @('ServerFS Native Tunnel', 'ServerFS Cloudflare WorkPC')
$new = @(
    'ServerFS MCP Core', 'ServerFS Agent Bridge',
    'ServerFS Cloudflare Connector', 'ServerFS OpenAI Connector'
)
$rows = New-Object System.Collections.Generic.List[string]
$events = New-Object System.Collections.Generic.List[string]
$stage = 'initial'
$done = $false
$eventStatus = 'UNVERIFIED'

try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
        -not (Test-Path -LiteralPath $probe -PathType Leaf)) {
        throw 'An exact read-only W2L probe or interpreter is unavailable.'
    }

    $stage = 'fixed-task-status-inventory'
    $tasks = @(Get-ScheduledTask -ErrorAction Stop)
    foreach ($name in $old) {
        $matches = @($tasks | Where-Object { $_.TaskName -ceq $name -and $_.TaskPath -eq '\' })
        if ($matches.Count -ne 1 -or [string]$matches[0].State -ne 'Running' -or
            -not [bool]$matches[0].Settings.Enabled) {
            throw 'Existing production task is not verified Running/Enabled.'
        }
        $rows.Add('- legacy/' + $name + ': Running/Enabled')
    }
    foreach ($name in $new) {
        $matches = @($tasks | Where-Object { $_.TaskName -ceq $name -and $_.TaskPath -eq '\' })
        if ($matches.Count -ne 1 -or [bool]$matches[0].Settings.Enabled -or
            [string]$matches[0].State -eq 'Running') {
            throw 'Independent Core or peer task is not disabled; refuse diagnosis.'
        }
        $rows.Add('- new/' + $name + ': Disabled/Enabled=False')
    }

    $stage = 'Core-last-scheduler-status'
    $info = Get-ScheduledTaskInfo -TaskName 'ServerFS MCP Core' -TaskPath '\' -ErrorAction Stop
    $result = [int64]$info.LastTaskResult
    $rows.Add('- new-Core-LastTaskResult: 0x' + ('{0:X8}' -f ($result -band 4294967295)))
    $rows.Add('- new-Core-LastRunTime-present: ' + [string]($info.LastRunTime.Year -ge 2000))

    $stage = 'dedicated-Core-state-readonly'
    $prior = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $lines = @(& $python '-I' '-B' $probe 2>&1)
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prior }
    if ($exitCode -ne 0 -or $lines.Count -ne 1) {
        throw 'Installed private Core read-only state probe refused.'
    }
    $data = ConvertFrom-Json -InputObject ([string]$lines[0]) -ErrorAction Stop
    foreach ($name in @(
        'dedicated_env', 'core_registered', 'core_disabled', 'core_offline',
        'core_listener_owner', 'core_last_state_stage',
        'core_state_has_generation_identity',
        'probe_python_process_image_matches_sys_executable',
        'probe_python_venv_launcher_verified',
        'probe_python_base_executable_verified',
        'probe_kernel_image_matches_python_base_executable'
    )) {
        $value = [string]$data.$name
        if ($name -eq 'core_last_state_stage') {
            # The Python probe emits only these three fixed stage labels.
            # Two include hyphens, which the former generic status regex
            # incorrectly rejected before historical events could be read.
            if ($value -cnotin @('disabled-never-started','owner-started','UNVERIFIED')) {
                throw 'Installed Core stage is not one of the fixed safe labels.'
            }
        } elseif ($value -notmatch '^[A-Za-z0-9_]+$') {
            throw 'Installed Core state returned a non-allowlisted field.'
        }
        $rows.Add('- installed-Core/' + $name + ': ' + $value)
    }
    if ($data.core_disabled -ne $true -or $data.core_offline -ne $true) {
        throw 'Installed Core no longer proves disabled/offline.'
    }

    $stage = 'historical-TaskScheduler-Operational-events'
    $windowStart = [DateTime]::Parse('2026-11-12T00:20:00Z').ToUniversalTime()
    $windowEnd = [DateTime]::Parse('2026-11-12T00:43:00Z').ToUniversalTime()
    try {
        $ids = @(100,101,102,103,106,107,110,111,118,129,140,141,142,200,201,202,203,204,322)
        $history = @(Get-WinEvent -FilterHashtable @{
            LogName = 'Microsoft-Windows-TaskScheduler/Operational'
            StartTime = $windowStart
            EndTime = $windowEnd
            Id = $ids
        } -MaxEvents 1200 -ErrorAction Stop)
        $matched = 0
        foreach ($item in $history) {
            # Event XML is used ONLY for exact task-name matching and approved
            # event ResultCode/ErrorCode fields; never record Message, Args,
            # ActionName, raw XML, user identity or any credential-bearing data.
            [xml]$xml = $item.ToXml()
            if ($null -eq $xml.Event.EventData) { continue }
            $fields = @($xml.Event.EventData.Data)
            $owner = @($fields | Where-Object {
                $_.GetAttribute('Name') -in @('TaskName', 'Task', 'TaskPath') -and
                (([string]$_.InnerText).TrimStart('\') -ceq 'ServerFS MCP Core')
            })
            if ($owner.Count -ne 1) { continue }
            $matched++
            $codes = New-Object System.Collections.Generic.List[string]
            foreach ($entry in $fields) {
                if ($entry.GetAttribute('Name') -in @('ResultCode','ErrorCode','ReturnCode','FailureCode')) {
                    $number = [string]$entry.InnerText
                    if ($number -match '^(?:0x[0-9A-Fa-f]{1,8}|-?[0-9]{1,12})$') {
                        $codes.Add($entry.GetAttribute('Name') + '=' + $number)
                    }
                }
            }
            if ($events.Count -lt 40) {
                $events.Add(
                    '- event: ' + $item.TimeCreated.ToUniversalTime().ToString('HH:mm:ssZ') +
                    ', ID=' + [string]$item.Id + ', code=' +
                    $(if ($codes.Count) { $codes -join ',' } else { 'none' })
                )
            }
        }
        $eventStatus = 'QUERY_OK'
        $rows.Add('- relevant-Core-TaskScheduler-event-count: ' + $matched)
        if ($matched -gt $events.Count) {
            $rows.Add('- event-records-truncated: True')
        }
    } catch {
        $eventStatus = 'QUERY_UNAVAILABLE'
        $rows.Add('- TaskScheduler-event-log: UNAVAILABLE (' +
            $_.Exception.GetType().Name + ')')
    }

    $stage = 'final-task-status-inventory'
    $tasks = @(Get-ScheduledTask -ErrorAction Stop)
    foreach ($name in $old) {
        $items = @($tasks | Where-Object { $_.TaskName -ceq $name -and $_.TaskPath -eq '\' })
        if ($items.Count -ne 1 -or [string]$items[0].State -ne 'Running' -or
            -not [bool]$items[0].Settings.Enabled) {
            throw 'A legacy Task Scheduler state changed during read-only diagnosis.'
        }
    }
    foreach ($name in $new) {
        $items = @($tasks | Where-Object { $_.TaskName -ceq $name -and $_.TaskPath -eq '\' })
        if ($items.Count -ne 1 -or [bool]$items[0].Settings.Enabled -or
            [string]$items[0].State -eq 'Running') {
            throw 'A new Task Scheduler state changed during read-only diagnosis.'
        }
    }
    $rows.Add('- all-six-task-statuses-unchanged: PASS')
    $done = $true
} catch {
    $rows.Add('- ' + $stage + ': FAILED (' +
        $_.Exception.GetType().Name + '/' + [string]$_.CategoryInfo.Category + ')')
} finally {
    $doc = New-Object System.Collections.Generic.List[string]
    $doc.Add('# Windows v0.25 W2L — read-only Core startup failure evidence')
    $doc.Add('')
    $doc.Add('UTC: ' + [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))
    $doc.Add('Overall: ' + $(if ($done) { 'OBSERVED' } else { 'INCOMPLETE' }))
    $doc.Add('Last gate: ' + $stage)
    $doc.Add('TaskScheduler Operational: ' + $eventStatus)
    $doc.Add('')
    $doc.Add('## Only fixed-state non-secret observations')
    foreach ($entry in $rows) { $doc.Add($entry) }
    $doc.Add('')
    $doc.Add('## W2i retry event IDs and approved result codes')
    foreach ($entry in $events) { $doc.Add($entry) }
    $doc.Add('')
    $doc.Add('No service start/stop/enable/disable, HTTP, provider,')
    $doc.Add('permission change, credential file read, raw EventLog message,')
    $doc.Add('process ID, private path or command line was collected.')
    $enc = New-Object System.Text.UTF8Encoding($false)
    $stream = [System.IO.File]::Open(
        $report, [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write, [System.IO.FileShare]::None
    )
    try {
        $writer = New-Object System.IO.StreamWriter($stream, $enc)
        try { foreach ($line in $doc) { $writer.WriteLine($line) }; $writer.Flush() }
        finally { $writer.Dispose() }
    } finally { $stream.Dispose() }
    Write-Output ('W2L Core evidence report: ' + $report)
}
if (-not $done) { throw ('W2L read-only Core evidence INCOMPLETE; gate: ' + $stage) }
