<#
.SYNOPSIS
    Installs the vox Windows companion into the project virtual environment.

.DESCRIPTION
    Verifies the host is 64-bit Windows 10 or 11, locates the uv package manager,
    creates or updates .venv with the "client" extra and the "dev" dependency group
    (Python 3.14, taken from pyproject.toml), and seeds config\client.yaml from
    config\client.example.yaml when it does not exist yet.

    An existing config\client.yaml is never overwritten. The backend .env file is
    not touched at all - scripts\start.ps1 creates that one.

    The script works from any working directory: the repository root is resolved
    from the script's own location.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-client.ps1

.NOTES
    Exit code 0 on success, 1 on any failure.
#>
#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Info {
    param([string]$Message)
    Write-Host "    $Message"
}

function Stop-WithError {
    param([string]$Message)
    Write-Host ""
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

# Native tools write progress to stderr; under $ErrorActionPreference = "Stop" Windows
# PowerShell can turn that into a terminating NativeCommandError, so relax it here.
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        # Out-Host keeps the tool's own output on the console instead of letting it
        # become this function's return value, which must be the exit code alone.
        & $FilePath @Arguments | Out-Host
    } finally {
        $ErrorActionPreference = $previous
    }
    return $LASTEXITCODE
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Write-Host "vox - Windows companion installer"
Write-Info "repository: $RepoRoot"

Write-Step "Checking the host"

$os = Get-CimInstance -ClassName Win32_OperatingSystem
$osVersion = [Version]$os.Version
if ($osVersion.Major -lt 10) {
    Stop-WithError "Windows 10 or 11 is required. Detected: $($os.Caption) ($($os.Version))."
}
if (-not [Environment]::Is64BitOperatingSystem) {
    Stop-WithError "A 64-bit Windows installation is required. Detected a 32-bit operating system."
}
Write-Info "$($os.Caption) build $($os.BuildNumber), 64-bit: OK"
Write-Info "PowerShell $($PSVersionTable.PSVersion)"

Write-Step "Locating uv"

$uv = $null
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCommand) {
    $uv = $uvCommand.Source
}
if (-not $uv) {
    $uvFallback = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path -LiteralPath $uvFallback) {
        $uv = $uvFallback
    }
}
if (-not $uv) {
    Write-Host ""
    Write-Host "ERROR: uv was not found on PATH and not at $env:USERPROFILE\.local\bin\uv.exe" -ForegroundColor Red
    Write-Host ""
    Write-Host "Install it with this exact command, then open a new PowerShell window and re-run this script:"
    Write-Host ""
    Write-Host "    irm https://astral.sh/uv/install.ps1 | iex"
    Write-Host ""
    exit 1
}
Write-Info "uv: $uv"

$uvVersionExit = Invoke-Native -FilePath $uv -Arguments @("--version")
if ($uvVersionExit -ne 0) {
    Stop-WithError "'$uv --version' failed with exit code $uvVersionExit. The uv installation looks broken; reinstall it with: irm https://astral.sh/uv/install.ps1 | iex"
}

Write-Step "Syncing the virtual environment (uv sync --extra client --group dev)"

Push-Location -LiteralPath $RepoRoot
try {
    $syncExit = Invoke-Native -FilePath $uv -Arguments @("sync", "--extra", "client", "--group", "dev")
} finally {
    Pop-Location
}
if ($syncExit -ne 0) {
    Stop-WithError "'uv sync --extra client --group dev' failed with exit code $syncExit. Read the uv output above; a missing Python 3.14 is fixed with: uv python install 3.14"
}

$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Stop-WithError "uv sync reported success but $venvPython does not exist. Delete the .venv directory and run this script again."
}

$previousPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $pythonVersion = & $venvPython --version
} finally {
    $ErrorActionPreference = $previousPreference
}
if ($LASTEXITCODE -ne 0) {
    Stop-WithError "'$venvPython --version' failed with exit code $LASTEXITCODE. Delete the .venv directory and run this script again."
}

Write-Step "Preparing config\client.yaml"

$configDir = Join-Path $RepoRoot "config"
$exampleConfig = Join-Path $configDir "client.example.yaml"
$userConfig = Join-Path $configDir "client.yaml"

if (-not (Test-Path -LiteralPath $exampleConfig)) {
    Stop-WithError "$exampleConfig is missing. The repository checkout is incomplete."
}
if (Test-Path -LiteralPath $userConfig) {
    Write-Info "config\client.yaml already exists - left untouched."
} else {
    Copy-Item -LiteralPath $exampleConfig -Destination $userConfig
    Write-Info "created config\client.yaml from config\client.example.yaml"
}

Write-Step "Done"

Write-Info "python : $pythonVersion ($venvPython)"
Write-Info "config : $userConfig"
Write-Host ""
Write-Host "Next: edit config\client.yaml if you want different hotkeys, then run:"
Write-Host ""
Write-Host "    powershell -ExecutionPolicy Bypass -File scripts\start.ps1"
Write-Host ""
exit 0
