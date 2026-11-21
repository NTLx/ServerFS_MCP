#requires -Version 5.1
<#
Windows v0.25.0 W2n native PythonW owner attestation candidate patch.
Only the Product Wheel in the DISABLED/OFFLINE core-owner-env is eligible
for an offline same-version reinstall after W2j accepted baseline proof and a strict
W2j->W2n four-module wheel-diff gate.  No service starts, no task changes.
Do not run this more than once after a partial stage; preserve evidence.

From normal current-user FullLanguage PowerShell:
  & .\deployment\windows\v0250-w2n-update-core-candidate.ps1
#>
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    throw 'W2n Core patch requires ordinary current-user FullLanguage PowerShell.'
}

$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).ProviderPath
$local = Join-Path $env:LOCALAPPDATA 'ServerFS'
$coreHome = Join-Path $local 'core-owner-env'
$corePython = Join-Path $coreHome 'Scripts\python.exe'
$python = Join-Path $repo '.venv\Scripts\python.exe'
$ruff = Join-Path $repo '.venv\Scripts\ruff.exe'
$oldRoot = Join-Path $repo 'scratchpad\w2f-candidate-20261111T0808368098875Z'
$oldWheel = Join-Path $repo 'scratchpad\w2j-core-20261112T0021506765472Z\product\serverfs_mcp-0.24.2-py3-none-any.whl'
$nativeWheel = Join-Path $oldRoot 'native\serverfs_windows_native-0.24.2-cp312-abi3-win_amd64.whl'
$oldProductHash = '3102218157180fa7a70204156bbe9b85ba3b5b7a1d82cc40d214484ae04e007a'
$oldNativeHash = '127a669511bc72e9d19fd9405a2dbeb32d9cfaaecbb791ea3a5060b6c0ab1df3'
$diffChecker = Join-Path $repo 'deployment\windows\v0250-w2n-pythonw-wheel-diff.py'
$generation = Join-Path $repo 'deployment\windows\v0250-w2j-core-generation-proof.py'
$venvBaseGate = Join-Path $repo 'deployment\windows\v0250-w2n-core-venv-home-gate.py'
$installed = Join-Path $repo 'deployment\windows\v0250-w2g-verify-installed.py'
$importGate = Join-Path $repo 'deployment\windows\v0250-w2g-installed-import-gate.py'
$acl = Join-Path $repo 'deployment\windows\v0250-w2g-private-acl.py'
$sourceRoot = $repo
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')
$target = Join-Path $repo ('scratchpad\w2n-pythonw-' + $stamp)
$report = Join-Path $repo ('docs\diagnostics\v0250-windows-w2n-pythonw-update-' + $stamp + '.md')
$oldTasks = @('ServerFS Native Tunnel', 'ServerFS Cloudflare WorkPC')
$newTasks = @(
    'ServerFS MCP Core', 'ServerFS Agent Bridge',
    'ServerFS Cloudflare Connector', 'ServerFS OpenAI Connector'
)
$events = New-Object System.Collections.Generic.List[string]
$stage = 'initial'
$pass = $false
$changeAttempted = $false
$wheelDigest = $null
$previousPytest = [Environment]::GetEnvironmentVariable('PYTEST_DISABLE_PLUGIN_AUTOLOAD','Process')

function Assert-FixedTasks {
    $all = @(Get-ScheduledTask -ErrorAction Stop)
    foreach ($name in $script:oldTasks) {
        $items = @($all | Where-Object {
            $_.TaskName -ceq $name -and $_.TaskPath -eq '\'
        })
        if ($items.Count -ne 1 -or [string]$items[0].State -ne 'Running' -or
            -not [bool]$items[0].Settings.Enabled) {
            throw 'Production Task Scheduler baseline is not Running/Enabled.'
        }
    }
    foreach ($name in $script:newTasks) {
        $items = @($all | Where-Object {
            $_.TaskName -ceq $name -and $_.TaskPath -eq '\'
        })
        if ($items.Count -ne 1 -or [bool]$items[0].Settings.Enabled -or
            [string]$items[0].State -eq 'Running') {
            throw 'The exact independent Core or sibling task is not Disabled.'
        }
    }
}

function Invoke-PrivateGate {
    param([string]$Executable,[string[]]$Parameters)
    $prior = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Executable @Parameters 2>&1 | Out-Null
        $status = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prior
    }
    if ($status -ne 0) {
        throw ('One nonsecret gate failed at ' + $script:stage + '.')
    }
}

function Require-Hash {
    param([string]$Path,[string]$Digest)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'An accepted pinned candidate Wheel is unavailable.'
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path -ErrorAction Stop).Hash.ToLowerInvariant()
    if ($actual -ne $Digest) {
        throw 'An accepted pinned Wheel digest changed.'
    }
}

foreach ($file in @($python,$ruff,$corePython,$oldWheel,$nativeWheel,
        $diffChecker,$generation,$venvBaseGate,$installed,$importGate,$acl)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw 'A fixed W2n source or accepted Wheel prerequisite is missing.'
    }
}
if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
    throw 'The candidate checkout source root is unavailable.'
}
$uv = (Get-Command uv -CommandType Application -ErrorAction Stop).Source
if ((Test-Path -LiteralPath $target) -or (Test-Path -LiteralPath $report)) {
    throw 'W2n destination already exists; refusing overwrite.'
}

Push-Location $repo
try {
    $stage = 'legacy-and-disabled-task-authority-before'
    Assert-FixedTasks
    $events.Add('legacy Running/Enabled, four new Disabled: PASS')

    $stage = 'previous-Core-generation-gone'
    Invoke-PrivateGate $corePython @('-I','-B',$generation)
    $events.Add('Core previous generation stopped and task disabled: PASS')

    $stage = 'accepted-W2j-Product-and-W2f-Native-hashes'
    Require-Hash $oldWheel $oldProductHash
    Require-Hash $nativeWheel $oldNativeHash
    $events.Add('accepted W2j Product and W2f Native immutable wheel hashes: PASS')

    $stage = 'existing-Core-owner-old-wheel-byte-proof'
    Invoke-PrivateGate $python @('-I','-B',$installed,$coreHome,$oldWheel,$nativeWheel)
    Invoke-PrivateGate $corePython @('-I','-B',$importGate,'core-owner-env')
    $events.Add('previous Core owner installed package bytes and imports: PASS')

    $stage = 'source-pythonw-image-native-and-regression-tests'
    [Environment]::SetEnvironmentVariable('PYTEST_DISABLE_PLUGIN_AUTOLOAD','1','Process')
    try {
        Invoke-PrivateGate $python @(
            '-B','-m','pytest','-q','-p','no:cacheprovider','-o','addopts=',
            'tests/test_windows_core_control.py',
            'tests/test_windows_core_listener.py',
            'tests/test_windows_connector_control.py',
            'tests/test_windows_pythonw_process_attestation.py',
            'tests/test_windows_w2n_pythonw_wheel_diff.py'
        )
    } finally {
        [Environment]::SetEnvironmentVariable('PYTEST_DISABLE_PLUGIN_AUTOLOAD',$previousPytest,'Process')
    }
    $sources = @(
        'src/serverfs_mcp/windows_process.py',
        'src/serverfs_mcp/windows_core_control.py',
        'src/serverfs_mcp/windows_core_listener.py',
        'src/serverfs_mcp/windows_connector_control.py',
        'deployment/windows/v0250-w2n-pythonw-wheel-diff.py',
        'deployment/windows/v0250-w2n-core-venv-home-gate.py',
        'tests/test_windows_pythonw_process_attestation.py',
        'tests/test_windows_core_control.py',
        'tests/test_windows_core_listener.py',
        'tests/test_windows_connector_control.py',
        'tests/test_windows_w2n_pythonw_wheel_diff.py'
    )
    Invoke-PrivateGate $ruff (@('check','--no-cache') + $sources)
    Invoke-PrivateGate $ruff (@('format','--check','--no-cache') + $sources)
    $events.Add('synthetic Wheel-diff, PythonW attestation, source lint/format: PASS')

    $stage = 'pre-install-Core-base-pythonw-origin'
    Invoke-PrivateGate $corePython @('-I','-B',$venvBaseGate,'--source-preflight')
    $events.Add('Core pyvenv.cfg/CPython base PythonW verified BEFORE install: PASS')

    $stage = 'rebuild-candidate-only'
    New-Item -ItemType Directory -Path $target -ErrorAction Stop | Out-Null
    $productDir = Join-Path $target 'product'
    New-Item -ItemType Directory -Path $productDir -ErrorAction Stop | Out-Null
    Invoke-PrivateGate $uv @('build','--offline','--wheel','--out-dir',$productDir)
    $wheels = @(Get-ChildItem -LiteralPath $productDir -Filter '*.whl' -File -ErrorAction Stop)
    if ($wheels.Count -ne 1 -or $wheels[0].Name -ne 'serverfs_mcp-0.24.2-py3-none-any.whl') {
        throw 'W2n wheel name/version is ambiguous.'
    }
    $candidate = $wheels[0].FullName
    $wheelDigest = (Get-FileHash -Algorithm SHA256 -LiteralPath $candidate).Hash.ToLowerInvariant()

    $stage = 'exact-four-source-module-wheel-diff'
    Invoke-PrivateGate $python @('-I','-B',$diffChecker,$oldWheel,$candidate,$sourceRoot)
    $events.Add('new Product Wheel differs only in four attestation sources and RECORD: PASS')

    $stage = 'pre-install-disabled-Core-double-check'
    Assert-FixedTasks
    Invoke-PrivateGate $corePython @('-I','-B',$generation)
    # This is the sole mutation point. Keep the W2j accepted candidate immutable.
    # uv --no-deps + --reinstall updates ONLY serverfs-mcp in the one Core
    # venv; it does not touch Native, Bridge, or any new/legacy Task Scheduler.
    $changeAttempted = $true
    $stage = 'offline-reinstall-Core-owner-Product-only'
    $previousLink = [Environment]::GetEnvironmentVariable('UV_LINK_MODE','Process')
    [Environment]::SetEnvironmentVariable('UV_LINK_MODE','copy','Process')
    try {
        Invoke-PrivateGate $uv @(
            'pip','install','--offline','--python',$corePython,
            '--no-deps','--reinstall','--link-mode','copy',$candidate
        )
    } finally {
        [Environment]::SetEnvironmentVariable('UV_LINK_MODE',$previousLink,'Process')
    }
    $events.Add('core-owner-env only Product wheel reinstall: completed')

    $stage = 'Core-package-private-DACL-restore'
    $packageDir = Join-Path $coreHome 'Lib\site-packages\serverfs_mcp'
    Invoke-PrivateGate $python @('-I','-B',$acl,$packageDir,'dir')
    $events.Add('Core package private protected DACL: PASS')

    $stage = 'new-Core-wheel-byte-import-and-owner-generation'
    Invoke-PrivateGate $python @('-I','-B',$installed,$coreHome,$candidate,$nativeWheel)
    Invoke-PrivateGate $corePython @('-I','-B',$importGate,'core-owner-env')
    Invoke-PrivateGate $corePython @('-I','-B',$venvBaseGate)
    Invoke-PrivateGate $corePython @('-I','-B',$generation)
    $events.Add('new Core bytes/import, venv base PythonW and generation: PASS')

    $stage = 'tasks-after-and-old-W2f-digests'
    Assert-FixedTasks
    Require-Hash $oldWheel $oldProductHash
    Require-Hash $nativeWheel $oldNativeHash
    $events.Add('accepted W2j Product/W2f Native, legacy tasks and siblings unchanged: PASS')
    $pass = $true
} catch {
    $events.Add($stage + ': FAILED (' + $_.Exception.GetType().Name +
        '/' + [string]$_.CategoryInfo.Category + ')')
} finally {
    [Environment]::SetEnvironmentVariable('PYTEST_DISABLE_PLUGIN_AUTOLOAD',$previousPytest,'Process')
    try {
        Assert-FixedTasks
        $events.Add('final Task Scheduler baseline: PASS')
    } catch {
        $events.Add('final Task Scheduler baseline: NOT_VERIFIED')
        $pass = $false
    }
    Pop-Location
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('# Windows v0.25.0 W2n — PythonW attestation Core-only offline correction')
    $lines.Add('')
    $lines.Add('UTC: ' + [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))
    $lines.Add('Overall: ' + $(if ($pass) {'PASS'} else {'NOT_ACCEPTED'}))
    $lines.Add('Last stage: ' + $stage)
    $lines.Add('Core owner package mutation attempted: ' + [string]$changeAttempted)
    if ($null -ne $wheelDigest) {
        $lines.Add('W2n new Product candidate SHA256: ' + $wheelDigest)
        $lines.Add('W2n scratchpad directory: ' + (Split-Path -Leaf $target))
    }
    $lines.Add('')
    $lines.Add('## Credential-free gates')
    foreach ($event in $events) { $lines.Add('- ' + $event) }
    $lines.Add('')
    $lines.Add('Only the independent disabled core-owner-env Product Wheel was')
    $lines.Add('eligible for offline replacement; no Task Scheduler mutation')
    $lines.Add('or component activation was requested. No secrets or private')
    $lines.Add('user paths logged. A partial Core Wheel install requires')
    $lines.Add('separate diagnosis; do not rerun blindly or delete the env.')
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $stream = [System.IO.File]::Open($report,
        [System.IO.FileMode]::CreateNew,[System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None)
    try {
        $writer = New-Object System.IO.StreamWriter($stream,$encoding)
        try {
            foreach ($line in $lines) { $writer.WriteLine($line) }
            $writer.Flush()
        } finally { $writer.Dispose() }
    } finally { $stream.Dispose() }
    Write-Output ('W2n Core candidate update report: ' + $report)
}
if (-not $pass) { throw ('W2n Core patch NOT_ACCEPTED; gate: ' + $stage) }
