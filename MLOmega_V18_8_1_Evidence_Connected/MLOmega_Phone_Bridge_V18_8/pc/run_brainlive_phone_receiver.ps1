param(
  [Parameter(Mandatory=$true)][string]$Token,
  [string]$ProjectRoot = (Get-Location).Path,
  [int]$Port = 8766,
  [double]$PumpSeconds = 0,
  [string]$PersonId = "me",
  [string]$StateRoot,
  [switch]$AllowPostStopOnSessionStop,
  [switch]$KeepQueueBlobs,
  [int]$QueueMaxAttempts = 10,
  [int]$AudioWorkers = 2,
  [int]$ImageWorkers = 1,
  [int]$GpsWorkers = 1,
  [int]$TranscriptWorkers = 1,
  [int]$SessionWorkers = 1,
  [switch]$NoCleanupAfterPostStop,
  [switch]$CleanupDryRun,
  [int]$CloseDayTimeoutSeconds = 7200,
  [int]$CloseDayPollSeconds = 2
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (!(Test-Path $venvPython)) {
  throw "Je ne trouve pas $venvPython. Passe -ProjectRoot C:\MLOmega ou lance depuis la racine du projet."
}

# Load the same immutable project configuration used by RUN/INSTALL.  This
# matters when the bridge is launched manually: its child CLI commands need the
# Hugging Face token, model choices and database paths too, not just the phone
# token passed on the command line. Explicit arguments below remain authoritative.
$envFile = Join-Path $ProjectRoot ".env"
if (Test-Path $envFile) {
  Get-Content -LiteralPath $envFile | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
    $parts = $_ -split '=', 2
    $key = $parts[0].Trim()
    if ($key) { Set-Item -Path "Env:$key" -Value $parts[1].Trim() }
  }
}
$env:MLOMEGA_PROJECT_ROOT = $ProjectRoot
if ($StateRoot) { $env:MLOMEGA_BRIDGE_STATE_ROOT = [IO.Path]::GetFullPath($StateRoot) } else { Remove-Item Env:MLOMEGA_BRIDGE_STATE_ROOT -ErrorAction SilentlyContinue }
$env:MLOMEGA_PHONE_TOKEN = $Token
if ($PumpSeconds -gt 0) { $env:MLOMEGA_PUMP_SECONDS = "$PumpSeconds" } else { Remove-Item Env:MLOMEGA_PUMP_SECONDS -ErrorAction SilentlyContinue }
$env:MLOMEGA_PERSON_ID = $PersonId
$env:MLOMEGA_QUEUE_MAX_ATTEMPTS = "$QueueMaxAttempts"
$env:MLOMEGA_AUDIO_WORKERS = "$AudioWorkers"
$env:MLOMEGA_IMAGE_WORKERS = "$ImageWorkers"
$env:MLOMEGA_GPS_WORKERS = "$GpsWorkers"
$env:MLOMEGA_TRANSCRIPT_WORKERS = "$TranscriptWorkers"
$env:MLOMEGA_SESSION_WORKERS = "$SessionWorkers"
if ($KeepQueueBlobs) { $env:MLOMEGA_KEEP_QUEUE_BLOBS = "1" } else { $env:MLOMEGA_KEEP_QUEUE_BLOBS = "0" }
if ($AllowPostStopOnSessionStop) { $env:MLOMEGA_ALLOW_POST_STOP = "1" } else { $env:MLOMEGA_ALLOW_POST_STOP = "0" }
if ($NoCleanupAfterPostStop) { $env:MLOMEGA_PHONE_CLEANUP_AFTER_POST_STOP = "0" } else { $env:MLOMEGA_PHONE_CLEANUP_AFTER_POST_STOP = "1" }
if ($CleanupDryRun) { $env:MLOMEGA_PHONE_CLEANUP_DRY_RUN = "1" } else { $env:MLOMEGA_PHONE_CLEANUP_DRY_RUN = "0" }
$env:MLOMEGA_PHONE_DRAIN_BEFORE_POST_STOP = "1"
$env:MLOMEGA_PHONE_CLEANUP_MEDIA_KINDS = "audio,image"
$env:MLOMEGA_CLOSE_DAY_TIMEOUT_S = "$CloseDayTimeoutSeconds"
$env:MLOMEGA_CLOSE_DAY_POLL_S = "$CloseDayPollSeconds"

Write-Host "MLOmega Phone Receiver V18.8" -ForegroundColor Cyan
Write-Host "ProjectRoot: $ProjectRoot"
Write-Host "Inbox: $(if($StateRoot){Join-Path $StateRoot 'brainlive_inbox'}else{Join-Path $ProjectRoot '.mlomega_audio_elite\brainlive_inbox'})"
Write-Host "URL Tailscale/Android: http://IP_TAILSCALE_DU_PC:$Port"
Write-Host "Close-day V18.8 on /session/stop: $($AllowPostStopOnSessionStop.IsPresent)"
Write-Host "  (drain -> post-stop -> longitudinal -> coordination -> Life Model -> live-ready -> cleanup gate)"
Write-Host "Keep queue blobs: $($KeepQueueBlobs.IsPresent)"
Write-Host "Cleanup after close-day gate: $(!$NoCleanupAfterPostStop.IsPresent)"
Write-Host "Cleanup dry run: $($CleanupDryRun.IsPresent)"
$pumpDisplay = if ($PumpSeconds -gt 0) { "${PumpSeconds}s" } else { "off (per-kind defaults)" }
Write-Host "Queue max attempts: $QueueMaxAttempts; global pump override: $pumpDisplay"
Write-Host "Close-day timeout: $CloseDayTimeoutSeconds s; poll: $CloseDayPollSeconds s"
Write-Host "Workers: audio=$AudioWorkers image=$ImageWorkers gps=$GpsWorkers transcript=$TranscriptWorkers session=$SessionWorkers"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir
& $venvPython -m uvicorn brainlive_phone_receiver:app --host 0.0.0.0 --port $Port
