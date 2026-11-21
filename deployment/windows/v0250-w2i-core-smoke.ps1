#requires -Version 5.1
<#
v0.25.0 Windows isolated Core lifecycle smoke, not a production cutover.
One normal, NON-ADMIN FullLanguage PowerShell command:
    & .\deployment\windows\v0250-w2i-core-smoke.ps1

Only the exact independent MCP Core Scheduled Task is eligible to start
and stop; old OpenAI/Cloudflare and all new Bridge/connector tasks are never
started, stopped, enabled or edited by this script. No credentials shown.
#>
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    throw 'W2i requires normal current-user FullLanguage PowerShell.'
}
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).ProviderPath
$python = Join-Path $repo '.venv\Scripts\python.exe'
$ruff = Join-Path $repo '.venv\Scripts\ruff.exe'
$source = Join-Path $repo 'deployment\windows\v0250-w2i-core-smoke.py'
$traceSource = Join-Path $repo 'deployment\windows\v0250-w2p-core-lifecycle-trace.py'
$wheelProof = Join-Path $repo 'deployment\windows\v0250-w2g-verify-installed.py'
$generationProof = Join-Path $repo 'deployment\windows\v0250-w2j-core-generation-proof.py'
$baseProof = Join-Path $repo 'deployment\windows\v0250-w2n-core-venv-home-gate.py'
$coreHome = Join-Path $env:LOCALAPPDATA 'ServerFS\core-owner-env'
$corePython = Join-Path $coreHome 'Scripts\python.exe'
$w2nWheel = Join-Path $repo 'scratchpad\w2n-pythonw-20261112T0402231886660Z\product\serverfs_mcp-0.24.2-py3-none-any.whl'
$nativeWheel = Join-Path $repo 'scratchpad\w2f-candidate-20261111T0808368098875Z\native\serverfs_windows_native-0.24.2-cp312-abi3-win_amd64.whl'
$expectedProductSha256 = '919ec7023a8920b6ef488653d7f17b7e63896c752b41e5b00565c7b6322112b1'
$expectedNativeSha256 = '127a669511bc72e9d19fd9405a2dbeb32d9cfaaecbb791ea3a5060b6c0ab1df3'
$report = Join-Path $repo (
    'docs\diagnostics\v0250-windows-w2i-core-host-' +
    [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '.md'
)
if (-not (Test-Path -LiteralPath $source -PathType Leaf) -or
    -not (Test-Path -LiteralPath $traceSource -PathType Leaf) -or
    -not (Test-Path -LiteralPath $python -PathType Leaf) -or
    -not (Test-Path -LiteralPath $ruff -PathType Leaf) -or
    -not (Test-Path -LiteralPath $wheelProof -PathType Leaf) -or
    -not (Test-Path -LiteralPath $generationProof -PathType Leaf) -or
    -not (Test-Path -LiteralPath $baseProof -PathType Leaf) -or
    -not (Test-Path -LiteralPath $corePython -PathType Leaf) -or
    -not (Test-Path -LiteralPath $w2nWheel -PathType Leaf) -or
    -not (Test-Path -LiteralPath $nativeWheel -PathType Leaf)) {
    throw 'W2i source, interpreter or lint tool is unavailable.'
}
$legacy = @('ServerFS Native Tunnel', 'ServerFS Cloudflare WorkPC')
$core = 'ServerFS MCP Core'
$other = @(
    'ServerFS Agent Bridge',
    'ServerFS Cloudflare Connector',
    'ServerFS OpenAI Connector'
)
$rows = New-Object System.Collections.Generic.List[string]
$pass = $false
$stage = 'initial'
$probeCompleted = $false
$coreActivationAttempted = $false
Push-Location $repo
try {
    $stage = 'preflight-task-authority'
    $tasks = @(Get-ScheduledTask -ErrorAction Stop)
    foreach ($taskName in $legacy) {
        $found = @($tasks | Where-Object {
            $_.TaskName -ceq $taskName -and $_.TaskPath -eq '\'
        })
        if ($found.Count -ne 1 -or [string]$found[0].State -ne 'Running' -or
            -not [bool]$found[0].Settings.Enabled) {
            throw 'Legacy service is not Running/Enabled; aborting isolated Core test.'
        }
    }
    foreach ($taskName in @($core) + @($other)) {
        $found = @($tasks | Where-Object {
            $_.TaskName -ceq $taskName -and $_.TaskPath -eq '\'
        })
        if ($found.Count -ne 1 -or [bool]$found[0].Settings.Enabled -or
            [string]$found[0].State -eq 'Running') {
            throw 'One new component is not registered/disabled; refusing Core test.'
        }
    }
    $rows.Add('- preflight: legacy Running/Enabled, four new disabled: PASS')

    $stage = 'W2n-exact-Core-installed-Wheel-proof'
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $w2nWheel).Hash.ToLowerInvariant() -ne
        $expectedProductSha256 -or
        (Get-FileHash -Algorithm SHA256 -LiteralPath $nativeWheel).Hash.ToLowerInvariant() -ne
        $expectedNativeSha256) {
        throw 'W2n accepted Core Product or original W2f Native Wheel hash differs.'
    }
    $previousPref = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $python '-I' '-B' $wheelProof $coreHome $w2nWheel $nativeWheel 2>&1 | Out-Null
        $wheelExit = $LASTEXITCODE
        if ($wheelExit -ne 0) {
            throw 'Installed Core Product/Native content differs from accepted candidate.'
        }
        & $corePython '-I' '-B' $baseProof 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Installed Core interpreter and base PythonW provenance differ.'
        }
        & $corePython '-I' '-B' $generationProof 2>&1 | Out-Null
        $genExit = $LASTEXITCODE
        if ($genExit -ne 0) {
            throw 'New Core disabled/offline generation cannot be proved.'
        }
    } finally {
        $ErrorActionPreference = $previousPref
    }
    $rows.Add('- installed W2n Product/W2f Native, base PythonW and Core generation: PASS')

    $stage = 'source-regressions'
    $previousPref = $ErrorActionPreference
    $autoload = [Environment]::GetEnvironmentVariable(
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'Process'
    )
    try {
        $ErrorActionPreference = 'Continue'
        [Environment]::SetEnvironmentVariable(
            'PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1', 'Process'
        )
        & $python '-B' '-m' 'pytest' '-q' '-p' 'no:cacheprovider' '-o' 'addopts=' 'tests/test_windows_w2i_core_smoke_contract.py' 'tests/test_windows_w2p_core_lifecycle_trace.py' 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'W2i source tests failed.' }
        & $ruff 'check' '--no-cache' 'deployment/windows/v0250-w2i-core-smoke.py' 'deployment/windows/v0250-w2p-core-lifecycle-trace.py' 'tests/test_windows_w2i_core_smoke_contract.py' 'tests/test_windows_w2p_core_lifecycle_trace.py' 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'W2i source Ruff lint failed.' }
        & $ruff 'format' '--check' '--no-cache' 'deployment/windows/v0250-w2i-core-smoke.py' 'deployment/windows/v0250-w2p-core-lifecycle-trace.py' 'tests/test_windows_w2i_core_smoke_contract.py' 'tests/test_windows_w2p_core_lifecycle_trace.py' 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'W2i source Ruff format failed.' }
    } finally {
        [Environment]::SetEnvironmentVariable(
            'PYTEST_DISABLE_PLUGIN_AUTOLOAD', $autoload, 'Process'
        )
        $ErrorActionPreference = $previousPref
    }
    $rows.Add('- W2i source regression: PASS')

    $stage = 'isolated-Core-only-start-probe-stop'
    $coreActivationAttempted = $true
    $previousPref = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $python '-I' '-B' $source 2>&1 | Out-Null
        $result = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPref
    }
    if ($result -ne 0) {
        throw 'Isolated Core-only activation/probe/stop did not pass.'
    }
    $probeCompleted = $true
    $rows.Add('- isolated Core own-listener/401/stop: PASS')

    $stage = 'postflight-Task-Scheduler'
    $tasks = @(Get-ScheduledTask -ErrorAction Stop)
    foreach ($taskName in $legacy) {
        $found = @($tasks | Where-Object {
            $_.TaskName -ceq $taskName -and $_.TaskPath -eq '\'
        })
        if ($found.Count -ne 1 -or [string]$found[0].State -ne 'Running' -or
            -not [bool]$found[0].Settings.Enabled) {
            throw 'Legacy service state changed during Core acceptance.'
        }
    }
    foreach ($taskName in @($core) + @($other)) {
        $found = @($tasks | Where-Object {
            $_.TaskName -ceq $taskName -and $_.TaskPath -eq '\'
        })
        if ($found.Count -ne 1 -or [bool]$found[0].Settings.Enabled -or
            [string]$found[0].State -eq 'Running') {
            throw 'New component is not confirmed disabled after Core acceptance.'
        }
    }
    $rows.Add('- postflight: legacy Running/Enabled, four new disabled: PASS')
    $pass = $true
} catch {
    $rows.Add(
        '- ' + $stage + ': FAILED (' + $_.Exception.GetType().Name + '/' +
        [string]$_.CategoryInfo.Category + ')'
    )
} finally {
    if ($coreActivationAttempted) {
        # Emergency fail-safe for this exact new task only. A failure in
        # Python's owned Core.stop() may otherwise leave Scheduler Enabled,
        # allowing the logon/repetition triggers to start a second Core.
        # Disabling the known task never kills a process or touches a legacy
        # owner; if the new Core is still Running, report that separately.
        try {
            $exclusive = Get-ScheduledTask -TaskName $core -TaskPath '\' -ErrorAction Stop
            if ($null -eq $exclusive -or $exclusive.TaskName -ne $core -or
                $exclusive.TaskPath -ne '\') {
                throw 'The exact Core Task Scheduler identity could not be confirmed.'
            }
            if ([bool]$exclusive.Settings.Enabled) {
                Disable-ScheduledTask -TaskName $core -TaskPath '\' -ErrorAction Stop | Out-Null
                $rows.Add('- Core fail-safe: disabled exact new Task Scheduler entry')
            } else {
                $rows.Add('- Core fail-safe: exact new task already Disabled')
            }
            $exclusive = Get-ScheduledTask -TaskName $core -TaskPath '\' -ErrorAction Stop
            if ([bool]$exclusive.Settings.Enabled) {
                throw 'New Core remained Enabled after fail-safe.'
            }
            if ([string]$exclusive.State -eq 'Running') {
                $rows.Add('- Core fail-safe: WARNING Core task remains Running')
                $pass = $false
            }
        } catch {
            $rows.Add('- Core fail-safe: FAILED (' +
                $_.Exception.GetType().Name + '/' +
                [string]$_.CategoryInfo.Category + ')')
            $pass = $false
        }
    }
    try {
        $tasks = @(Get-ScheduledTask -ErrorAction Stop)
        foreach ($taskName in @($legacy) + @($core) + @($other)) {
            $found = @($tasks | Where-Object {
            $_.TaskName -ceq $taskName -and $_.TaskPath -eq '\'
        })
            if ($found.Count -eq 1) {
                $rows.Add(
                    '- os-task/' + $taskName + ': ' + [string]$found[0].State +
                    '/enabled=' + [string][bool]$found[0].Settings.Enabled
                )
            } elseif ($found.Count -eq 0) {
                $rows.Add('- os-task/' + $taskName + ': ABSENT_VERIFIED')
                $pass = $false
            } else {
                $rows.Add('- os-task/' + $taskName + ': QUERY_AMBIGUOUS')
                $pass = $false
            }
        }
    } catch {
        $rows.Add('- postflight task inventory: UNVERIFIED')
        $pass = $false
    }
    Pop-Location
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('# Windows v0.25.0 W2i — isolated Core acceptance')
    $lines.Add('')
    $lines.Add('UTC: ' + [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))
    $lines.Add('Overall: ' + $(if ($pass) { 'PASS' } else { 'NOT_ACCEPTED' }))
    $lines.Add('Last stage: ' + $stage)
    $lines.Add('Core smoke completed: ' + [string]$probeCompleted)
    $lines.Add('')
    $lines.Add('## Non-secret task and lifecycle status')
    foreach ($row in $rows) { $lines.Add($row) }
    $lines.Add('')
    $lines.Add('Only the fixed independent Core lifecycle was eligible for')
    $lines.Add('mutation. Bridge, connectors and legacy tasks were never')
    $lines.Add('targeted. No user path, secret, process command line,')
    $lines.Add('HTTP response body or agent workdir content was logged.')
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
    Write-Output ('W2i Core host report: ' + $report)
}
if (-not $pass) { throw ('W2i isolated Core test NOT ACCEPTED; gate: ' + $stage) }
