<# Resume only unresolved V18.7 checkpoints after timeout, PC shutdown or process crash.
   It restores the local runtime first, acknowledges retained inbox media, then
   resumes exactly the same durable close-day run. #>
[CmdletBinding()]
param(
  [string]$PersonId = "me",
  [string]$PackageDate
)
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path $PSScriptRoot).Path
$EnvPath = Join-Path $ProjectRoot ".env"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$InstallSuccess = Join-Path $ProjectRoot ".mlomega_audio_elite\install\installation-success-v18_7.json"
$ReleaseManifest = Join-Path $ProjectRoot "release-manifest-v18_7.json"
function Fail([string]$Message) { throw "RESUME V18.7 BLOQUÉ: $Message" }
function Invoke-Checked([scriptblock]$Command, [string]$What) { & $Command; if ($LASTEXITCODE -ne 0) { Fail "$What (code $LASTEXITCODE)" } }
function Wait-Http([string]$Url, [int]$TimeoutSeconds, [string]$Label) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds); $last = $null
  while ((Get-Date) -lt $deadline) {
    try { return Invoke-RestMethod -Uri $Url -TimeoutSec 8 }
    catch { $last = $_.Exception.Message; Start-Sleep -Seconds 2 }
  }
  Fail "$Label non disponible après $TimeoutSeconds s: $last"
}
function Read-Json([string]$Path) { if(!(Test-Path $Path)){return $null}; try{return (Get-Content $Path -Raw | ConvertFrom-Json)}catch{return $null} }
function Assert-ReleaseManifestIntegrity {
  if(!(Test-Path $ReleaseManifest)){Fail "Manifest de release V18.7 absent."}
  try{$manifest=Get-Content $ReleaseManifest -Raw|ConvertFrom-Json}catch{Fail "Manifest de release illisible: $($_.Exception.Message)"}
  if([string]$manifest.version -ne "18.7.1"){Fail "Manifest de release incompatible: $($manifest.version)"}
  foreach($entry in @($manifest.files)){
    $relative=[string]$entry.path
    if(!$relative -or $relative -match '(^[\\/]|:|\.\.)'){Fail "Chemin invalide dans le manifest: $relative"}
    $full=Join-Path $ProjectRoot $relative
    if(!(Test-Path $full -PathType Leaf)){Fail "Fichier de release absent: $relative"}
    $actual=(Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash.ToLowerInvariant()
    if($actual -ne ([string]$entry.sha256).ToLowerInvariant()){Fail "Release modifiée/incohérente: $relative. Réinstalle avant RESUME."}
  }
}
function Assert-ValidatedInstall {
  $installed=Read-Json $InstallSuccess
  if(!$installed){Fail "Aucun rapport d'installation V18.7 réussie. Relance INSTALL avant RESUME."}
  if([string]$installed.version -ne "18.7.1"){Fail "Rapport d'installation incompatible: $($installed.version)"}
  if([bool]$installed.model_smoke_skipped){Fail "Installation sans smoke modèles. Relance INSTALL sans -SkipHeavyModelSmoke avant RESUME."}
  if(!(Test-Path $EnvPath)){Fail ".env absent"}
  $envHash=(Get-FileHash -LiteralPath $EnvPath -Algorithm SHA256).Hash.ToLowerInvariant()
  if($envHash -ne ([string]$installed.env_sha256).ToLowerInvariant()){Fail ".env a changé depuis la validation. Relance INSTALL avant RESUME."}
  $manifestHash=(Get-FileHash -LiteralPath $ReleaseManifest -Algorithm SHA256).Hash.ToLowerInvariant()
  if($manifestHash -ne ([string]$installed.release_manifest_sha256).ToLowerInvariant()){Fail "Manifest de release modifié depuis l'installation. Réinstalle avant RESUME."}
  Assert-ReleaseManifestIntegrity
}
function Import-ProjectEnv {
  if (!(Test-Path $EnvPath)) { Fail ".env absent" }
  Get-Content $EnvPath | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
    $parts = $_ -split '=', 2
    if ($parts[0].Trim()) { Set-Item "Env:$($parts[0].Trim())" $parts[1].Trim() }
  }
  $env:MLOMEGA_PROJECT_ROOT = $ProjectRoot
}
function Ensure-Ollama {
  try { Wait-Http "$($env:MLOMEGA_OLLAMA_BASE_URL)/api/tags" 2 "Ollama" | Out-Null; return } catch {}
  $bin = Get-Command ollama -ErrorAction SilentlyContinue
  if (-not $bin) { Fail "Ollama est absent; relance l'installateur V18.7." }
  $logDir = Join-Path $ProjectRoot ".mlomega_audio_elite\runtime\logs"; New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  Start-Process -FilePath $bin.Source -ArgumentList "serve" -WorkingDirectory $ProjectRoot -RedirectStandardOutput (Join-Path $logDir "ollama-resume.out.log") -RedirectStandardError (Join-Path $logDir "ollama-resume.err.log") | Out-Null
  Wait-Http "$($env:MLOMEGA_OLLAMA_BASE_URL)/api/tags" 90 "Ollama" | Out-Null
}
try {
  Set-Location $ProjectRoot
  Assert-ValidatedInstall
  Import-ProjectEnv
  if (!(Test-Path $Python)) { Fail ".venv absent : relance INSTALL_MLOMEGA_V18_7_WINDOWS.ps1" }
  Invoke-Checked { docker compose -f (Join-Path $ProjectRoot "docker-compose.core-v18_7.yml") up -d } "Qdrant ne démarre pas"
  Wait-Http "$($env:MLOMEGA_QDRANT_URL)/collections" 90 "Qdrant" | Out-Null
  Ensure-Ollama
  Invoke-Checked { & $Python -m mlomega_audio_elite.cli doctor-core-v18-7 --fail | Out-Host } "Doctor core V18.7 non conforme"
  Invoke-Checked { & $Python -m mlomega_audio_elite.cli brainlive-recover-stale-services | Out-Host } "Récupération des services interrompus impossible"
  Invoke-Checked { & $Python -m mlomega_audio_elite.cli brainlive-resume-inbox-drain --person-id $PersonId | Out-Host } "Reprise de l'inbox a échoué : les sources sont conservées"
  $args = @("-m", "mlomega_audio_elite.cli", "brainlive-resume-close-day", "--person-id", $PersonId)
  if ($PackageDate) { $args += @("--package-date", $PackageDate) }
  # An explicit RESUME is an operator-approved retry. Force only bypasses a
  # backoff timer; it never discards completed checkpoints or source evidence.
  $args += "--force"
  & $Python @args | Out-Host
  exit $LASTEXITCODE
}
catch {
  Write-Host $_.Exception.Message -ForegroundColor Red
  exit 1
}
