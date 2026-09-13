<#
.SYNOPSIS
    Verifies that a running vox backend is healthy end to end.

.DESCRIPTION
    Runs eight checks against the backend and prints PASS, FAIL or SKIP for each:

      a) GET /health is reachable
      b) /health reports status "ready"
      c) stt.ready is true (warns loudly when the model landed on the CPU)
      d) gpu.cuda_available is true
      e) GET /v1/modes returns context, dictation, clean and task
      f) llm.ready is true (skipped with -SkipLlm)
      g) POST /v1/process without audio is rejected with HTTP 400 or 422
      h) POST /v1/process with a generated WAV runs the whole pipeline

    The WAV fixture for check (h) is built in-script: two seconds of quiet 440 Hz
    tone, 16 kHz mono 16-bit PCM. A tone contains no speech, so both HTTP 200 with
    a non-empty output and HTTP 422 empty_transcript count as a pass - either way
    the audio reached Whisper and came back through the API.

    Nothing is written outside $env:TEMP, and the fixture is deleted afterwards.

.PARAMETER BaseUrl
    Backend base URL. Default http://127.0.0.1:8765

.PARAMETER SkipLlm
    Skip the LLM readiness check, for a dictation-only setup with no model server.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\smoke-test.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\smoke-test.ps1 -SkipLlm

.NOTES
    The exit code is the number of failed checks: 0 means everything passed.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8765",
    [switch]$SkipLlm
)

$ErrorActionPreference = "Stop"

$script:Failures = 0

function Write-Result {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][ValidateSet("PASS", "FAIL", "SKIP")][string]$Status,
        [string]$Detail = ""
    )
    $color = "Green"
    if ($Status -eq "FAIL") {
        $color = "Red"
        $script:Failures = $script:Failures + 1
    } elseif ($Status -eq "SKIP") {
        $color = "Yellow"
    }
    $line = "[{0}] {1}" -f $Status, $Name
    if ($Detail -ne "") {
        $line = "$line - $Detail"
    }
    Write-Host $line -ForegroundColor $color
}

function Write-Warn {
    param([string]$Message)
    Write-Host "       WARNING: $Message" -ForegroundColor Yellow
}

function Write-Note {
    param([string]$Message)
    Write-Host "       $Message" -ForegroundColor DarkGray
}

function Invoke-Http {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [string]$Method = "Get",
        [byte[]]$BodyBytes = $null,
        [string]$ContentType = $null,
        [hashtable]$Form = $null,
        [int]$TimeoutSec = 180
    )

    $parameters = @{
        Uri             = $Uri
        Method          = $Method
        TimeoutSec      = $TimeoutSec
        UseBasicParsing = $true
        ErrorAction     = "Stop"
    }
    if ($null -ne $Form) {
        $parameters["Form"] = $Form
    } elseif ($null -ne $BodyBytes) {
        $parameters["Body"] = $BodyBytes
        $parameters["ContentType"] = $ContentType
    }

    try {
        $response = Invoke-WebRequest @parameters
        return [pscustomobject]@{
            StatusCode = [int]$response.StatusCode
            Content    = [string]$response.Content
            Error      = $null
        }
    } catch {
        $failure = $_
        $statusCode = 0
        $content = ""
        $webResponse = $null
        if ($null -ne $failure.Exception -and ($failure.Exception | Get-Member -Name "Response" -MemberType Property)) {
            $webResponse = $failure.Exception.Response
        }
        if ($null -ne $webResponse) {
            try {
                $statusCode = [int]$webResponse.StatusCode
            } catch {
                $statusCode = 0
            }
        }
        if ($null -ne $failure.ErrorDetails -and $failure.ErrorDetails.Message) {
            $content = [string]$failure.ErrorDetails.Message
        } elseif ($null -ne $webResponse -and ($webResponse | Get-Member -Name "GetResponseStream" -MemberType Method)) {
            try {
                $reader = New-Object System.IO.StreamReader($webResponse.GetResponseStream())
                $content = $reader.ReadToEnd()
                $reader.Close()
            } catch {
                $content = ""
            }
        }
        return [pscustomobject]@{
            StatusCode = $statusCode
            Content    = $content
            Error      = $failure.Exception.Message
        }
    }
}

function ConvertFrom-ResponseJson {
    param([string]$Content)
    if ([string]::IsNullOrWhiteSpace($Content)) {
        return $null
    }
    try {
        return $Content | ConvertFrom-Json
    } catch {
        return $null
    }
}

function New-ToneWav {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$SampleRate = 16000,
        [double]$Seconds = 2.0,
        [double]$Frequency = 440.0,
        [double]$Amplitude = 0.05
    )

    $frameCount = [int]($SampleRate * $Seconds)
    $dataBytes = $frameCount * 2
    $pcm = New-Object byte[] $dataBytes
    for ($i = 0; $i -lt $frameCount; $i++) {
        $sample = [int16][math]::Round($Amplitude * 32767.0 * [math]::Sin(2.0 * [math]::PI * $Frequency * $i / $SampleRate))
        $pair = [System.BitConverter]::GetBytes($sample)
        $pcm[$i * 2] = $pair[0]
        $pcm[$i * 2 + 1] = $pair[1]
    }

    $ascii = [System.Text.Encoding]::ASCII
    $stream = New-Object System.IO.MemoryStream
    $writer = New-Object System.IO.BinaryWriter($stream)
    try {
        $writer.Write($ascii.GetBytes("RIFF"))
        $writer.Write([int](36 + $dataBytes))
        $writer.Write($ascii.GetBytes("WAVE"))
        $writer.Write($ascii.GetBytes("fmt "))
        $writer.Write([int]16)
        $writer.Write([int16]1)
        $writer.Write([int16]1)
        $writer.Write([int]$SampleRate)
        $writer.Write([int]($SampleRate * 2))
        $writer.Write([int16]2)
        $writer.Write([int16]16)
        $writer.Write($ascii.GetBytes("data"))
        $writer.Write([int]$dataBytes)
        $writer.Write($pcm)
        $writer.Flush()
        [System.IO.File]::WriteAllBytes($Path, $stream.ToArray())
    } finally {
        $writer.Dispose()
        $stream.Dispose()
    }
}

# Windows PowerShell 5.1 has no Invoke-WebRequest -Form, so multipart bodies are assembled
# by hand here; PowerShell 7 takes the -Form path in the callers below.
function New-MultipartBody {
    param(
        [Parameter(Mandatory = $true)][string]$Boundary,
        [hashtable]$Fields = @{},
        [string]$FilePath = "",
        [string]$FileField = "audio",
        [string]$FileName = "audio.wav",
        [string]$FileContentType = "audio/wav"
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    $stream = New-Object System.IO.MemoryStream
    try {
        foreach ($name in $Fields.Keys) {
            $header = "--$Boundary`r`nContent-Disposition: form-data; name=`"$name`"`r`n`r`n$($Fields[$name])`r`n"
            $bytes = $encoding.GetBytes($header)
            $stream.Write($bytes, 0, $bytes.Length)
        }
        if ($FilePath -ne "") {
            $header = "--$Boundary`r`nContent-Disposition: form-data; name=`"$FileField`"; filename=`"$FileName`"`r`nContent-Type: $FileContentType`r`n`r`n"
            $bytes = $encoding.GetBytes($header)
            $stream.Write($bytes, 0, $bytes.Length)
            $fileBytes = [System.IO.File]::ReadAllBytes($FilePath)
            $stream.Write($fileBytes, 0, $fileBytes.Length)
            $newline = $encoding.GetBytes("`r`n")
            $stream.Write($newline, 0, $newline.Length)
        }
        $closing = $encoding.GetBytes("--$Boundary--`r`n")
        $stream.Write($closing, 0, $closing.Length)
        return $stream.ToArray()
    } finally {
        $stream.Dispose()
    }
}

function Invoke-ProcessRequest {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [string]$Mode = "dictation",
        [string]$WavPath = ""
    )

    if ($PSVersionTable.PSVersion.Major -ge 6) {
        $form = @{ mode = $Mode }
        if ($WavPath -ne "") {
            $form["audio"] = Get-Item -LiteralPath $WavPath
        }
        return Invoke-Http -Uri $Uri -Method "Post" -Form $form
    }

    $boundary = [Guid]::NewGuid().ToString("N")
    $body = New-MultipartBody -Boundary $boundary -Fields @{ mode = $Mode } -FilePath $WavPath
    return Invoke-Http -Uri $Uri -Method "Post" -BodyBytes $body -ContentType "multipart/form-data; boundary=$boundary"
}

$BaseUrl = $BaseUrl.TrimEnd("/")

Write-Host "vox - smoke test"
Write-Host "    backend    : $BaseUrl"
Write-Host "    powershell : $($PSVersionTable.PSVersion)"
Write-Host ""

$health = $null

$healthResponse = Invoke-Http -Uri "$BaseUrl/health" -Method "Get" -TimeoutSec 15
if ($healthResponse.StatusCode -eq 200) {
    $health = ConvertFrom-ResponseJson -Content $healthResponse.Content
    if ($null -eq $health) {
        Write-Result -Name "a) GET /health reachable" -Status "FAIL" -Detail "the response body is not valid JSON"
    } else {
        Write-Result -Name "a) GET /health reachable" -Status "PASS" -Detail "HTTP 200"
    }
} else {
    $detail = "no HTTP response"
    if ($healthResponse.StatusCode -ne 0) {
        $detail = "HTTP $($healthResponse.StatusCode)"
    }
    Write-Result -Name "a) GET /health reachable" -Status "FAIL" -Detail $detail
    Write-Note "Start the backend with: powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -NoClient"
    if ($healthResponse.Error) {
        Write-Note $healthResponse.Error
    }
}

if ($null -eq $health) {
    Write-Result -Name "b) status is ready" -Status "FAIL" -Detail "no health payload"
    Write-Result -Name "c) STT ready" -Status "FAIL" -Detail "no health payload"
    Write-Result -Name "d) GPU cuda_available" -Status "FAIL" -Detail "no health payload"
} else {
    $status = "$($health.status)"
    if ($status -eq "ready") {
        Write-Result -Name "b) status is ready" -Status "PASS" -Detail "status=ready, uptime $([int]$health.uptime_s)s"
    } else {
        Write-Result -Name "b) status is ready" -Status "FAIL" -Detail "status=$status"
    }

    $sttDevice = "$($health.stt.device)"
    if ($health.stt.ready -eq $true) {
        Write-Result -Name "c) STT ready" -Status "PASS" -Detail "model=$($health.stt.model) device=$sttDevice compute=$($health.stt.compute_type)"
        if ($sttDevice -eq "cpu") {
            Write-Warn "STT fell back to the CPU. Transcription will be several times slower than on the GPU. Check that Docker Desktop exposes the NVIDIA runtime and that 'docker run --gpus all' works."
        }
    } else {
        Write-Result -Name "c) STT ready" -Status "FAIL" -Detail "device=$sttDevice error=$($health.stt.error)"
    }

    if ($health.gpu.cuda_available -eq $true) {
        Write-Result -Name "d) GPU cuda_available" -Status "PASS" -Detail "device_count=$($health.gpu.device_count)"
    } else {
        Write-Result -Name "d) GPU cuda_available" -Status "FAIL" -Detail "CTranslate2 sees no CUDA device inside the container"
    }
}

$expectedModes = @("clean", "context", "dictation", "task")
$modesResponse = Invoke-Http -Uri "$BaseUrl/v1/modes" -Method "Get" -TimeoutSec 15
if ($modesResponse.StatusCode -ne 200) {
    $detail = "no HTTP response"
    if ($modesResponse.StatusCode -ne 0) {
        $detail = "HTTP $($modesResponse.StatusCode)"
    }
    Write-Result -Name "e) GET /v1/modes" -Status "FAIL" -Detail $detail
} else {
    $modesPayload = ConvertFrom-ResponseJson -Content $modesResponse.Content
    $modeNames = @()
    if ($null -ne $modesPayload -and $null -ne $modesPayload.modes) {
        $modeNames = @($modesPayload.modes | ForEach-Object { "$($_.name)" })
    }
    $missing = @($expectedModes | Where-Object { $modeNames -notcontains $_ })
    if ($missing.Count -eq 0) {
        Write-Result -Name "e) GET /v1/modes" -Status "PASS" -Detail ("{0} modes: {1}" -f $modeNames.Count, (($modeNames | Sort-Object) -join ", "))
        $extra = @($modeNames | Where-Object { $expectedModes -notcontains $_ })
        if ($extra.Count -gt 0) {
            Write-Note ("extra modes present: {0}" -f ($extra -join ", "))
        }
    } else {
        Write-Result -Name "e) GET /v1/modes" -Status "FAIL" -Detail ("missing: {0}; got: {1}" -f ($missing -join ", "), (($modeNames | Sort-Object) -join ", "))
    }
}

if ($SkipLlm) {
    Write-Result -Name "f) LLM ready" -Status "SKIP" -Detail "-SkipLlm was passed"
} elseif ($null -eq $health) {
    Write-Result -Name "f) LLM ready" -Status "FAIL" -Detail "no health payload"
} elseif ($health.llm.ready -eq $true) {
    Write-Result -Name "f) LLM ready" -Status "PASS" -Detail "model=$($health.llm.model) base_url=$($health.llm.base_url)"
} else {
    Write-Result -Name "f) LLM ready" -Status "FAIL" -Detail "base_url=$($health.llm.base_url) error=$($health.llm.error)"
    Write-Note "Start your model server (for example 'ollama serve' plus 'ollama pull qwen2.5:7b-instruct') or correct LLM_BASE_URL in .env."
}

$processUri = "$BaseUrl/v1/process"

$noAudioResponse = Invoke-ProcessRequest -Uri $processUri -Mode "dictation"
if ($noAudioResponse.StatusCode -eq 422 -or $noAudioResponse.StatusCode -eq 400) {
    Write-Result -Name "g) POST /v1/process without audio is rejected" -Status "PASS" -Detail "HTTP $($noAudioResponse.StatusCode)"
} else {
    $detail = "no HTTP response"
    if ($noAudioResponse.StatusCode -ne 0) {
        $detail = "HTTP $($noAudioResponse.StatusCode), expected 400 or 422"
    }
    Write-Result -Name "g) POST /v1/process without audio is rejected" -Status "FAIL" -Detail $detail
}

$wavPath = Join-Path $env:TEMP ("vox-smoke-{0}.wav" -f ([Guid]::NewGuid().ToString("N")))
try {
    New-ToneWav -Path $wavPath
    $wavSize = (Get-Item -LiteralPath $wavPath).Length
    Write-Note "fixture: 2.0s of 440 Hz tone, 16 kHz mono 16-bit PCM, $wavSize bytes"

    $audioResponse = Invoke-ProcessRequest -Uri $processUri -Mode "dictation" -WavPath $wavPath
    $payload = ConvertFrom-ResponseJson -Content $audioResponse.Content

    if ($audioResponse.StatusCode -eq 200) {
        $output = ""
        if ($null -ne $payload) {
            $output = "$($payload.output)"
        }
        if ($output.Trim() -ne "") {
            Write-Result -Name "h) POST /v1/process with a WAV fixture" -Status "PASS" -Detail "HTTP 200 with a non-empty output - the full pipeline ran"
            Write-Note "request_id=$($payload.request_id) timings_ms=$($payload.timings_ms.total)"
        } else {
            Write-Result -Name "h) POST /v1/process with a WAV fixture" -Status "FAIL" -Detail "HTTP 200 but the output field is empty"
        }
    } elseif ($audioResponse.StatusCode -eq 422) {
        $errorCode = ""
        if ($null -ne $payload) {
            $errorCode = "$($payload.error)"
        }
        if ($errorCode -eq "empty_transcript") {
            Write-Result -Name "h) POST /v1/process with a WAV fixture" -Status "PASS" -Detail "HTTP 422 empty_transcript - expected for a speechless tone; STT ran and the API answered"
        } else {
            Write-Result -Name "h) POST /v1/process with a WAV fixture" -Status "FAIL" -Detail "HTTP 422 error=$errorCode (expected empty_transcript)"
        }
    } elseif ($SkipLlm -and $null -ne $payload -and "$($payload.error)" -like "llm_*") {
        # Every mode sets requires_llm, so a fixture that Whisper did not hear as silence
        # reaches the LLM step; with -SkipLlm that is a skipped stage, not a failure.
        Write-Result -Name "h) POST /v1/process with a WAV fixture" -Status "PASS" -Detail "HTTP $($audioResponse.StatusCode) $($payload.error) - STT produced a transcript and -SkipLlm excuses the LLM stage"
    } else {
        $detail = "no HTTP response"
        if ($audioResponse.StatusCode -ne 0) {
            $detail = "HTTP $($audioResponse.StatusCode)"
            if ($null -ne $payload -and $payload.error) {
                $detail = "$detail error=$($payload.error)"
            }
        }
        Write-Result -Name "h) POST /v1/process with a WAV fixture" -Status "FAIL" -Detail $detail
    }
} finally {
    if (Test-Path -LiteralPath $wavPath) {
        Remove-Item -LiteralPath $wavPath -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
if ($script:Failures -eq 0) {
    Write-Host "All checks passed." -ForegroundColor Green
} else {
    Write-Host "$($script:Failures) check(s) failed." -ForegroundColor Red
    Write-Host "Backend logs: docker compose logs -f backend"
}
Write-Host ""
exit $script:Failures
