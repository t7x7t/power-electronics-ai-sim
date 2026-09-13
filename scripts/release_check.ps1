[CmdletBinding()]
param(
    [string]$EvidenceDirectory,
    [ValidateRange(1, 65535)]
    [int]$ServicePort = 8765,
    [string]$FormalDemoRunsRoot,
    [switch]$PreflightOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Exact values from docs/28_release_toolchain_policy_v0.md. Compatibility
# ranges remain in project metadata; release evidence needs this narrower set.
$ExpectedToolchain = [ordered]@{
    python = "3.12.7"
    pytest = "7.4.4"
    jsonschema = "4.23.0"
    numpy = "1.26.4"
    node = "v24.15.0"
    npm = "11.12.1"
}

$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$commit = (& git -C $RepositoryRoot rev-parse HEAD 2>$null).Trim()
if ($commit -notmatch "^[0-9a-f]{40}$") { $commit = "unknown" }
if (-not $EvidenceDirectory) {
    $suffix = if ($commit -eq "unknown") { "unknown" } else { $commit.Substring(0, 12) }
    $EvidenceDirectory = Join-Path $RepositoryRoot ".tmp\release-evidence\$timestamp-$suffix"
}
$EvidenceDirectory = [System.IO.Path]::GetFullPath($EvidenceDirectory)
if ((Test-Path -LiteralPath $EvidenceDirectory) -and (Get-ChildItem -LiteralPath $EvidenceDirectory -Force | Select-Object -First 1)) {
    throw "evidence directory must not already contain files: $EvidenceDirectory"
}
New-Item -ItemType Directory -Force -Path $EvidenceDirectory | Out-Null

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$records = New-Object System.Collections.Generic.List[object]
$inputDigests = New-Object System.Collections.Generic.List[object]
$formalDemoEvidence = @()
$serviceProcess = $null
$hasFailure = $false
$executionMode = if ($PreflightOnly) { "preflight_only" } else { "full_local_gate" }

function Format-Command {
    param([string]$FilePath, [string[]]$Arguments)
    $parts = @($FilePath) + @($Arguments | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\\"') + '"' } else { $_ }
    })
    return $parts -join " "
}

function Add-Record {
    param(
        [string]$Gate,
        [ValidateSet("pass", "fail", "not_run")][string]$Status,
        [int]$ExitCode = 0,
        [string]$Detail = "",
        [string]$Command = "",
        [string]$Log = ""
    )
    $entry = [ordered]@{
        gate = $Gate
        status = $Status
        exit_code = $ExitCode
        detail = $Detail
        command = $Command
        log = $Log
        log_sha256 = $null
    }
    $script:records.Add($entry)
    if ($Status -eq "fail") { $script:hasFailure = $true }
    return $entry
}

function Add-NotRunRecord {
    param([string]$Gate, [string]$Reason)
    Add-Record $Gate "not_run" 0 $Reason "" "" | Out-Null
}

function Invoke-NativeGate {
    param([string]$Gate, [string]$FilePath, [string[]]$Arguments, [string]$WorkingDirectory = $RepositoryRoot)
    $log = Join-Path $EvidenceDirectory ("{0}.log" -f $Gate)
    $command = Format-Command $FilePath $Arguments
    $previous = Get-Location
    try {
        Set-Location -LiteralPath $WorkingDirectory
        & $FilePath @Arguments *>&1 | Tee-Object -FilePath $log | Out-Host
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
        if ($code -eq 0) {
            Add-Record $Gate "pass" $code "" $command $log | Out-Null
            return $true
        }
        Add-Record $Gate "fail" $code "command returned a non-zero exit code" $command $log | Out-Null
        return $false
    } catch {
        [System.IO.File]::WriteAllText($log, ($_ | Out-String), $Utf8NoBom)
        Add-Record $Gate "fail" 1 $_.Exception.Message $command $log | Out-Null
        return $false
    } finally {
        Set-Location -LiteralPath $previous
    }
}

function Add-InputDigest {
    param([string]$RelativePath)
    $path = Join-Path $RepositoryRoot $RelativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Add-Record "source-inputs" "fail" 1 "required release input is missing: $RelativePath" "" "" | Out-Null
        return
    }
    $inputDigests.Add([ordered]@{
        path = $RelativePath.Replace('\', '/')
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
    })
}

function Record-InputDigests {
    foreach ($relative in @(
        "pyproject.toml",
        "requirements-tested.txt",
        ".github/workflows/ci.yml",
        "workbench/package.json",
        "workbench/package-lock.json",
        "docs/27_minimum_local_release_scope_v0.md",
        "docs/28_release_toolchain_policy_v0.md",
        "docs/29_local_release_check_v0.md",
        "docs/30_stage11_browser_e2e_v0.md",
        "scripts/release_check.ps1",
        "scripts/generate_formal_demos.py",
        "src/pe_sim/default_contracts.py",
        "examples/buck_pi_step/config.json",
        "examples/boost_pi_step/config.json"
    )) {
        Add-InputDigest $relative
    }
    Get-ChildItem -LiteralPath (Join-Path $RepositoryRoot "tests\fixtures\visualization_service") -Recurse -File |
        Sort-Object FullName |
        ForEach-Object {
            $relative = $_.FullName.Substring($RepositoryRoot.Length).TrimStart([char[]]"\/").Replace('\', '/')
            $inputDigests.Add([ordered]@{ path = $relative; sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant() })
        }
    # Bind every shipped workbench source file to the evidence as well as its
    # dependency manifests. Generated dependencies and build output are
    # intentionally excluded because the gate recreates them with npm ci/build.
    Get-ChildItem -LiteralPath (Join-Path $RepositoryRoot "workbench") -Recurse -File |
        Where-Object { $_.FullName -notmatch '[\\/]node_modules[\\/]' -and $_.FullName -notmatch '[\\/]dist[\\/]' } |
        Sort-Object FullName |
        ForEach-Object {
            $relative = $_.FullName.Substring($RepositoryRoot.Length).TrimStart([char[]]"/\\").Replace('\\', '/')
            if (-not ($inputDigests | Where-Object { $_.path -eq $relative })) {
                $inputDigests.Add([ordered]@{ path = $relative; sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant() })
            }
        }
}

function Test-GitClean {
    param([string]$Gate = "git-clean")
    $log = Join-Path $EvidenceDirectory ("$Gate.log")
    $gitStatus = (& git -C $RepositoryRoot status --porcelain=v1 --untracked-files=all 2>&1 | Out-String)
    [System.IO.File]::WriteAllText($log, $gitStatus, $Utf8NoBom)
    if ([string]::IsNullOrWhiteSpace($gitStatus)) {
        Add-Record $Gate "pass" 0 "working tree is clean at $commit" "git status --porcelain=v1 --untracked-files=all" $log | Out-Null
        return $true
    }
    Add-Record $Gate "fail" 1 "working tree is dirty; commit, stash, or remove every listed change before release checks" "git status --porcelain=v1 --untracked-files=all" $log | Out-Null
    return $false
}

function Test-ExactToolchain {
    $probe = "import importlib.metadata as m, json, sys; print(json.dumps({'python': '.'.join(map(str, sys.version_info[:3])), 'pytest': m.version('pytest'), 'jsonschema': m.version('jsonschema'), 'numpy': m.version('numpy')}))"
    $probeOk = Invoke-NativeGate "python-toolchain-probe" "python" @("-c", $probe)
    if (-not $probeOk) {
        Add-Record "toolchain-exact" "fail" 1 "Python package versions could not be inspected" "" "" | Out-Null
        return $false
    }
    try {
        $observed = (Get-Content -LiteralPath (Join-Path $EvidenceDirectory "python-toolchain-probe.log") -Raw | ConvertFrom-Json)
        $node = (& node --version 2>&1 | Out-String).Trim()
        $npm = (& npm --version 2>&1 | Out-String).Trim()
        $mismatches = @()
        foreach ($name in @("python", "pytest", "jsonschema", "numpy")) {
            if ($observed.$name -ne $ExpectedToolchain[$name]) { $mismatches += "$name expected $($ExpectedToolchain[$name]), observed $($observed.$name)" }
        }
        if ($node -ne $ExpectedToolchain.node) { $mismatches += "node expected $($ExpectedToolchain.node), observed $node" }
        if ($npm -ne $ExpectedToolchain.npm) { $mismatches += "npm expected $($ExpectedToolchain.npm), observed $npm" }
        $toolchainJson = [ordered]@{
            expected = $ExpectedToolchain
            observed = [ordered]@{ python = $observed.python; pytest = $observed.pytest; jsonschema = $observed.jsonschema; numpy = $observed.numpy; node = $node; npm = $npm }
        } | ConvertTo-Json -Depth 4
        [System.IO.File]::WriteAllText((Join-Path $EvidenceDirectory "toolchain-observed.json"), $toolchainJson + [Environment]::NewLine, $Utf8NoBom)
        if ($mismatches.Count -gt 0) {
            Add-Record "toolchain-exact" "fail" 1 ($mismatches -join "; ") "python/node/npm version probe" (Join-Path $EvidenceDirectory "toolchain-observed.json") | Out-Null
            return $false
        }
        Add-Record "toolchain-exact" "pass" 0 "exact verified Python/Node/npm versions observed" "python/node/npm version probe" (Join-Path $EvidenceDirectory "toolchain-observed.json") | Out-Null
        return $true
    } catch {
        Add-Record "toolchain-exact" "fail" 1 $_.Exception.Message "python/node/npm version probe" "" | Out-Null
        return $false
    }
}

function Copy-IsolatedWorkbench {
    $source = Join-Path $RepositoryRoot "workbench"
    $destination = Join-Path $EvidenceDirectory "workbench-source"
    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
        Add-Record "workbench-copy" "fail" 1 "workbench directory is missing" "" "" | Out-Null
        return $null
    }
    New-Item -ItemType Directory -Path $destination | Out-Null
    try {
        Get-ChildItem -LiteralPath $source -Force | Where-Object { $_.Name -notin @("node_modules", "dist") } |
            ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $destination -Recurse -Force }
        Add-Record "workbench-copy" "pass" 0 "npm runs only in the evidence-directory copy; source workbench/node_modules is untouched" "Copy-Item workbench -> evidence/workbench-source (excluding node_modules, dist)" "" | Out-Null
        return $destination
    } catch {
        Add-Record "workbench-copy" "fail" 1 $_.Exception.Message "Copy-Item workbench -> evidence/workbench-source" "" | Out-Null
        return $null
    }
}

function Test-FormalDemoEvidence {
    if (-not $FormalDemoRunsRoot) {
        Add-Record "formal-demo-evidence" "fail" 1 "full local release gate requires -FormalDemoRunsRoot pointing to externally generated formal Buck/Boost evidence" "" "" | Out-Null
        return
    }
    $root = [System.IO.Path]::GetFullPath($FormalDemoRunsRoot)
    $probe = @'
import json
import sys
from pathlib import Path
from pe_sim.workbench_export import load_run_workbench_contracts

root = Path(sys.argv[1]).resolve()
commit = sys.argv[2]
records = []
for topology in ('buck', 'boost'):
    run_dir = root / topology
    contracts = load_run_workbench_contracts(run_dir)
    manifest = json.loads((run_dir / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('run_mode') != 'formal_comparison':
        raise ValueError(f'{topology} is not a formal_comparison run')
    if manifest.get('source_commit') != commit:
        raise ValueError(f'{topology} source_commit does not match release commit')
    records.append({'topology': topology, 'run_path': str(run_dir), 'manifest_sha256': contracts.manifest_sha256, 'package_sha256': contracts.package_sha256})
print(json.dumps(records, sort_keys=True))
'@
    $ok = Invoke-NativeGate "formal-demo-evidence" "python" @("-c", $probe, $root, $commit)
    if (-not $ok) { return }
    try {
        $script:formalDemoEvidence = @((Get-Content -LiteralPath (Join-Path $EvidenceDirectory "formal-demo-evidence.log") -Raw | ConvertFrom-Json))
        Add-Record "formal-demo-record" "pass" 0 "formal Buck/Boost demo paths and integrity hashes were recorded" "read formal-demo-evidence output" (Join-Path $EvidenceDirectory "formal-demo-evidence.log") | Out-Null
    } catch {
        Add-Record "formal-demo-record" "fail" 1 $_.Exception.Message "read formal-demo-evidence output" "" | Out-Null
    }
}

function Write-Evidence {
    param([string]$Outcome)
    foreach ($record in $records) {
        if ($record.log -and (Test-Path -LiteralPath $record.log -PathType Leaf)) {
            $record.log_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $record.log).Hash.ToLowerInvariant()
        }
    }
    $endCommit = (& git -C $RepositoryRoot rev-parse HEAD 2>$null).Trim()
    $evidence = [ordered]@{
        schema_version = "pe-sim.local-release-evidence.v1"
        outcome = $Outcome
        execution_mode = $executionMode
        generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        repository = [ordered]@{
            source_commit = $commit
            source_commit_after_checks = $endCommit
            root = $RepositoryRoot
            clean_commit_required = $true
        }
        toolchain_policy = [ordered]@{
            policy_document = "docs/28_release_toolchain_policy_v0.md"
            expected = $ExpectedToolchain
            observed_file = "toolchain-observed.json"
        }
        source_input_digests = @($inputDigests.ToArray())
        gates = @($records.ToArray())
        formal_demo_evidence = @($formalDemoEvidence)
        limitations = @(
            "This script does not create, modify, or approve official Buck/Boost release demonstrations; item 4 supplies them through -FormalDemoRunsRoot.",
            "The source-controlled visualization contract fixture proves only the local read-only service and independent consumer path. It is not release demonstration evidence.",
            "A passing local gate is not human approval, a version tag, model validation, or an engineering or hardware claim.",
            "The browser acceptance suite uses an isolated workbench copy and Python Playwright locally; it is not an npm runtime dependency or a hosted-browser claim."
        )
        human_review_required = $true
    }
    $recordPath = Join-Path $EvidenceDirectory "release-evidence.json"
    [System.IO.File]::WriteAllText($recordPath, (($evidence | ConvertTo-Json -Depth 14) + [Environment]::NewLine), $Utf8NoBom)
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $recordPath).Hash.ToLowerInvariant()
    [System.IO.File]::WriteAllText((Join-Path $EvidenceDirectory "release-evidence.sha256"), "$hash  release-evidence.json`n", $Utf8NoBom)
    Write-Host "Release evidence: $recordPath"
    Write-Host "Evidence SHA-256: $hash"
}

try {
    Push-Location -LiteralPath $RepositoryRoot
    Record-InputDigests
    $gitClean = Test-GitClean
    if (-not $gitClean) {
        Add-NotRunRecord "full-local-gate" "blocked before tests, demo handling, service startup, and evidence qualification because the exact source commit is not clean"
        return
    }

    $toolchainOk = Test-ExactToolchain
    # Keep the complete resolved Python environment alongside the exact
    # version probe so a later reviewer can audit transitive dependencies.
    $pipFreezeOk = Invoke-NativeGate "python-pip-freeze" "python" @("-m", "pip", "freeze")
    $pipOk = Invoke-NativeGate "python-pip-check" "python" @("-m", "pip", "check")
    $environmentOk = Invoke-NativeGate "python-environment-check" "python" @("-m", "pe_sim.cli", "environment-check", "--requirements", "requirements-tested.txt", "--pyproject", "pyproject.toml")
    if ($PreflightOnly) {
        Add-NotRunRecord "full-local-gate" "preflight only: tests, npm ci, build, service, consumer, and formal-demo evidence checks were intentionally not run"
        return
    }
    if (-not ($toolchainOk -and $pipFreezeOk -and $pipOk -and $environmentOk)) {
        Add-NotRunRecord "python-pytest" "blocked by exact toolchain, pip freeze, or Python environment failure"
        Add-NotRunRecord "workbench-npm-ci" "blocked by exact toolchain or Python environment failure"
        Add-NotRunRecord "visualization-service" "blocked by exact toolchain or Python environment failure"
        Add-NotRunRecord "external-consumer-service" "blocked by exact toolchain or Python environment failure"
        return
    }

    $pytestBase = Join-Path $EvidenceDirectory "pytest-basetemp"
    $pythonTestsOk = Invoke-NativeGate "python-pytest" "python" @("-m", "pytest", "-q", "--basetemp", $pytestBase, "-o", "cache_dir=$EvidenceDirectory\\pytest-cache")
    $workbenchCopy = Copy-IsolatedWorkbench
    $nodeOk = $false
    if ($null -ne $workbenchCopy) {
        $npmInstallOk = Invoke-NativeGate "workbench-npm-ci" "npm" @("ci") $workbenchCopy
        if ($npmInstallOk) {
            $nodeOk = Invoke-NativeGate "workbench-build" "npm" @("run", "build") $workbenchCopy
        } else {
            Add-NotRunRecord "workbench-build" "isolated npm ci failed; source workbench was not modified"
        }
    } else {
        Add-NotRunRecord "workbench-npm-ci" "isolated workbench copy could not be prepared"
        Add-NotRunRecord "workbench-build" "isolated workbench copy could not be prepared"
    }
    if (-not ($pythonTestsOk -and $nodeOk)) {
        Add-NotRunRecord "visualization-service" "blocked by test or workbench build failure"
        Add-NotRunRecord "external-consumer-service" "blocked by test or workbench build failure"
        return
    }

    Test-FormalDemoEvidence
    $fixtureRoot = Join-Path $RepositoryRoot "tests\fixtures\visualization_service"
    $serviceStdout = Join-Path $EvidenceDirectory "visualization-service.stdout.log"
    $serviceStderr = Join-Path $EvidenceDirectory "visualization-service.stderr.log"
    $serviceCommand = Format-Command "python" @("-m", "pe_sim.cli", "visualization-serve", "--runs-root", $fixtureRoot, "--host", "127.0.0.1", "--port", "$ServicePort")
    $serviceProcess = Start-Process -FilePath "python" -ArgumentList @("-m", "pe_sim.cli", "visualization-serve", "--runs-root", $fixtureRoot, "--host", "127.0.0.1", "--port", "$ServicePort") -WorkingDirectory $RepositoryRoot -RedirectStandardOutput $serviceStdout -RedirectStandardError $serviceStderr -PassThru
    $baseUrl = "http://127.0.0.1:$ServicePort"
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 200
        if ($serviceProcess.HasExited) { break }
        try {
            $health = Invoke-RestMethod -Uri "$baseUrl/health" -TimeoutSec 1
            if ($health.status -eq "ok" -and $health.read_only -eq $true) { $ready = $true; break }
        } catch { }
    }
    if ($ready) {
        Add-Record "visualization-service" "pass" 0 "process_id=$($serviceProcess.Id); loopback read-only health endpoint responded" $serviceCommand $serviceStdout | Out-Null
        Invoke-NativeGate "external-consumer-service" "python" @("examples/visualization_consumer/verify_contracts.py", "--service", $baseUrl, "--run", "buck-contract-fixture") | Out-Null
        # The test starts Vite only from the isolated npm-ci copy.  The service
        # remains the loopback process started above, exercising CORS and the
        # actual service-mode contract load without modifying workbench/.
        $env:PE_SIM_RUN_BROWSER_E2E = "1"
        $env:PE_SIM_WORKBENCH_DIR = $workbenchCopy
        $env:PE_SIM_BROWSER_SERVICE_URL = $baseUrl
        $env:PE_SIM_BROWSER_RUN_ID = "buck-contract-fixture"
        Invoke-NativeGate "workbench-browser-e2e" "python" @("-m", "pytest", "-q", "tests/test_workbench_browser_e2e.py", "--basetemp", (Join-Path $EvidenceDirectory "browser-e2e-basetemp"), "-o", "cache_dir=$EvidenceDirectory\\browser-e2e-cache") | Out-Null
        Remove-Item Env:PE_SIM_RUN_BROWSER_E2E -ErrorAction SilentlyContinue
        Remove-Item Env:PE_SIM_WORKBENCH_DIR -ErrorAction SilentlyContinue
        Remove-Item Env:PE_SIM_BROWSER_SERVICE_URL -ErrorAction SilentlyContinue
        Remove-Item Env:PE_SIM_BROWSER_RUN_ID -ErrorAction SilentlyContinue
    } else {
        $detail = "service did not become ready on loopback port $ServicePort; only the process started by this script will be stopped"
        Add-Record "visualization-service" "fail" 1 $detail $serviceCommand $serviceStdout | Out-Null
        Add-NotRunRecord "external-consumer-service" "visualization service did not become ready"
        Add-NotRunRecord "workbench-browser-e2e" "visualization service did not become ready"
    }

    Test-GitClean "git-clean-after-checks" | Out-Null
} catch {
    Add-Record "release-check-internal" "fail" 1 $_.Exception.Message "" "" | Out-Null
    Write-Error $_
} finally {
    if ($null -ne $serviceProcess) {
        try {
            if (-not $serviceProcess.HasExited) {
                # This is the PID created above, never a guessed or externally
                # discovered process, so unrelated Vite/user processes survive.
                Stop-Process -Id $serviceProcess.Id -ErrorAction Stop
                $serviceProcess.WaitForExit(5000) | Out-Null
            }
        } catch {
            Add-Record "visualization-service-stop" "fail" 1 $_.Exception.Message "Stop-Process -Id $($serviceProcess.Id)" "" | Out-Null
        }
    }
    try { Pop-Location } catch { }
    $outcome = if ($hasFailure) { "fail" } elseif ($PreflightOnly) { "preflight_pass" } else { "pass" }
    Write-Evidence $outcome
    if ($hasFailure) { exit 1 }
}
