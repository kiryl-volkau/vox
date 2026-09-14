<#
.SYNOPSIS
    Shows what the backend actually did with one dictation.

.DESCRIPTION
    Reads the JSON trace files the backend writes when tracing is switched on and prints
    one of them as a report: which project file was picked up, what Whisper heard, what
    prompt was assembled, what the model returned, what the cleanup changed, and what was
    finally produced.

    Tracing is OFF by default - it writes transcripts and prompts to disk, which the product
    otherwise never does. When no traces are found this script says how to switch it on.

    The script works from any working directory: the repository root is resolved from the
    script's own location.

.PARAMETER TraceDir
    Directory holding the trace files, absolute or relative to the repository root.
    Default "traces", which is what compose.yaml mounts into the container as /traces.

.PARAMETER Last
    How many of the most recent traces to show. Default 1 for the report, 20 for -List.

.PARAMETER RequestId
    Show the trace of this request id instead of the most recent one. A unique prefix is
    enough; the id is printed by -List and by the plugin's notification.

.PARAMETER Full
    Print transcripts, prompts and output in full. Without it long text is cut short.

.PARAMETER List
    Print a table of recent traces instead of a report.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\show-trace.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\show-trace.ps1 -List -Last 20

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\show-trace.ps1 -RequestId 8f3a1c2e -Full

.NOTES
    Exit code 0 on success and when tracing is simply off, 1 when the requested trace does
    not exist. A malformed trace file is skipped with a warning, never an exception.
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$TraceDir = "traces",
    [ValidateRange(1, 1000)]
    [int]$Last = 1,
    [string]$RequestId,
    [switch]$Full,
    [switch]$List
)

$ErrorActionPreference = "Stop"

$MaxTextChars = 800
$ListDefaultCount = 20

# Traces of Russian speech are UTF-8; without this the console prints them as question marks.
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    Write-Verbose "could not switch the console to UTF-8: $($_.Exception.Message)"
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Section {
    param([string]$Message)
    Write-Host ""
    Write-Host "  -- $Message" -ForegroundColor DarkCyan
}

function Write-Info {
    param([string]$Message)
    Write-Host "    $Message"
}

function Write-Warn {
    param([string]$Message)
    Write-Host "WARNING: $Message" -ForegroundColor Yellow
}

function Get-Prop {
    param($Object, [string]$Name)
    if ($null -eq $Object) {
        return $null
    }
    if ($Object.PSObject.Properties.Name -contains $Name) {
        return $Object.$Name
    }
    return $null
}

function Format-Value {
    param($Value, [string]$Fallback = "-")
    if ($null -eq $Value) {
        return $Fallback
    }
    if ($Value -is [bool]) {
        if ($Value) {
            return "yes"
        }
        return "no"
    }
    $text = [string]$Value
    if ($text.Trim().Length -eq 0) {
        return $Fallback
    }
    return $text
}

function Write-Field {
    param([string]$Name, $Value)
    Write-Host ("    {0,-16}{1}" -f $Name, (Format-Value $Value))
}

function Write-TextField {
    param([string]$Name, $Value)
    if ($null -eq $Value -or [string]$Value -eq "") {
        Write-Field $Name $null
        return
    }
    $text = [string]$Value
    Write-Host ("    {0,-16}({1} chars)" -f $Name, $text.Length)
    $shown = $text
    $hidden = 0
    if (-not $Full -and $text.Length -gt $MaxTextChars) {
        $shown = $text.Substring(0, $MaxTextChars)
        $hidden = $text.Length - $MaxTextChars
    }
    foreach ($line in ($shown -split "`r?`n")) {
        Write-Host ("      " + $line)
    }
    if ($hidden -gt 0) {
        Write-Host ("      [... $hidden more characters - rerun with -Full]") -ForegroundColor DarkGray
    }
}

function Read-Trace {
    param([System.IO.FileInfo]$File)
    try {
        $raw = Get-Content -LiteralPath $File.FullName -Raw -Encoding UTF8 -ErrorAction Stop
    } catch {
        Write-Warn "cannot read $($File.Name): $($_.Exception.Message)"
        return $null
    }
    if ([string]::IsNullOrWhiteSpace($raw)) {
        Write-Warn "$($File.Name) is empty, skipping"
        return $null
    }
    try {
        return ConvertFrom-Json -InputObject $raw -ErrorAction Stop
    } catch {
        Write-Warn "$($File.Name) is not valid JSON, skipping: $($_.Exception.Message)"
        return $null
    }
}

function Select-Traces {
    param([System.IO.FileInfo[]]$Files, [int]$Count)
    $picked = @()
    foreach ($file in $Files) {
        if ($picked.Count -ge $Count) {
            break
        }
        $trace = Read-Trace $file
        if ($null -eq $trace) {
            continue
        }
        $picked += [pscustomobject]@{ File = $file; Trace = $trace }
    }
    # No leading comma: the caller already wraps the result in @(). Returning ", $picked" made
    # this an array holding one array, which only shows up with two or more traces - with a
    # single one PowerShell unwraps member access and the bug hides.
    return $picked
}

function Get-TraceTime {
    param($Trace, [System.IO.FileInfo]$File)
    $started = Get-Prop $Trace "started_at"
    if ($started) {
        $parsed = [datetime]::MinValue
        $styles = [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor [System.Globalization.DateTimeStyles]::AssumeUniversal
        $culture = [System.Globalization.CultureInfo]::InvariantCulture
        if ([datetime]::TryParse([string]$started, $culture, $styles, [ref]$parsed)) {
            return $parsed.ToString("yyyy-MM-dd HH:mm:ss")
        }
        return [string]$started
    }
    return $File.LastWriteTimeUtc.ToString("yyyy-MM-dd HH:mm:ss")
}

function Show-TracingIsOff {
    param([string]$Directory)
    Write-Host ""
    Write-Host "No traces in $Directory"
    Write-Host ""
    Write-Host "Tracing is off by default. It writes transcripts and prompts to disk, which the"
    Write-Host "backend otherwise never does, so you have to ask for it."
    Write-Host ""
    Write-Host "To switch it on:"
    Write-Info "1. add to .env in the repository root:"
    Write-Info "       VOX_TRACE_DIR=/traces"
    Write-Info "       VOX_TRACE_KEEP=200"
    Write-Info "2. restart the backend:"
    Write-Info "       docker compose up -d"
    Write-Info "3. dictate once, then run this script again."
    Write-Host ""
    Write-Host "compose.yaml already mounts .\traces into the container at /traces, so the files"
    Write-Host "land in the repository. Remove VOX_TRACE_DIR and restart to switch tracing back off,"
    Write-Host "and delete the directory when you are done with it."
    Write-Host ""
}

function Show-Header {
    param($Trace, [System.IO.FileInfo]$File)
    Write-Step $File.Name
    Write-Field "request" (Get-Prop $Trace "request_id")
    Write-Field "started" (Get-TraceTime $Trace $File)
    Write-Field "endpoint" (Get-Prop $Trace "endpoint")
    $status = Format-Value (Get-Prop $Trace "status")
    if ($status -eq "error") {
        $failure = Get-Prop $Trace "error"
        $type = Format-Value (Get-Prop $failure "type") "unknown"
        $message = Format-Value (Get-Prop $failure "message") ""
        Write-Host ("    {0,-16}{1}" -f "status", "error - $type $message") -ForegroundColor Red
    } else {
        Write-Field "status" $status
    }
}

function Show-Client {
    param($Trace)
    Write-Section "CLIENT"
    $client = Get-Prop $Trace "client"
    if ($null -eq $client) {
        Write-Info "not recorded"
        return
    }
    Write-Field "id" (Get-Prop $client "id")
    Write-Field "version" (Get-Prop $client "version")
    Write-Field "audio s" (Get-Prop $client "audio_seconds")
}

function Show-Audio {
    param($Trace)
    Write-Section "AUDIO"
    $audio = Get-Prop $Trace "audio"
    if ($null -eq $audio) {
        Write-Info "no audio for this endpoint"
        return
    }
    Write-Field "bytes" (Get-Prop $audio "bytes")
    Write-Field "decoded s" (Get-Prop $audio "decoded_seconds")
}

function Show-Stt {
    param($Trace)
    Write-Section "STT"
    $stt = Get-Prop $Trace "stt"
    if ($null -eq $stt) {
        Write-Info "no transcription for this endpoint"
        return
    }
    Write-Field "model" (Get-Prop $stt "model")
    Write-Field "device" ("{0} / {1}" -f (Format-Value (Get-Prop $stt "device")), (Format-Value (Get-Prop $stt "compute_type")))
    Write-Field "language" ("{0} (p={1})" -f (Format-Value (Get-Prop $stt "language")), (Format-Value (Get-Prop $stt "language_probability")))
    Write-Field "duration ms" (Get-Prop $stt "duration_ms")
    Write-TextField "transcript" (Get-Prop $stt "transcript")
}

function Show-Project {
    param($Trace)
    Write-Section "PROJECT"
    $project = Get-Prop $Trace "project"
    $name = Get-Prop $project "name"
    if ($null -eq $project -or $null -eq $name) {
        Write-Info "no project context was sent with this request"
        Write-Info "the plugin logs why in Help | Show Log in Explorer"
        return
    }
    Write-Field "name" $name
    Write-Field "file" ("{0}\.vox.md" -f $name)
    Write-Field "received" ("{0} bytes" -f (Format-Value (Get-Prop $project "received_bytes") "?"))
    Write-Field "used" ("{0} bytes" -f (Format-Value (Get-Prop $project "used_bytes") "?"))
    Write-Field "truncated" (Get-Prop $project "truncated")
    Write-TextField "text" (Get-Prop $project "text")
}

function Show-Prompt {
    param($Trace)
    Write-Section "PROMPT"
    $prompt = Get-Prop $Trace "prompt"
    $glossary = Get-Prop $Trace "glossary"
    if ($null -ne $glossary) {
        Write-Field "glossary" ("{0} entries, {1} chars in the prompt" -f (Format-Value (Get-Prop $glossary "entries") "?"), (Format-Value (Get-Prop $glossary "prompt_block_chars") "?"))
    }
    if ($null -eq $prompt) {
        Write-Info "no prompt was assembled"
        return
    }
    Write-Field "system chars" (Get-Prop $prompt "system_chars")
    Write-Field "user chars" (Get-Prop $prompt "user_chars")
    Write-TextField "system" (Get-Prop $prompt "system")
    Write-TextField "user" (Get-Prop $prompt "user")
}

function Show-Llm {
    param($Trace)
    Write-Section "LLM"
    $llm = Get-Prop $Trace "llm"
    if ($null -eq $llm) {
        Write-Info "the model was not called for this endpoint"
        return
    }
    Write-Field "model" (Get-Prop $llm "model")
    Write-Field "base url" (Get-Prop $llm "base_url")
    Write-Field "temperature" (Get-Prop $llm "temperature")
    Write-Field "duration ms" (Get-Prop $llm "duration_ms")
    Write-Field "cleanup changed" (Get-Prop $llm "cleanup_changed")
    Write-TextField "raw output" (Get-Prop $llm "raw_output")
    if (Get-Prop $llm "cleanup_changed") {
        Write-TextField "cleaned" (Get-Prop $llm "cleaned_output")
    }
}

function Show-Output {
    param($Trace)
    Write-Section "OUTPUT"
    $output = Get-Prop $Trace "output"
    if ($null -eq $output) {
        Write-Info "nothing was produced"
        return
    }
    Write-Field "chars" (Get-Prop $output "chars")
    Write-TextField "text" (Get-Prop $output "text")
}

function Show-Timings {
    param($Trace)
    Write-Section "TIMINGS"
    $timings = Get-Prop $Trace "timings_ms"
    if ($null -eq $timings) {
        Write-Info "not recorded"
        return
    }
    Write-Field "transcription" (Get-Prop $timings "transcription")
    Write-Field "llm" (Get-Prop $timings "llm")
    Write-Field "total" (Get-Prop $timings "total")
}

function Show-Trace {
    param($Trace, [System.IO.FileInfo]$File)
    Show-Header $Trace $File
    Show-Client $Trace
    Show-Audio $Trace
    Show-Stt $Trace
    Show-Project $Trace
    Show-Prompt $Trace
    Show-Llm $Trace
    Show-Output $Trace
    Show-Timings $Trace
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Directory = $TraceDir
if (-not [System.IO.Path]::IsPathRooted($Directory)) {
    $Directory = Join-Path $RepoRoot $TraceDir
}

$files = @()
if (Test-Path -LiteralPath $Directory) {
    # The file names start with a UTC timestamp, so a lexical sort is a chronological one.
    $files = @(Get-ChildItem -LiteralPath $Directory -File |
        Where-Object { $_.Extension -eq ".json" } |
        Sort-Object -Property Name -Descending)
}

if ($files.Count -eq 0) {
    Show-TracingIsOff $Directory
    exit 0
}

$total = $files.Count

if ($RequestId) {
    $files = @($files | Where-Object { $_.BaseName -like "*$RequestId*" })
    if ($files.Count -eq 0) {
        Write-Host ""
        Write-Warn "no trace in $Directory belongs to request id '$RequestId'"
        Write-Info "list what is there: powershell -ExecutionPolicy Bypass -File scripts\show-trace.ps1 -List"
        Write-Host ""
        exit 1
    }
}

$count = $Last
if ($List -and -not $PSBoundParameters.ContainsKey("Last")) {
    $count = $ListDefaultCount
}

if ($List) {
    Write-Host ""
    Write-Host "vox - traces in $Directory (times are UTC)"
}

$picked = @(Select-Traces $files $count)
if ($picked.Count -eq 0) {
    Write-Host ""
    Write-Warn "nothing in $Directory could be read as a trace"
    Write-Host ""
    exit 0
}

if ($List) {
    $rows = @()
    foreach ($entry in $picked) {
        $rows += [pscustomobject]@{
            Time      = (Get-TraceTime $entry.Trace $entry.File)
            RequestId = (Format-Value (Get-Prop $entry.Trace "request_id"))
            Project   = (Format-Value (Get-Prop (Get-Prop $entry.Trace "project") "name"))
            Status    = (Format-Value (Get-Prop $entry.Trace "status"))
            TotalMs   = (Format-Value (Get-Prop (Get-Prop $entry.Trace "timings_ms") "total"))
        }
    }
    $rows | Format-Table -AutoSize | Out-Host
    Write-Host "Show one of them: scripts\show-trace.ps1 -RequestId <id> [-Full]"
    Write-Host ""
    exit 0
}

foreach ($entry in $picked) {
    Show-Trace $entry.Trace $entry.File
}

Write-Host ""
if (-not $Full) {
    Write-Host "Long text is cut short above; -Full prints everything."
}
Write-Host "$total trace file(s) in $Directory - list them with -List."
Write-Host ""
exit 0
