<#
.SYNOPSIS
    Starts the vox Docker backend and the Windows companion.

.DESCRIPTION
    The daily one-command path:

      1. checks that the Docker engine answers;
      2. creates .env from .env.example when it is missing;
      3. brings the compose stack up in the background (rebuilding when asked or
         when the backend image does not exist yet);
      4. polls http://127.0.0.1:8765/health until it reports "ready", printing the
         current status so the first-run model download is visible;
      5. launches the companion detached with pythonw.exe, unless it is already
         running or -NoClient was passed, and prints the push-to-talk hotkeys.

    The script works from any working directory: the repository root is resolved
    from the script's own location.

.PARAMETER NoClient
    Start only the Docker backend; do not launch the Windows companion.

.PARAMETER Build
    Force "docker compose up -d --build" even when the image already exists.

.PARAMETER TimeoutSeconds
    How long to wait for /health to report "ready". Default 600. The first start
    downloads the Whisper weights, which can take several minutes.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Build -TimeoutSeconds 1200

.NOTES
    Exit code 0 on success, 1 when Docker is unavailable or the backend never
    became reachable. A "degraded" backend warns loudly but does not fail.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$NoClient,
    [switch]$Build,
    [ValidateRange(10, 7200)]
    [int]$TimeoutSeconds = 600
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

function Get-Health {
    param([string]$Uri)
    try {
        return Invoke-RestMethod -Uri $Uri -Method Get -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    } catch {
        return $null
    }
}

function Format-Field {
    param($Value)
    if ($null -eq $Value) {
        return "unknown"
    }
    $text = "$Value"
    if ($text.Trim() -eq "") {
        return "unknown"
    }
    return $text
}

function Get-CompanionProcess {
    $filter = "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'vox.exe'"
    $processes = @(Get-CimInstance -ClassName Win32_Process -Filter $filter -ErrorAction SilentlyContinue)
    return @($processes | Where-Object { $_.Name -eq "vox.exe" -or $_.CommandLine -like "*vox_client*" })
}

# Minimal reader for the "hotkeys:" block of the client YAML. Printing the combos is
# cosmetic, so anything unparseable is skipped rather than reported.
function Get-HotkeyBinding {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return @()
    }
    $lines = @()
    $inHotkeys = $false
    foreach ($line in (Get-Content -LiteralPath $Path)) {
        if ($line -match '^\s*#') {
            continue
        }
        if ($line -match '^hotkeys:\s*$') {
            $inHotkeys = $true
            continue
        }
        if (-not $inHotkeys) {
            continue
        }
        if ($line -match '^\S') {
            break
        }
        if ($line -match '^\s\s(record|dictate|cancel):\s*["'']?([^"''#]+?)["'']?\s*$') {
            $lines += ("{0,-12} {1}" -f $Matches[1], $Matches[2])
        }
    }
    return $lines
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ComposeFile = Join-Path $RepoRoot "compose.yaml"
$HealthUrl = "http://127.0.0.1:8765/health"

Write-Host "vox - start"
Write-Info "repository: $RepoRoot"

if (-not (Test-Path -LiteralPath $ComposeFile)) {
    Stop-WithError "$ComposeFile is missing. The repository checkout is incomplete."
}

Write-Step "Checking Docker"

$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
    Stop-WithError "The 'docker' command was not found. Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and start it, then run this script again."
}
$dockerExit = Invoke-Native -FilePath $docker.Source -Quiet -Arguments @("version")
if ($dockerExit -ne 0) {
    Stop-WithError "The Docker engine is not responding. Start Docker Desktop, wait until the whale icon stops animating, then run this script again."
}
Write-Info "docker engine: OK"

Write-Step "Checking .env"

$envFile = Join-Path $RepoRoot ".env"
$envExample = Join-Path $RepoRoot ".env.example"
if (Test-Path -LiteralPath $envFile) {
    Write-Info ".env found - left untouched."
} elseif (Test-Path -LiteralPath $envExample) {
    Copy-Item -LiteralPath $envExample -Destination $envFile
    Write-Info "created .env from .env.example (all defaults). Edit it to change the LLM or the STT model."
} else {
    Write-Warn ".env and .env.example are both missing; the backend will run on its built-in defaults."
}

Write-Step "Starting the backend"

$needsBuild = $Build.IsPresent
if (-not $needsBuild) {
    $imageExit = Invoke-Native -FilePath $docker.Source -Quiet -Arguments @("image", "inspect", "vox-backend:latest")
    if ($imageExit -ne 0) {
        Write-Info "image vox-backend:latest not found - building it (the first build takes a few minutes)."
        $needsBuild = $true
    }
}

$composeArgs = @("compose", "-f", $ComposeFile, "up", "-d")
if ($needsBuild) {
    $composeArgs += "--build"
}
$upExit = Invoke-Native -FilePath $docker.Source -Arguments $composeArgs
if ($upExit -ne 0) {
    Stop-WithError "'docker compose up -d' failed with exit code $upExit. Read the output above, then inspect the logs with: docker compose -f '$ComposeFile' logs backend"
}

Write-Step "Waiting for the backend to become ready (timeout ${TimeoutSeconds}s)"

$started = Get-Date
$deadline = $started.AddSeconds($TimeoutSeconds)
$lastStatus = ""
$lastReport = $started.AddSeconds(-60)
$degradedSince = $null
$health = $null
$status = "unreachable"

while ((Get-Date) -lt $deadline) {
    $health = Get-Health -Uri $HealthUrl
    if ($null -eq $health) {
        $status = "unreachable"
    } else {
        $status = Format-Field $health.status
    }

    $elapsed = [int]((Get-Date) - $started).TotalSeconds
    if ($status -ne $lastStatus -or ((Get-Date) - $lastReport).TotalSeconds -ge 30) {
        Write-Info ("[{0,4}s] status: {1}" -f $elapsed, $status)
        $lastReport = Get-Date
    }
    $lastStatus = $status

    if ($status -eq "ready") {
        break
    }
    if ($status -eq "degraded") {
        if ($null -eq $degradedSince) {
            $degradedSince = Get-Date
        } elseif (((Get-Date) - $degradedSince).TotalSeconds -ge 30) {
            break
        }
    } else {
        $degradedSince = $null
    }

    Start-Sleep -Seconds 3
}

if ($status -eq "ready") {
    Write-Host ""
    Write-Host "Backend ready." -ForegroundColor Green
    Write-Info ("STT : model {0} / device {1} / compute {2}" -f (Format-Field $health.stt.model), (Format-Field $health.stt.device), (Format-Field $health.stt.compute_type))
    Write-Info ("LLM : model {0} / base_url {1}" -f (Format-Field $health.llm.model), (Format-Field $health.llm.base_url))
    Write-Info ("GPU : cuda_available {0} / device_count {1}" -f (Format-Field $health.gpu.cuda_available), (Format-Field $health.gpu.device_count))
    Write-Info ("version {0}, uptime {1}s" -f (Format-Field $health.version), [int]$health.uptime_s)
    if ("$($health.stt.device)" -eq "cpu") {
        Write-Warn "STT is running on the CPU. Transcription will be slow. Check that Docker Desktop has GPU support enabled and that 'docker run --gpus all' works."
    }
} elseif ($status -eq "degraded") {
    Write-Host ""
    Write-Warn "The backend is DEGRADED - it answers HTTP but part of the pipeline is down."
    Write-Info ("stt.ready    : {0}" -f (Format-Field $health.stt.ready))
    Write-Info ("stt.error    : {0}" -f (Format-Field $health.stt.error))
    Write-Info ("llm.ready    : {0}" -f (Format-Field $health.llm.ready))
    Write-Info ("llm.error    : {0}" -f (Format-Field $health.llm.error))
    Write-Info ("llm.base_url : {0}" -f (Format-Field $health.llm.base_url))
    Write-Host ""
    Write-Warn "Modes that do not need the LLM may still work. A missing LLM is usually a model server that is not running: start it (for example 'ollama serve' plus 'ollama pull qwen2.5:7b-instruct') or fix LLM_BASE_URL in .env."
    Write-Info "Backend logs: docker compose -f '$ComposeFile' logs -f backend"
} else {
    Stop-WithError "The backend did not become ready within ${TimeoutSeconds}s (last status: $status). Inspect it with: docker compose -f '$ComposeFile' logs -f backend   -- or re-run with a longer -TimeoutSeconds if the model is still downloading."
}

if ($NoClient) {
    Write-Step "Companion"
    Write-Info "-NoClient was passed - the Windows companion was not started."
    Write-Host ""
    exit 0
}

Write-Step "Starting the Windows companion"

$running = Get-CompanionProcess
if ($running.Count -gt 0) {
    $runningPids = ($running | ForEach-Object { $_.ProcessId }) -join ", "
    Write-Info "already running (PID $runningPids) - not starting a second instance."
} else {
    $interpreter = Join-Path $RepoRoot ".venv\Scripts\pythonw.exe"
    if (-not (Test-Path -LiteralPath $interpreter)) {
        $interpreter = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    }
    if (-not (Test-Path -LiteralPath $interpreter)) {
        Stop-WithError "No Python interpreter in $RepoRoot\.venv. Run scripts\install-client.ps1 first."
    }

    # Start-Process joins -ArgumentList with bare spaces and quotes nothing, so any path
    # containing a space would reach the companion as several separate arguments.
    $clientArgs = @("-m", "vox_client.main")
    $clientConfig = Join-Path $RepoRoot "config\client.yaml"
    if (Test-Path -LiteralPath $clientConfig) {
        $clientArgs += @("--config", ('"' + $clientConfig + '"'))
    }

    Start-Process -FilePath $interpreter -ArgumentList $clientArgs -WorkingDirectory $RepoRoot -WindowStyle Hidden
    Start-Sleep -Seconds 2

    $running = Get-CompanionProcess
    if ($running.Count -gt 0) {
        $runningPids = ($running | ForEach-Object { $_.ProcessId }) -join ", "
        Write-Info "companion started with $(Split-Path -Leaf $interpreter) (PID $runningPids)"
    } else {
        Write-Warn "The companion exited immediately. Run it in a console to see why: $RepoRoot\.venv\Scripts\python.exe -m vox_client.main"
    }
}

$configForHotkeys = Join-Path $RepoRoot "config\client.yaml"
if (-not (Test-Path -LiteralPath $configForHotkeys)) {
    $configForHotkeys = Join-Path $RepoRoot "config\client.example.yaml"
}
$bindings = @(Get-HotkeyBinding -Path $configForHotkeys)

Write-Host ""
Write-Host "Push-to-talk hotkeys (hold the combo to record, release the trigger key to send):"
if ($bindings.Count -gt 0) {
    foreach ($binding in $bindings) {
        Write-Info $binding
    }
} else {
    Write-Info "record       ctrl+alt+space"
    Write-Info "dictate      ctrl+alt+d"
    Write-Info "cancel       esc"
}
Write-Host ""
Write-Host "The result is pasted into the focused window. Enter is never pressed for you."
Write-Host "Stop everything with: powershell -ExecutionPolicy Bypass -File scripts\stop.ps1"
Write-Host ""
exit 0
