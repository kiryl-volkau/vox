<#
.SYNOPSIS
    Builds the standalone vox.exe companion with PyInstaller.

.DESCRIPTION
    Runs PyInstaller through uv (uv run --extra client --group dev) to produce a
    one-file, windowed executable named vox.exe in dist\.

    The server stack (faster-whisper, CTranslate2, FastAPI, uvicorn, torch,
    transformers) is explicitly excluded, so the executable never carries the
    Whisper or LLM dependencies - those live in the Docker backend.

    config\client.example.yaml is bundled next to the executable as config\ so a
    fresh machine has a template to copy.

    The script works from any working directory: the repository root is resolved
    from the script's own location.

.PARAMETER KeepBuildDir
    Keep the intermediate build\ directory instead of asking PyInstaller to clean
    its cache first. Useful when iterating on the build.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build-client.ps1

.NOTES
    Exit code 0 on success, 1 on any failure.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$KeepBuildDir
)

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
        [string[]]$Arguments = @(),
        [switch]$Quiet
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Quiet) {
            & $FilePath @Arguments > $null 2>&1
        } else {
            # Out-Host keeps the tool's own output on the console instead of letting it
            # become this function's return value, which must be the exit code alone.
            & $FilePath @Arguments | Out-Host
        }
    } finally {
        $ErrorActionPreference = $previous
    }
    return $LASTEXITCODE
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Write-Host "vox - companion executable build"
Write-Info "repository: $RepoRoot"

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

$entryPoint = Join-Path $RepoRoot "src\vox_client\main.py"
$exampleConfig = Join-Path $RepoRoot "config\client.example.yaml"
$distDir = Join-Path $RepoRoot "dist"
$specDir = Join-Path $RepoRoot "build"
# --clean empties the workpath before PyInstaller executes the generated spec file, so the
# spec must live outside it.
$workDir = Join-Path $specDir "pyinstaller"
$exePath = Join-Path $distDir "vox.exe"

if (-not (Test-Path -LiteralPath $entryPoint)) {
    Stop-WithError "$entryPoint is missing. The repository checkout is incomplete."
}
if (-not (Test-Path -LiteralPath $exampleConfig)) {
    Stop-WithError "$exampleConfig is missing. The repository checkout is incomplete."
}

Write-Step "Checking for the bundled PortAudio data package"

$soundDeviceDataArgs = @()
$probeExit = Invoke-Native -FilePath $uv -Quiet -Arguments @(
    "run", "--extra", "client", "--group", "dev", "python", "-c",
    "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('_sounddevice_data') else 1)"
)
if ($probeExit -eq 0) {
    # sounddevice ships the PortAudio DLL in a separate top-level package.
    $soundDeviceDataArgs = @("--collect-binaries", "_sounddevice_data", "--collect-data", "_sounddevice_data")
    Write-Info "_sounddevice_data found - PortAudio binaries will be collected."
} else {
    Write-Info "_sounddevice_data not found - relying on the PyInstaller sounddevice hook."
}

Write-Step "Running PyInstaller"

$pyinstallerArgs = @(
    "run", "--extra", "client", "--group", "dev", "pyinstaller",
    "--noconfirm",
    "--onefile",
    "--noconsole",
    "--name", "vox",
    "--distpath", $distDir,
    "--workpath", $workDir,
    "--specpath", $specDir,
    "--hidden-import", "pynput.keyboard._win32",
    "--hidden-import", "pynput.mouse._win32",
    "--hidden-import", "pynput._util.win32",
    "--hidden-import", "pystray._win32",
    "--hidden-import", "sounddevice",
    "--hidden-import", "win32timezone",
    "--collect-binaries", "sounddevice",
    "--collect-data", "sounddevice"
)
$pyinstallerArgs += $soundDeviceDataArgs
$pyinstallerArgs += @(
    "--add-data", "$exampleConfig;config",
    "--exclude-module", "faster_whisper",
    "--exclude-module", "ctranslate2",
    "--exclude-module", "fastapi",
    "--exclude-module", "uvicorn",
    "--exclude-module", "torch",
    "--exclude-module", "transformers"
)
if (-not $KeepBuildDir) {
    $pyinstallerArgs += "--clean"
}
$pyinstallerArgs += $entryPoint

Push-Location -LiteralPath $RepoRoot
try {
    $buildExit = Invoke-Native -FilePath $uv -Arguments $pyinstallerArgs
} finally {
    Pop-Location
}
if ($buildExit -ne 0) {
    Stop-WithError "PyInstaller failed with exit code $buildExit. Read the output above; run scripts\install-client.ps1 first if the client dependencies are not installed."
}

Write-Step "Verifying the result"

if (-not (Test-Path -LiteralPath $exePath)) {
    Stop-WithError "PyInstaller reported success but $exePath does not exist. Check the PyInstaller warnings above and the log in $workDir."
}

$exe = Get-Item -LiteralPath $exePath
$sizeMb = [math]::Round($exe.Length / 1MB, 1)
Write-Info "executable : $($exe.FullName)"
Write-Info "size       : $sizeMb MB ($($exe.Length) bytes)"
Write-Info "built      : $($exe.LastWriteTime)"
Write-Host ""
Write-Host "The executable still needs the Docker backend running (scripts\start.ps1 -NoClient)."
Write-Host ""
exit 0
