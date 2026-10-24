# T4 upgrade gate: an installed release reads a package produced by an older
# release and states cross-version comparability honestly.
#
# This script never mutates a published package and never installs into the
# developer environment: it builds the current checkout as a wheel, installs it
# into an isolated virtual environment, and runs the documented read-only
# commands against an older package.
#
# Evidence is written to `upgrade-evidence.json`; any failed check makes the
# script exit non-zero so a release gate cannot record a pass.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$EvidenceDirectory,
    [string]$OlderPackage,
    [string]$PythonExecutable = "python",
    [string]$VirtualEnvironment,
    [switch]$ReuseVirtualEnvironment
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-JsonFile([string]$Path, $Value) {
    [System.IO.File]::WriteAllText($Path, (($Value | ConvertTo-Json -Depth 14) + [Environment]::NewLine), $Utf8NoBom)
}

function Get-PropertyValue($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Invoke-Probe([string]$Name, [string]$Tool, [string[]]$Arguments, [int[]]$ExpectedExit = @(0)) {
    $log = Join-Path $EvidenceDirectory ("{0}.log" -f $Name)
    # A native command that writes to stderr must not become a terminating
    # error just because the caller runs with $ErrorActionPreference = "Stop";
    # the exit code is the contract, and the stderr text is evidence.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Tool @Arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    $text = ($output | Out-String).Trim()
    [System.IO.File]::WriteAllText($log, $text + [Environment]::NewLine, $Utf8NoBom)
    $payload = $null
    if ($text.StartsWith("{")) {
        try { $payload = $text | ConvertFrom-Json } catch { $payload = $null }
    }
    $passed = $ExpectedExit -contains $code
    return [ordered]@{
        name = $Name
        command = ($Tool + " " + ($Arguments -join " "))
        exit_code = $code
        expected_exit_codes = $ExpectedExit
        passed = $passed
        log = $log
        output = $payload
    }
}

$EvidenceDirectory = [System.IO.Path]::GetFullPath($EvidenceDirectory)
if ((Test-Path -LiteralPath $EvidenceDirectory) -and (Get-ChildItem -LiteralPath $EvidenceDirectory -Force | Select-Object -First 1)) {
    throw "evidence directory must not already contain files: $EvidenceDirectory"
}
New-Item -ItemType Directory -Force -Path $EvidenceDirectory | Out-Null

if (-not $OlderPackage) {
    $OlderPackage = Join-Path $RepositoryRoot "tests\fixtures\older_packages\v0.5.9-buck"
}
$OlderPackage = [System.IO.Path]::GetFullPath($OlderPackage)
if (-not (Test-Path -LiteralPath (Join-Path $OlderPackage "manifest.json") -PathType Leaf)) {
    throw "older package does not look like a run package: $OlderPackage"
}

$commit = (& git -C $RepositoryRoot rev-parse HEAD 2>$null).Trim()
$dirty = (& git -C $RepositoryRoot status --porcelain --untracked-files=all 2>$null)
$versionText = (Get-Content -LiteralPath (Join-Path $RepositoryRoot "pyproject.toml") -Raw)
$version = ([regex]::Match($versionText, '(?m)^version\s*=\s*"([^"]+)"')).Groups[1].Value

$checks = New-Object System.Collections.Generic.List[object]
$result = [ordered]@{
    schema = "pe-sim.upgrade-evidence.v1"
    created_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    source_commit = if ($commit -match "^[0-9a-f]{40}$") { $commit } else { "unknown" }
    working_tree_status = if ($dirty) { "dirty" } else { "clean" }
    reader_version = $version
    older_package = $OlderPackage
    python_environment = $null
    checks = $checks
    outcome = "fail"
}

try {
    if ($dirty) {
        throw "the upgrade gate needs a clean source commit; commit or stash every change first"
    }

    if (-not $VirtualEnvironment) {
        $VirtualEnvironment = Join-Path $EvidenceDirectory "venv"
    }
    $venv = [System.IO.Path]::GetFullPath($VirtualEnvironment)
    $venvPython = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython) -or -not $ReuseVirtualEnvironment) {
        if (Test-Path -LiteralPath $venv) { Remove-Item -Recurse -Force -LiteralPath $venv }
        $creator = $PythonExecutable
        $creatorArgs = @("-m", "venv", $venv)
        if ($creator -eq "py") { $creatorArgs = @("-3.12", "-m", "venv", $venv) }
        & $creator @creatorArgs *>&1 | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
            throw "could not create an isolated virtual environment at $venv"
        }
    }
    $result.python_environment = [ordered]@{
        mode = "isolated_venv"
        path = $venv
        interpreter = (& $venvPython -c "import sys; print(sys.version.split()[0])").Trim()
    }

    $bootstrap = Invoke-Probe "pip-bootstrap" $venvPython @("-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "pip", "setuptools", "wheel")
    $checks.Add($bootstrap)
    if (-not $bootstrap.passed) { throw "pip bootstrap failed" }

    $wheelDir = Join-Path $EvidenceDirectory "wheel"
    New-Item -ItemType Directory -Force -Path $wheelDir | Out-Null
    $build = Invoke-Probe "wheel-build" $venvPython @("-m", "pip", "wheel", $RepositoryRoot, "--no-deps", "--quiet", "-w", $wheelDir)
    $checks.Add($build)
    if (-not $build.passed) { throw "could not build a wheel from the checkout" }
    $wheel = Get-ChildItem -LiteralPath $wheelDir -Filter "*.whl" | Select-Object -First 1
    if ($null -eq $wheel) { throw "wheel build produced no wheel" }

    $requirements = Invoke-Probe "pinned-requirements-install" $venvPython @("-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", (Join-Path $RepositoryRoot "requirements-tested.txt"))
    $checks.Add($requirements)
    if (-not $requirements.passed) { throw "pinned requirements install failed" }

    $install = Invoke-Probe "wheel-install" $venvPython @("-m", "pip", "install", "--disable-pip-version-check", "-q", "--no-deps", $wheel.FullName)
    $checks.Add($install)
    if (-not $install.passed) { throw "wheel install failed" }

    $installed = Invoke-Probe "installed-version" $venvPython @("-m", "pe_sim.cli", "--version")
    $checks.Add($installed)
    $installedText = (Get-Content -LiteralPath $installed.log -Raw).Trim()
    $versionMatches = $installedText -eq $version
    $checks.Add([ordered]@{
        name = "installed-version-matches-checkout"
        command = "compare installed --version with pyproject"
        exit_code = if ($versionMatches) { 0 } else { 1 }
        expected_exit_codes = @(0)
        passed = $versionMatches
        log = $installed.log
        output = $null
    })
    if (-not $versionMatches) { throw "installed version $installedText does not match checkout version $version" }

    $inspect = Invoke-Probe "read-older-package" $venvPython @("-m", "pe_sim.cli", "inspect-run", $OlderPackage, "--compact")
    $checks.Add($inspect)
    $inspectStatus = Get-PropertyValue $inspect.output "status"
    $compatibilityStatus = Get-PropertyValue (Get-PropertyValue $inspect.output "compatibility") "status"
    $readable = $inspect.passed -and $inspectStatus -eq "ok" -and $compatibilityStatus -in @("current", "read_only")
    $checks.Add([ordered]@{
        name = "older-package-readability"
        command = "inspect-run status/compatibility"
        exit_code = if ($readable) { 0 } else { 1 }
        expected_exit_codes = @(0)
        passed = $readable
        log = $inspect.log
        output = [ordered]@{ status = $inspectStatus; compatibility = $compatibilityStatus }
    })

    $listRuns = Invoke-Probe "older-package-discovery" $venvPython @("-m", "pe_sim.cli", "list-runs", (Split-Path -Parent $OlderPackage))
    $checks.Add($listRuns)
    $discoveredCountRaw = Get-PropertyValue $listRuns.output "count"
    $discoveredCount = if ($null -eq $discoveredCountRaw) { 0 } else { [int]$discoveredCountRaw }
    $discovered = $listRuns.passed -and ($discoveredCount -ge 1)
    $checks.Add([ordered]@{
        name = "older-package-listed"
        command = "list-runs <older package parent>"
        exit_code = if ($discovered) { 0 } else { 1 }
        expected_exit_codes = @(0)
        passed = [bool]$discovered
        log = $listRuns.log
        output = [ordered]@{ count = $discoveredCount }
    })

    # Produce a package with the installed release so the comparison is a real
    # cross-release comparison, not a simulated one.
    $currentRuns = Join-Path $EvidenceDirectory "current-runs"
    $configSource = Join-Path $RepositoryRoot "examples\buck_pi_step\config.json"
    $config = Get-Content -LiteralPath $configSource -Raw | ConvertFrom-Json
    $config.output.directory = $currentRuns
    $configPath = Join-Path $EvidenceDirectory "current-config.json"
    Write-JsonFile $configPath $config
    $currentRun = Invoke-Probe "current-release-run" $venvPython @("-m", "pe_sim.cli", "run", $configPath)
    $checks.Add($currentRun)
    if (-not $currentRun.passed) { throw "the installed release could not run the reference example" }
    $currentPackage = $config.output.directory + "\" + $config.run_id

    $compare = Invoke-Probe "cross-release-comparison" $venvPython @("-m", "pe_sim.cli", "compare-runs", $OlderPackage, $currentPackage) @(0, 1)
    $checks.Add($compare)
    $note = Get-PropertyValue $compare.output "compatibility"
    $crossVersion = Get-PropertyValue $note "cross_version_comparison"
    $declared = $null -ne $note -and $crossVersion -eq "not_assessed"
    $checks.Add([ordered]@{
        name = "cross-release-comparability-stated"
        command = "compare-runs compatibility.cross_version_comparison"
        exit_code = if ($declared) { 0 } else { 1 }
        expected_exit_codes = @(0)
        passed = [bool]$declared
        log = $compare.log
        output = [ordered]@{
            outcome = Get-PropertyValue $compare.output "outcome"
            cross_version_comparison = $crossVersion
            reasons = @(Get-PropertyValue $note "reasons")
        }
    })

    $failedChecks = @($checks | Where-Object { -not $_.passed })
    $result.outcome = if ($failedChecks.Count -eq 0) { "pass" } else { "fail" }
}
catch {
    $checks.Add([ordered]@{
        name = "upgrade-gate"
        command = "upgrade_check.ps1"
        exit_code = 1
        expected_exit_codes = @(0)
        passed = $false
        log = $null
        output = [ordered]@{ error = $_.Exception.Message }
    })
    $result.outcome = "fail"
    Write-Error $_
}

$evidencePath = Join-Path $EvidenceDirectory "upgrade-evidence.json"
Write-JsonFile $evidencePath $result
Write-Host "Upgrade evidence: $evidencePath"
Write-Host "Upgrade outcome: $($result.outcome)"
if ($result.outcome -ne "pass") { exit 1 }
exit 0
