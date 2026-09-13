<#
.SYNOPSIS
    Stops the vox Windows companion and the Docker backend.

.DESCRIPTION
    Terminates every companion process (any python.exe / pythonw.exe whose command
    line mentions vox_client, plus a running vox.exe), then takes the
    compose stack down.

    The script works from any working directory: the repository root is resolved
    from the script's own location.

.PARAMETER KeepBackend
    Stop only the Windows companion and leave the Docker backend running. Useful
    when restarting the companion after a config change.

.PARAMETER Volumes
    Also remove the named volumes. This DELETES the cached Whisper weights and the
    Ollama models, which are then downloaded again on the next start, so it asks
    for confirmation first.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\stop.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\stop.ps1 -KeepBackend

.NOTES
    Exit code 0 on success, 1 when "docker compose down" fails.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$KeepBackend,
    [switch]$Volumes
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

function Write-Warn {
    param([string]$Message)
    Write-Host "WARNING: $Message" -ForegroundColor Yellow
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

function Get-CompanionProcess {
    $filter = "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'vox.exe'"
    $processes = @(Get-CimInstance -ClassName Win32_Process -Filter $filter -ErrorAction SilentlyContinue)
    return @($processes | Where-Object { $_.Name -eq "vox.exe" -or $_.CommandLine -like "*vox_client*" })
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ComposeFile = Join-Path $RepoRoot "compose.yaml"

Write-Host "vox - stop"
Write-Info "repository: $RepoRoot"

Write-Step "Stopping the Windows companion"

$companions = Get-CompanionProcess
if ($companions.Count -eq 0) {
    Write-Info "no companion process is running."
} else {
    foreach ($companion in $companions) {
        $processId = $companion.ProcessId
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
            Write-Info "stopped $($companion.Name) (PID $processId)"
        } catch {
            Write-Warn "could not stop $($companion.Name) (PID ${processId}): $($_.Exception.Message)"
        }
    }

    Start-Sleep -Seconds 1
    $survivors = Get-CompanionProcess
    if ($survivors.Count -gt 0) {
        $survivorPids = ($survivors | ForEach-Object { $_.ProcessId }) -join ", "
        Write-Warn "still running after the stop request: PID $survivorPids. Another user may own them; end them from Task Manager."
    }
}

if ($KeepBackend) {
    Write-Step "Backend"
    Write-Info "-KeepBackend was passed - the Docker backend is still running."
    Write-Host ""
    exit 0
}

Write-Step "Stopping the Docker backend"

if (-not (Test-Path -LiteralPath $ComposeFile)) {
    Stop-WithError "$ComposeFile is missing. The repository checkout is incomplete."
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
    Write-Warn "The 'docker' command was not found, so there is no backend to stop."
    Write-Host ""
    exit 0
}
$dockerExit = Invoke-Native -FilePath $docker.Source -Quiet -Arguments @("version")
if ($dockerExit -ne 0) {
    Write-Warn "The Docker engine is not responding, so the backend is not running. Nothing to stop."
    Write-Host ""
    exit 0
}

$removeVolumes = $false
if ($Volumes) {
    Write-Warn "-Volumes removes the named volumes hf-cache, ct2-cache and ollama-models."
    Write-Info "That deletes the cached Whisper weights and any Ollama models; the next start downloads them again."
    if (-not [Environment]::UserInteractive) {
        Stop-WithError "Refusing to remove volumes without a confirmation in a non-interactive session. Re-run this script from an interactive PowerShell window."
    }
    $answer = Read-Host "Type 'yes' to remove the volumes, anything else to keep them"
    if ($answer -eq "yes") {
        $removeVolumes = $true
    } else {
        Write-Info "keeping the volumes."
    }
}

# The bundled-model profile must be named or 'down' leaves vox-ollama running and
# never removes ollama-models, contradicting the confirmation prompt above.
$composeArgs = @("compose", "-f", $ComposeFile, "--profile", "bundled-model", "down")
if ($removeVolumes) {
    $composeArgs += "--volumes"
}
$downExit = Invoke-Native -FilePath $docker.Source -Arguments $composeArgs
if ($downExit -ne 0) {
    Stop-WithError "'docker compose down' failed with exit code $downExit. Read the output above; inspect the stack with: docker compose -f '$ComposeFile' ps"
}

Write-Host ""
Write-Host "Stopped." -ForegroundColor Green
if ($removeVolumes) {
    Write-Info "volumes removed - the next start re-downloads the Whisper weights."
}
Write-Host ""
exit 0
