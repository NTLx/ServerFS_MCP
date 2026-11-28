#requires -Version 5.1
<#
W2h disabled-only Windows independent component staging orchestrator.
Must run from a normal NON-ADMIN FullLanguage Windows PowerShell 5.1 session:
    & .\deployment\windows\v0250-w2h-stage-disabled.ps1

Calls read-only W2h authority/input gate first. Only on PASS will it
provision private service files and register four NEW disabled-only Tasks
via the components' installed, independent Python environments.
Never calls service start/stop/restart, never alters legacy tasks.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    throw 'W2h disabled staging requires FullLanguage normal current-user PowerShell.'
}
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).ProviderPath
$preflight = Join-Path $repo 'deployment\windows\v0250-w2h-host-preflight.ps1'
$installer = Join-Path $repo 'deployment\windows\v0250-w2h-stage-disabled.py'
$python = Join-Path $repo '.venv\Scripts\python.exe'
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')
$report = Join-Path $repo ('docs\diagnostics\v0250-windows-w2h-operator-' + $stamp + '.md')
$oldNames = @('ServerFS Native Tunnel', 'ServerFS Cloudflare WorkPC')
$newNames = @(
    'ServerFS MCP Core', 'ServerFS Agent Bridge',
    'ServerFS Cloudflare Connector', 'ServerFS OpenAI Connector'
)
$events = New-Object System.Collections.Generic.List[string]
$stage = 'initial'
$passed = $false
$preflightPassed = $false
$installerPassed = $false

if (-not (Test-Path -LiteralPath $preflight -PathType Leaf) -or
    -not (Test-Path -LiteralPath $installer -PathType Leaf) -or
    -not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'W2h installer/preflight or source interpreter is not available.'
}
Push-Location $repo
try {
    $stage = 'preflight-readonly'
    # Child PowerShell script writes only one new redacted report and fails
    # closed on any stale/conflicting source input or Task Scheduler state.
    & $preflight | Out-Null
    $preflightPassed = $true
    $events.Add('- W2h authority/input preflight: PASS')

    # Recheck the exact checked-out source before any LocalAppData
    # mutation. These checks create no service state; pytest only uses
    # synthetic values and does not run external provider processes.
    $stage = 'W2h-source-native-regression'
    $ruff = Join-Path $repo '.venv\Scripts\ruff.exe'
    if (-not (Test-Path -LiteralPath $ruff -PathType Leaf)) {
        throw 'The W2h source lint/format verifier is unavailable.'
    }
    $targets = @(
        'deployment/windows/v0250-w2h-input-audit.py',
        'deployment/windows/v0250-w2h-stage-disabled.py',
        'tests/test_windows_w2h_disabled_staging_contract.py'
    )
    $previousAutoload = [Environment]::GetEnvironmentVariable(
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'Process'
    )
    $preference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        [Environment]::SetEnvironmentVariable(
            'PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1', 'Process'
        )
        & $python '-B' '-m' 'pytest' '-q' '-p' 'no:cacheprovider' '-o' 'addopts=' 'tests/test_windows_w2h_disabled_staging_contract.py' 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'W2h source contract pytest failed.' }
        & $ruff 'check' '--no-cache' @targets 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'W2h source Ruff lint failed.' }
        & $ruff 'format' '--check' '--no-cache' @targets 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'W2h source Ruff format failed.' }
    } finally {
        [Environment]::SetEnvironmentVariable(
            'PYTEST_DISABLE_PLUGIN_AUTOLOAD', $previousAutoload, 'Process'
        )
        $ErrorActionPreference = $preference
    }
    $events.Add('- W2h source native regression: PASS (pytest, Ruff lint, Ruff format)')

    $stage = 'disabled-only-private-staging'
    $preference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $python '-I' '-B' $installer 2>&1 | Out-Null
        $exitStatus = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $preference
    }
    if ($exitStatus -ne 0) {
        throw 'The W2h isolated Python stage refused or preserved a partial install.'
    }
    $installerPassed = $true
    $events.Add('- W2h four-component private staging: reported PASS')

    $stage = 'postregistration-authoritative-task-check'
    $all = @(Get-ScheduledTask -ErrorAction Stop)
    foreach ($name in $oldNames) {
        $match = @($all | Where-Object { $_.TaskName -eq $name })
        if ($match.Count -ne 1 -or [string]$match[0].State -ne 'Running' -or
            -not [bool]$match[0].Settings.Enabled) {
            throw 'Legacy service is not proven Running/Enabled after staging.'
        }
    }
    foreach ($name in $newNames) {
        $match = @($all | Where-Object { $_.TaskName -eq $name })
        if ($match.Count -ne 1 -or [bool]$match[0].Settings.Enabled -or
            [string]$match[0].State -eq 'Running') {
            throw 'Independent component Task Scheduler is not verified disabled.'
        }
    }
    $events.Add('- legacy-tasks-after: PASS (both Running/Enabled)')
    $events.Add('- new-component-tasks-after: PASS (four registered, disabled, not running)')
    $passed = $true
} catch {
    $events.Add(
        '- ' + $stage + ': FAILED (' + $_.Exception.GetType().Name + '/' +
        [string]$_.CategoryInfo.Category + ')'
    )
} finally {
    # Never hide a partial registration: summarize every task name as an
    # independent last-observed OS entry. No XML, argv, SID or paths.
    try {
        $all = @(Get-ScheduledTask -ErrorAction Stop)
        foreach ($name in @($oldNames) + @($newNames)) {
            $matches = @($all | Where-Object { $_.TaskName -eq $name })
            if ($matches.Count -eq 1) {
                $task = $matches[0]
                $events.Add(
                    '- os-task/' + $name + ': ' + [string]$task.State +
                    '/enabled=' + [string][bool]$task.Settings.Enabled
                )
            } elseif ($matches.Count -eq 0) {
                $events.Add('- os-task/' + $name + ': ABSENT_VERIFIED')
            } else {
                $events.Add('- os-task/' + $name + ': AMBIGUOUS_MULTIPLE')
            }
        }
    } catch {
        $events.Add('- os-task-inventory: UNVERIFIED')
        $passed = $false
    }
    Pop-Location
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('# Windows v0.25.0 W2h — disabled-only OS staging')
    $lines.Add('')
    $lines.Add('UTC: ' + [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))
    $lines.Add('Overall: ' + $(if ($passed) { 'PASS' } else { 'NOT_ACCEPTED' }))
    $lines.Add('Last stage: ' + $stage)
    $lines.Add('Read-only preflight completed: ' + [string]$preflightPassed)
    $lines.Add('Private component stage completed: ' + [string]$installerPassed)
    $lines.Add('')
    $lines.Add('## Host-verifiable non-secret outcomes')
    foreach ($row in $events) { $lines.Add($row) }
    $lines.Add('')
    $lines.Add('Only disabled new Tasks were eligible for registration. No')
    $lines.Add('new Core/Bridge/Connector service activation was requested.')
    $lines.Add('Legacy tasks, workdirs, providers and existing credentials')
    $lines.Add('were never targeted by the installer.')
    $lines.Add('A partial failure is preserved without broad cleanup.')
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $stream = [System.IO.File]::Open(
        $report, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $writer = New-Object System.IO.StreamWriter($stream, $encoding)
        try {
            foreach ($line in $lines) { $writer.WriteLine($line) }
            $writer.Flush()
        } finally {
            $writer.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
    Write-Output ('W2h host stage report: ' + $report)
}
if (-not $passed) {
    throw ('W2h disabled staging NOT ACCEPTED; last stage: ' + $stage)
}
