<# Start the verified V18.7 core flow: Qdrant, Ollama, Phone Bridge, BrainLive. #>
[CmdletBinding()]
param(
  [string]$PersonId = "me",
  [string]$Title = "BrainLive daily capture",
  [switch]$Restart # Compatibility flag: V18.7 refuses unsafe mid-session replacement.
)
$ErrorActionPreference="Stop"
$ProjectRoot=(Resolve-Path $PSScriptRoot).Path
$EnvPath=Join-Path $ProjectRoot ".env"
$VenvPython=Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$RuntimeDir=Join-Path $ProjectRoot ".mlomega_audio_elite\runtime"
$LauncherManifest=Join-Path $RuntimeDir "launcher-v18_7.json"
$InstallSuccess=Join-Path $ProjectRoot ".mlomega_audio_elite\install\installation-success-v18_7.json"
$ReleaseManifest=Join-Path $ProjectRoot "release-manifest-v18_7.json"
function Fail($m){throw "RUN V18.7 BLOQUÉ: $m"}
function Wait-Http($url,[int]$timeout,$label){$d=(Get-Date).AddSeconds($timeout);$last=$null;while((Get-Date)-lt $d){try{return Invoke-RestMethod -Uri $url -TimeoutSec 8}catch{$last=$_.Exception.Message;Start-Sleep 2}};Fail "$label non disponible: $last"}
function Invoke-Checked([scriptblock]$c,$m){& $c;if($LASTEXITCODE -ne 0){Fail "$m (code $LASTEXITCODE)"}}
function Export-CoreEnv { if(-not(Test-Path $EnvPath)){Fail "Lance d'abord INSTALL_MLOMEGA_V18_7_WINDOWS.ps1"}; Get-Content $EnvPath | ForEach-Object {if($_ -match '^\s*#' -or $_ -notmatch '='){return};$x=$_ -split '=',2;if($x[0].Trim()){Set-Item "Env:$($x[0].Trim())" $x[1]}}; $env:MLOMEGA_PROJECT_ROOT=$ProjectRoot }
function Read-Json($p){if(!(Test-Path $p)){return $null};try{$x=Get-Content $p -Raw|ConvertFrom-Json;return $x}catch{return $null}}
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
    if($actual -ne ([string]$entry.sha256).ToLowerInvariant()){Fail "Release modifiée/incohérente: $relative. Réinstalle avant RUN."}
  }
}
function Assert-ProductionInstall {
  $installed=Read-Json $InstallSuccess
  if(!$installed){Fail "Aucun rapport d'installation V18.7 réussi. Lance INSTALL_MLOMEGA_V18_7_WINDOWS.ps1."}
  if([string]$installed.version -ne "18.7.1"){Fail "Rapport d'installation incompatible: $($installed.version)"}
  if([bool]$installed.model_smoke_skipped){Fail "Installation technique sans smoke models. Exécute INSTALL sans -SkipHeavyModelSmoke avant RUN."}
  if(!(Test-Path $ReleaseManifest)){Fail "Manifest de release V18.7 absent."}
  $envHash=(Get-FileHash -LiteralPath $EnvPath -Algorithm SHA256).Hash.ToLowerInvariant()
  if($envHash -ne ([string]$installed.env_sha256).ToLowerInvariant()){
    Fail ".env a changé depuis la validation. Relance INSTALL_MLOMEGA_V18_7_WINDOWS.ps1 pour revalider la configuration avant capture."
  }
  $releaseHash=(Get-FileHash -LiteralPath $ReleaseManifest -Algorithm SHA256).Hash.ToLowerInvariant()
  if($releaseHash -ne ([string]$installed.release_manifest_sha256).ToLowerInvariant()){
    Fail "Le manifest de release a changé depuis l'installation. Réinstalle/revalide V18.7 avant capture."
  }
  Assert-ReleaseManifestIntegrity
}
function Get-RecoveryStatus {
  $raw = & $VenvPython -m mlomega_audio_elite.cli brainlive-recovery-status --person-id $PersonId 2>&1
  if ($LASTEXITCODE -ne 0) { Fail "Impossible de lire l'état de reprise V18.7: $($raw -join ' ')" }
  try { return (($raw -join "`n") | ConvertFrom-Json) }
  catch { Fail "État de reprise V18.7 invalide: $($raw -join ' ')" }
}
function Wait-BrainLiveHeartbeat([string]$ServiceRunId, [int]$TimeoutSeconds = 45) {
  $deadline=(Get-Date).AddSeconds($TimeoutSeconds);$last=$null
  while((Get-Date)-lt $deadline){
    $raw=& $VenvPython -m mlomega_audio_elite.cli brainlive-service-status --service-run-id $ServiceRunId 2>&1
    if($LASTEXITCODE -eq 0){
      try {
        $status=($raw -join "`n")|ConvertFrom-Json
        $heartbeat=[string]$status.last_heartbeat_at
        if($status.status -eq 'running' -and $heartbeat){
          $at=[DateTimeOffset]::Parse($heartbeat)
          $age=([DateTimeOffset]::UtcNow-$at.ToUniversalTime()).TotalSeconds
          if($age -ge 0 -and $age -le 20){return $status}
          $last="heartbeat trop ancien (${age}s)"
        } else {$last="status=$($status.status), heartbeat=$heartbeat"}
      } catch {$last=$_.Exception.Message}
    } else {$last=$raw -join ' '}
    Start-Sleep -Seconds 1
  }
  Fail "BrainLive a publié un manifeste mais aucun heartbeat frais pour $ServiceRunId après $TimeoutSeconds s. Consulte $RuntimeDir\logs."
}
function Ensure-Ollama {
  try { Wait-Http "$($env:MLOMEGA_OLLAMA_BASE_URL)/api/tags" 2 "Ollama" | Out-Null; return } catch {}
  $bin=Get-Command ollama -ErrorAction SilentlyContinue
  if(-not $bin){Fail "Ollama est absent; relance l'installateur V18.7."}
  $logDir=Join-Path $RuntimeDir "logs"; New-Item -ItemType Directory -Force -Path $logDir|Out-Null
  Start-Process -FilePath $bin.Source -ArgumentList "serve" -WorkingDirectory $ProjectRoot -RedirectStandardOutput (Join-Path $logDir "ollama-serve.out.log") -RedirectStandardError (Join-Path $logDir "ollama-serve.err.log")|Out-Null
  Wait-Http "$($env:MLOMEGA_OLLAMA_BASE_URL)/api/tags" 90 "Ollama"|Out-Null
}
function Assert-PhoneBridgeAuthenticated($bridgeBase) {
  if(-not $env:MLOMEGA_PHONE_TOKEN){Fail "MLOMEGA_PHONE_TOKEN est absent; le Bridge ne peut pas être validé."}
  try {
    $status=Invoke-RestMethod -Uri "$($bridgeBase.Scheme)://$($bridgeBase.Host):$($bridgeBase.Port)/status" -Headers @{"X-MLOmega-Token"=$env:MLOMEGA_PHONE_TOKEN} -TimeoutSec 10
    if(-not $status.ok){Fail "Le Phone Bridge répond mais son endpoint authentifié /status est invalide."}
  } catch { Fail "Le Phone Bridge ne répond pas avec le jeton de ce projet: $($_.Exception.Message)" }
}
function Start-Bridge {
  $bridgeBase=[uri]$env:MLOMEGA_PHONE_BRIDGE_URL
  $healthUrl="$($bridgeBase.Scheme)://$($bridgeBase.Host):$($bridgeBase.Port)/health"
  # Probe directly: an absent bridge is normal at first launch. Do not route this
  # expected condition through Wait-Http, which is reserved for mandatory checks.
  $h=$null
  try { $h=Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2 } catch { $h=$null }
  if($h){
    $sameRoot=[IO.Path]::GetFullPath([string]$h.project_root).TrimEnd('\') -eq [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
    if($h.ok -and $sameRoot -and [bool]$h.allow_post_stop){ Assert-PhoneBridgeAuthenticated $bridgeBase; return $null }
    Fail "Un Phone Bridge incompatible est déjà actif ($healthUrl). Il doit viser ce projet et allow_post_stop=true."
  }
  $runner=Join-Path $ProjectRoot "MLOmega_Phone_Bridge_V18_7\pc\run_brainlive_phone_receiver.ps1"
  if(!(Test-Path $runner)){Fail "Phone Bridge V18.7 absent."}
  if($bridgeBase.Port -lt 1){Fail "MLOMEGA_PHONE_BRIDGE_URL doit inclure un port valide."}
  $logDir=Join-Path $RuntimeDir "logs";New-Item -ItemType Directory -Force -Path $logDir|Out-Null
  $p=Start-Process powershell.exe -ArgumentList @("-NoProfile","-ExecutionPolicy","Bypass","-File",$runner,"-Token",$env:MLOMEGA_PHONE_TOKEN,"-ProjectRoot",$ProjectRoot,"-Port","$($bridgeBase.Port)","-PersonId",$PersonId,"-AllowPostStopOnSessionStop") -WorkingDirectory $ProjectRoot -PassThru -RedirectStandardOutput (Join-Path $logDir "phone-bridge.out.log") -RedirectStandardError (Join-Path $logDir "phone-bridge.err.log")
  $h=Wait-Http $healthUrl 60 "Phone Bridge"
  $sameRoot=[IO.Path]::GetFullPath([string]$h.project_root).TrimEnd('\') -eq [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
  if(-not $h.ok -or -not $sameRoot -or -not [bool]$h.allow_post_stop){ Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Fail "Phone Bridge démarré mais son contrat santé est invalide." }
  try { Assert-PhoneBridgeAuthenticated $bridgeBase }
  catch { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; throw }
  return $p
}
$bridge=$null
$svc=$null
try {
  Set-Location $ProjectRoot; Export-CoreEnv
  Assert-ProductionInstall
  if(!(Test-Path $VenvPython)){Fail ".venv V18.7 absent"}
  New-Item -ItemType Directory -Force -Path $RuntimeDir|Out-Null
  Invoke-Checked { docker compose -f (Join-Path $ProjectRoot "docker-compose.core-v18_7.yml") up -d } "Qdrant ne démarre pas"
  Wait-Http "$($env:MLOMEGA_QDRANT_URL)/collections" 60 "Qdrant"|Out-Null
  Ensure-Ollama
  Invoke-Checked { & $VenvPython -m mlomega_audio_elite.cli doctor-core-v18-7 --fail | Out-Host } "Doctor core V18.7 non conforme"
  Invoke-Checked { & $VenvPython -m mlomega_audio_elite.cli brainlive-recover-stale-services | Out-Host } "Récupération des services interrompus impossible"
  $recovery=Get-RecoveryStatus
  if($recovery.status -eq "resume_required"){
    $count=@($recovery.unresolved).Count
    Fail "$count reprise(s) durable(s) sont en attente. Pour ne pas démarrer une capture sur des sources non clôturées, exécute d'abord .\RESUME_MLOMEGA_V18_7.ps1 -PersonId $PersonId."
  }
  # The runtime JSON is only a convenience file.  After a power cut it can
  # still say `running` even though recover-stale has already marked the DB
  # service orphaned.  Decide from the durable service record, not the stale
  # file, then remove the stale file so it cannot block a healthy next RUN.
  $serviceRaw=& $VenvPython -m mlomega_audio_elite.cli brainlive-service-status 2>&1
  if($LASTEXITCODE -ne 0){Fail "Impossible de vérifier l'état durable BrainLive: $($serviceRaw -join ' ')"}
  try{$serviceState=($serviceRaw -join "`n")|ConvertFrom-Json}catch{Fail "État durable BrainLive invalide: $($serviceRaw -join ' ')"}
  if($serviceState.status -eq "running"){
    $hint = if($Restart){" -Restart ne coupe jamais une session active en V18.7."}else{""}
    Fail "Une session BrainLive est déjà active: $($serviceState.service_run_id). Lance STOP_MLOMEGA_V18_7.ps1 puis, si nécessaire, RESUME_MLOMEGA_V18_7.ps1.$hint"
  }
  $old=Read-Json (Join-Path $RuntimeDir "brainlive_service.json")
  if($old -and $old.status -eq "running"){
    Remove-Item (Join-Path $RuntimeDir "brainlive_service.json") -Force -ErrorAction SilentlyContinue
  }
  $bridge=Start-Bridge
  Invoke-Checked { & $VenvPython -m mlomega_audio_elite.cli doctor-core-v18-7 --fail --check-bridge | Out-Host } "Doctor Phone Bridge V18.7 non conforme"
  $serviceManifest=Join-Path $RuntimeDir "brainlive_service.json"; if(Test-Path $serviceManifest){Remove-Item $serviceManifest -Force}
  $logDir=Join-Path $RuntimeDir "logs"; New-Item -ItemType Directory -Force -Path $logDir|Out-Null
  $svc=Start-Process -FilePath $VenvPython -ArgumentList @("-m","mlomega_audio_elite.cli","brainlive-start-service","--person-id",$PersonId,"--title",$Title) -WorkingDirectory $ProjectRoot -PassThru -RedirectStandardOutput (Join-Path $logDir "brainlive.out.log") -RedirectStandardError (Join-Path $logDir "brainlive.err.log")
  $deadline=(Get-Date).AddSeconds(45);$service=$null
  while((Get-Date)-lt $deadline){$service=Read-Json $serviceManifest;if($service -and $service.status -eq "running" -and $service.service_run_id){break};if($svc.HasExited){Fail "BrainLive s'est arrêté au démarrage. Consulte $logDir\brainlive.err.log"};Start-Sleep 1}
  if(-not $service -or -not $service.service_run_id){Fail "BrainLive n'a pas publié son manifeste de session."}
  $heartbeat=Wait-BrainLiveHeartbeat -ServiceRunId ([string]$service.service_run_id) -TimeoutSeconds 45
  $packageDate=(Get-Date).ToString('yyyy-MM-dd')
  [ordered]@{version="18.7.1";started_at=(Get-Date).ToString("o");person_id=$PersonId;package_date=$packageDate;brainlive_pid=$svc.Id;bridge_pid=if($bridge){$bridge.Id}else{$null};service_run_id=$service.service_run_id;live_session_id=$service.live_session_id;last_heartbeat_at=$heartbeat.last_heartbeat_at;bridge_url=$env:MLOMEGA_PHONE_BRIDGE_URL}|ConvertTo-Json|Set-Content $LauncherManifest -Encoding UTF8
  Write-Host "`nRUN V18.7 ACTIF" -ForegroundColor Green
  Write-Host "BrainLive run: $($service.service_run_id)"; Write-Host "Live session: $($service.live_session_id)"; Write-Host "Bridge: $($env:MLOMEGA_PHONE_BRIDGE_URL)"
  exit 0
} catch {
  if($bridge -and -not $bridge.HasExited){ Stop-Process -Id $bridge.Id -Force -ErrorAction SilentlyContinue }
  Write-Host $_.Exception.Message -ForegroundColor Red
  exit 1
}
