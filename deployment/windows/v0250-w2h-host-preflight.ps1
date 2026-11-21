#requires -Version 5.1
<#
Windows v0.25.0 W2h authority/input inventory; read-only except one
create-new redacted Markdown diagnostics report inside this repository.
Run as the ordinary user (not Administrator):
    & .\deployment\windows\v0250-w2h-host-preflight.ps1

Never installs/registers/enables/stops tasks, reads secrets, edits service
files, or attempts any ACL change. This is a hard W2h prerequisite.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    throw 'W2h preflight requires normal current-user FullLanguage PowerShell.'
}
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).ProviderPath
$python = Join-Path $repo '.venv\Scripts\python.exe'
$audit = Join-Path $repo 'deployment\windows\v0250-w2h-input-audit.py'
$folder = Join-Path $repo 'docs\diagnostics'
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
    -not (Test-Path -LiteralPath $audit -PathType Leaf) -or
    -not (Test-Path -LiteralPath $folder -PathType Container)) {
    throw 'W2h source interpreter, input audit or diagnostics location is unavailable.'
}

$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')
$report = Join-Path $folder ('v0250-windows-w2h-preflight-' + $stamp + '.md')
$rows = New-Object System.Collections.Generic.List[string]
$passed = $false
$stage = 'start'
$oldNames = @('ServerFS Native Tunnel', 'ServerFS Cloudflare WorkPC')
$newNames = @(
    'ServerFS MCP Core', 'ServerFS Agent Bridge',
    'ServerFS Cloudflare Connector', 'ServerFS OpenAI Connector'
)

Push-Location $repo
try {
    $stage = 'fixed-Task-Scheduler-inventory'
    $all = @(Get-ScheduledTask -ErrorAction Stop)
    foreach ($name in $oldNames) {
        $selected = @($all | Where-Object { $_.TaskName -eq $name })
        if ($selected.Count -ne 1 -or [string]$selected[0].State -ne 'Running' -or
            -not [bool]$selected[0].Settings.Enabled) {
            throw 'A legacy fixed task is not verified Running/Enabled.'
        }
        $rows.Add('- legacy-' + $name + ': Running/Enabled')
    }
    foreach ($name in $newNames) {
        $selected = @($all | Where-Object { $_.TaskName -eq $name })
        if ($selected.Count -ne 0) {
            throw 'A new component Task Scheduler name is already registered.'
        }
        $rows.Add('- candidate-' + $name + ': ABSENT_VERIFIED')
    }
    $stage = 'private-input-metadata-audit'
    $oldPreference = $ErrorActionPreference
    try {
        # Python 3.13 -I/-B file-backed argv avoids Windows PowerShell 5.1
        # inline -c quoting and NativeCommandError preference pitfalls.
        $ErrorActionPreference = 'Continue'
        $lines = @(& $python '-I' '-B' $audit 2>&1)
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldPreference
    }
    if ($code -ne 0 -or $lines.Count -ne 1) {
        throw 'The independent read-only input audit did not return one safe JSON object.'
    }
    $doc = ConvertFrom-Json -InputObject ([string]$lines[0]) -ErrorAction Stop
    $properties = @(
        'legacy_default_manifest',
        'legacy_cloudflare_manifest',
        'config_same_in_legacy_instances',
        'native_config',
        'core_port_18766_free',
        'core_public_hostname_match',
        'legacy_cloudflare_binary',
        'legacy_cloudflare_token_metadata',
        'legacy_default_key_metadata',
        'legacy_origin_auth_metadata',
        'legacy_default_env_placement'
    )
    foreach ($key in $properties) {
        $value = [string]$doc.$key
        if ($value -notmatch '^[A-Z0-9_]+$') {
            throw 'Input audit returned a non-status field.'
        }
        $rows.Add('- ' + $key + ': ' + $value)
    }
    foreach ($sectionName in @('candidate_envs', 'new_service_homes')) {
        $section = $doc.$sectionName
        foreach ($prop in $section.PSObject.Properties) {
            if ($prop.Name -notmatch '^[a-z_]+$' -or
                [string]$prop.Value -notmatch '^[A-Z0-9_]+$') {
                throw 'Input audit returned an unexpected status entry.'
            }
            $rows.Add('- ' + $sectionName + '/' + $prop.Name + ': ' + [string]$prop.Value)
        }
    }
    $stage = 'input-audit-evaluation'
    $required = @(
        ([string]$doc.legacy_default_manifest -eq 'VALID'),
        ([string]$doc.legacy_cloudflare_manifest -eq 'VALID'),
        ([string]$doc.config_same_in_legacy_instances -eq 'PASS'),
        ([string]$doc.native_config -eq 'CONFIG_WITH_WORKDIRS_AND_BRIDGE'),
        ([string]$doc.core_port_18766_free -eq 'PASS'),
        ([string]$doc.core_public_hostname_match -eq 'PASS'),
        ([string]$doc.legacy_cloudflare_binary -eq 'BINARY_SHA256_PINNED'),
        ([string]$doc.legacy_cloudflare_token_metadata -eq 'CURRENT_USER_PRIVATE'),
        ([string]$doc.legacy_origin_auth_metadata -eq 'CURRENT_USER_PRIVATE')
    )
    foreach ($prop in $doc.candidate_envs.PSObject.Properties) {
        $required += ([string]$prop.Value -eq 'PRIVATE_PYTHON_OK')
    }
    foreach ($prop in $doc.new_service_homes.PSObject.Properties) {
        $required += ([string]$prop.Value -eq 'ABSENT')
    }
    # The legacy default API key may have inherited ACL and must NEVER be
    # passed directly to a new connector. A protected copy is a separate
    # later W2h action after an explicitly reviewed source rule.
    if (@($required | Where-Object { -not $_ }).Count -eq 0) {
        $passed = $true
        $rows.Add('- future-new-OpenAI-control-plane-key: PROTECTED_COPY_REQUIRED')
        $rows.Add('- W2h authority/input inventory: PASS (no installation performed)')
    } else {
        $rows.Add('- W2h authority/input inventory: NOT_ACCEPTED')
    }
} catch {
    $rows.Add('- ' + $stage + ': FAILED (' +
        $_.Exception.GetType().Name + '/' + [string]$_.CategoryInfo.Category + ')')
} finally {
    Pop-Location
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('# Windows v0.25.0 W2h — read-only host authority/input inventory')
    $lines.Add('')
    $lines.Add('UTC: ' + [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))
    $lines.Add('Result: ' + $(if ($passed) { 'PASS' } else { 'NOT_ACCEPTED' }))
    $lines.Add('Last gate: ' + $stage)
    $lines.Add('')
    $lines.Add('## Fixed names, nonsecret metadata and prerequisite gates')
    foreach ($entry in $rows) { $lines.Add($entry) }
    $lines.Add('')
    $lines.Add('No credential file bytes, token IDs, SID, user paths, argv, task XML,')
    $lines.Add('private settings or environment values were written to this report.')
    $lines.Add('No legacy or new Task Scheduler mutation, service operation,')
    $lines.Add('permission change, virtualenv mutation, or HTTP request performed.')
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $handle = [System.IO.File]::Open(
        $report, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None
    )
    try {
        $writer = New-Object System.IO.StreamWriter($handle, $encoding)
        try {
            foreach ($line in $lines) { $writer.WriteLine($line) }
            $writer.Flush()
        } finally {
            $writer.Dispose()
        }
    } finally {
        $handle.Dispose()
    }
    Write-Output ('W2h host preflight report: ' + $report)
}
if (-not $passed) { throw ('W2h preflight NOT ACCEPTED; last gate: ' + $stage) }
