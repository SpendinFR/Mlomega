<#
MLOmega V18.8 core Windows installer.

A success exit code means all core dependencies were concretely verified:
Python venv + pinned packages, Qdrant health, Ollama model pulls and real
LLM/VLM requests, WhisperX/Pyannote/SpeechBrain/Silero loads, vector loads,
SQLite schema, and a temporary Phone Bridge health probe. It never treats a
missing optional legacy Graphiti/Mem0 component as an error because they are
not part of this deployment profile.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory=$false)][string]$HfToken,
  [string]$PersonId = "me",
  [switch]$ResumeAfterReboot,
  [switch]$SkipAutoInstallPrerequisites,
  [switch]$KeepPreviousVenv,
  [switch]$SkipHeavyModelSmoke,
  [ValidateRange(8,512)][int]$MinimumFreeGB = 45,
  [ValidateRange(4,128)][int]$MinimumVramGB = 8
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ProjectRoot = (Resolve-Path $PSScriptRoot).Path
$StateDir = Join-Path $ProjectRoot ".mlomega_audio_elite\install"
$EnvPath = Join-Path $ProjectRoot ".env"
$TemplatePath = Join-Path $ProjectRoot ".env.core-v18_8.template"
$LockPath = Join-Path $ProjectRoot "requirements-v18_8-windows.lock.txt"
$ComposePath = Join-Path $ProjectRoot "docker-compose.core-v18_8.yml"
$ReleaseManifestPath = Join-Path $ProjectRoot "release-manifest-v18_8.json"
$InstallStatePath = Join-Path $StateDir "install-state-v18_8.json"
$ResumeSecretPath = Join-Path $StateDir "resume-hf-token-v18_8.secure"
$ResumeTaskName = "MLOmega V18.8 Installation Resume"
$script:RebootRequested = $false
$VenvPath = Join-Path $ProjectRoot ".venv"
$VenvNew = Join-Path $ProjectRoot ".venv.new"
$VenvOld = Join-Path $ProjectRoot ".venv.previous"
$ConfigBackup = $null
$VenvWasSwapped = $false
$EnvExistedAtStart = Test-Path $EnvPath
$script:OllamaWasUpgraded = $false

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Fail([string]$Message) { throw "INSTALLATION V18.8 BLOQUÉE: $Message" }
function Invoke-Checked([scriptblock]$Command, [string]$What) {
  & $Command
  $code = $LASTEXITCODE
  if ($code -in @(3010,1641)) { Request-Reboot "$What demande un redémarrage Windows (code $code)." }
  if ($code -ne 0) { Fail "$What (code $code)" }
}
function Test-IsAdmin {
  $current = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal($current)
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Refresh-ProcessPath {
  # winget updates Machine/User PATH but the current elevated PowerShell keeps
  # its old environment. Refresh it before discovering a freshly installed
  # Python, ffmpeg, Docker or Ollama executable.
  $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $user = [Environment]::GetEnvironmentVariable("Path", "User")
  $env:Path = @($machine, $user, $env:Path | Where-Object { $_ }) -join ';'
}
function Install-WingetPackage([string]$Id, [string]$Label) {
  if ($SkipAutoInstallPrerequisites) { Fail "$Label absent et -SkipAutoInstallPrerequisites a été demandé." }
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { Fail "$Label absent. winget est requis pour l'installation automatique." }
  Write-Host "Installation de $Label via winget..." -ForegroundColor Yellow
  Invoke-Checked { winget install --id $Id --exact --accept-package-agreements --accept-source-agreements } "Impossible d'installer $Label"
  Refresh-ProcessPath
}
function Get-Python311 {
  $py = Get-Command py -ErrorAction SilentlyContinue
  if (-not $py) { Install-WingetPackage "Python.Python.3.11" "Python 3.11"; $py = Get-Command py -ErrorAction SilentlyContinue }
  if (-not $py) { Fail "Python Launcher (py) reste introuvable après installation." }
  $candidate = (& py -3.11 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
  if ($LASTEXITCODE -ne 0 -or -not $candidate) { Fail "Python 3.11 est requis; py -3.11 ne fonctionne pas." }
  $candidate = $candidate.Trim()
  $version = (& $candidate -c "import sys; print('.'.join(map(str,sys.version_info[:2])))" | Select-Object -First 1)
  if ($version -ne "3.11") { Fail "Python 3.11 requis; version détectée: $version" }
  $bits = (& $candidate -c "import struct; print(struct.calcsize('P')*8)" | Select-Object -First 1)
  if ($bits -ne "64") { Fail "Python 3.11 64-bit requis; interpréteur détecté: $bits-bit." }
  return $candidate
}
function Wait-Http([string]$Url, [int]$TimeoutSeconds, [string]$Label) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  $last = $null
  while ((Get-Date) -lt $deadline) {
    try {
      $response = Invoke-RestMethod -Uri $Url -Method Get -TimeoutSec 8
      return $response
    } catch { $last = $_.Exception.Message; Start-Sleep -Seconds 2 }
  }
  Fail "$Label ne répond pas après $TimeoutSeconds s. Dernière erreur: $last"
}
function Set-EnvValue([string]$Path, [string]$Key, [string]$Value) {
  $escaped = [regex]::Escape($Key)
  $line = "$Key=$Value"
  $content = @()
  if (Test-Path $Path) { $content = Get-Content -LiteralPath $Path -ErrorAction Stop }
  $found = $false
  $updated = foreach ($row in $content) {
    if ($row -match "^$escaped=") { $found = $true; $line } else { $row }
  }
  if (-not $found) { $updated += $line }
  [System.IO.File]::WriteAllLines($Path, [string[]]$updated, (New-Object System.Text.UTF8Encoding($false)))
}
function Get-EnvValue([string]$Path, [string]$Key) {
  if (-not (Test-Path $Path)) { return $null }
  $match = Get-Content -LiteralPath $Path | Where-Object { $_ -match "^$([regex]::Escape($Key))=" } | Select-Object -Last 1
  if ($match) { return ($match -split "=",2)[1] }
  return $null
}
function New-PhoneToken {
  $bytes = New-Object byte[] 32
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  return ([Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_'))
}
function Write-InstallState([string]$State, [string]$Detail = "") {
  try {
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    [ordered]@{version="18.8.1"; state=$State; detail=$Detail; at=(Get-Date).ToString("o"); project_root=$ProjectRoot; person_id=$PersonId; resume_after_reboot=[bool]$ResumeAfterReboot} |
      ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $InstallStatePath -Encoding UTF8
  } catch { }
}
function Save-ResumeToken([string]$Token) {
  New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
  # DPAPI ties the encrypted continuation secret to this Windows account and PC.
  (ConvertTo-SecureString $Token -AsPlainText -Force | ConvertFrom-SecureString) | Set-Content -LiteralPath $ResumeSecretPath -Encoding ASCII
}
function Restore-ResumeToken {
  if (-not (Test-Path $ResumeSecretPath)) { return $null }
  try {
    $secure = ConvertTo-SecureString (Get-Content -LiteralPath $ResumeSecretPath -Raw) 
    return (New-Object System.Management.Automation.PSCredential("mlomega", $secure)).GetNetworkCredential().Password
  } catch { return $null }
}
function Register-InstallResumeTask([string]$Reason) {
  if (-not $HfToken) { Fail "Impossible de préparer la reprise après reboot sans jeton Hugging Face." }
  Save-ResumeToken $HfToken
  Write-InstallState "reboot_required" $Reason
  $taskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -ResumeAfterReboot -PersonId `"$PersonId`""
  & schtasks.exe /Create /TN $ResumeTaskName /SC ONLOGON /RL HIGHEST /TR $taskCommand /F | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail "Impossible de créer la tâche de reprise après reboot (code $LASTEXITCODE)." }
}
function Clear-InstallResumeTask {
  try { & schtasks.exe /Delete /TN $ResumeTaskName /F | Out-Null } catch { }
  Remove-Item -LiteralPath $ResumeSecretPath -Force -ErrorAction SilentlyContinue
}
function Request-Reboot([string]$Reason) {
  $script:RebootRequested = $true
  throw "MLOMEGA_REBOOT_REQUIRED: $Reason"
}
function Test-ReleaseManifest {
  if (-not (Test-Path $ReleaseManifestPath)) { Fail "Manifest de release V18.8 absent: $ReleaseManifestPath" }
  try { $manifest = Get-Content -LiteralPath $ReleaseManifestPath -Raw | ConvertFrom-Json } catch { Fail "Manifest de release illisible: $($_.Exception.Message)" }
  if ([string]$manifest.version -ne "18.8.1") { Fail "Manifest de release incompatible: version '$($manifest.version)'" }
  if (-not $manifest.files) { Fail "Manifest de release sans liste de fichiers." }
  $rootPrefix = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar
  foreach ($entry in @($manifest.files)) {
    $relative = [string]$entry.path
    if (-not $relative -or $relative -match '(^[\\/]|:|\.\.)') { Fail "Chemin invalide dans le manifest: $relative" }
    $full = [IO.Path]::GetFullPath((Join-Path $ProjectRoot $relative))
    if (-not $full.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) { Fail "Le manifest sort de la release: $relative" }
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { Fail "Fichier requis absent: $relative" }
    $actual = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne ([string]$entry.sha256).ToLowerInvariant()) { Fail "Intégrité invalide pour $relative" }
  }
  return $manifest
}
function Write-AndroidConfig {
  $host = $null
  $tailscale = Get-Command tailscale -ErrorAction SilentlyContinue
  if ($tailscale) { try { $host = ((& $tailscale.Source ip -4 2>$null | Select-Object -First 1).Trim()) } catch {} }
  if (-not $host) {
    $candidate = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object -First 1
    if ($candidate) { $host = $candidate.IPAddress }
  }
  if (-not $host) { return $null }
  $out = Join-Path $ProjectRoot "MLOmega_Phone_Bridge_V18_8\android\mlomega_android_config.env.v18_8.generated"
  @(
    '# Generated by INSTALL_MLOMEGA_V18_8_WINDOWS.ps1. Copy to ~/mlomega_android_config.env in Termux.',
    "API_BASE=\"http://$host`:8766\"",
    "TOKEN=\"$env:MLOMEGA_PHONE_TOKEN\"",
    'MLOMEGA_DEVICE_ID="android_phone"',
    'AUDIO_SECONDS=4','IMAGE_SECONDS=25','GPS_SECONDS=30',
    'ENABLE_AUDIO=1','ENABLE_IMAGES=1','ENABLE_GPS=1',
    'POST_SESSION_STOP=1','DRAIN_UPLOADS_ON_STOP=1','DRAIN_UPLOADS_TIMEOUT_SECONDS=180','KEEP_SENT_FILES=0'
  ) | Set-Content -LiteralPath $out -Encoding UTF8
  return $out
}
function Export-CoreEnvToProcess {
  Get-Content -LiteralPath $EnvPath | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
    $parts = $_ -split '=',2
    if ($parts[0].Trim()) { Set-Item -Path "Env:$($parts[0].Trim())" -Value $parts[1] }
  }
  $env:MLOMEGA_PROJECT_ROOT = $ProjectRoot
}
function Test-DockerEngineReady {
  $previous = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    & docker version --format '{{.Server.Version}}' 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
  } catch { return $false }
  finally { $ErrorActionPreference = $previous }
}
function Ensure-DockerReady {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Install-WingetPackage "Docker.DockerDesktop" "Docker Desktop" }
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "Docker Desktop a été installé mais la commande docker reste introuvable après rafraîchissement du PATH." }
  if (Test-DockerEngineReady) { return }
  # Docker Desktop often starts on login, but a scripted first install should not
  # depend on that timing. Start it explicitly and wait for its Linux engine.
  # Build candidates defensively: some managed Windows installs omit one of
  # these environment variables. `Join-Path $null` would hide Docker's real
  # readiness problem behind a PowerShell parameter error.
  $desktopCandidates = @()
  foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
    if ($base) {
      foreach ($relative in @("Docker\Docker\Docker Desktop.exe", "Docker\Docker Desktop.exe")) {
        $candidate = Join-Path $base $relative
        if (Test-Path $candidate) { $desktopCandidates += $candidate }
      }
    }
  }
  $desktopCandidates = $desktopCandidates | Select-Object -Unique
  if ($desktopCandidates) {
    try { Start-Process -FilePath $desktopCandidates[0] | Out-Null } catch {}
  }
  $deadline = (Get-Date).AddSeconds(180)
  while ((Get-Date) -lt $deadline) {
    if (Test-DockerEngineReady) { return }
    Start-Sleep -Seconds 3
  }
  $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
  if ($wsl) {
    try {
      & $wsl.Source --status 2>$null | Out-Null
      if ($LASTEXITCODE -ne 0) {
        if ($SkipAutoInstallPrerequisites) { Request-Reboot "WSL2/Docker nécessite la finalisation Windows avant démarrage." }
        Write-Host "Installation/activation de WSL2 requise pour Docker Desktop..." -ForegroundColor Yellow
        & $wsl.Source --install --no-distribution 2>$null | Out-Null
        $wslCode = $LASTEXITCODE
        if ($wslCode -in @(0,3010,1641)) { Request-Reboot "WSL2 a été activé pour Docker Desktop." }
        Fail "Activation WSL2 impossible (code $wslCode). Active WSL2/Docker Desktop puis relance le même script."
      }
    } catch {
      if ($_.Exception.Message -like "MLOMEGA_REBOOT_REQUIRED:*") { throw }
    }
  }
  Fail "Docker Desktop/WSL2 ne devient pas prêt après 180 s. L'installation est arrêtée sans faux succès; termine l'initialisation Windows/Docker requise par le système puis relance le même script."
}

function Get-OllamaVersion([string]$Executable) {
  try {
    $raw = (& $Executable --version 2>&1 | Out-String).Trim()
    $match = [regex]::Match($raw, '(?<!\d)(\d+\.\d+\.\d+)(?!\d)')
    if ($match.Success) { return [version]$match.Groups[1].Value }
  } catch { }
  return $null
}
function Ensure-OllamaMinimumVersion([object]$OllamaCommand) {
  # qwen3-vl:8b is a required V18.8 model and Ollama's own model page requires
  # 0.12.7 or later. Check before pulling gigabytes, not after a later VLM crash.
  $minimum = [version]'0.12.7'
  $current = Get-OllamaVersion $OllamaCommand.Source
  if ($null -ne $current -and $current -ge $minimum) { return $OllamaCommand }
  if ($SkipAutoInstallPrerequisites) {
    $shown = if ($current) { $current.ToString() } else { 'inconnue' }
    Fail "Ollama $minimum ou plus récent est requis pour qwen3-vl:8b; version détectée: $shown. Mets Ollama à jour puis relance."
  }
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Fail "Ollama $minimum ou plus récent est requis, mais winget est absent pour la mise à jour automatique."
  }
  Write-Host "Mise à jour Ollama requise pour qwen3-vl:8b..." -ForegroundColor Yellow
  Invoke-Checked { winget upgrade --id Ollama.Ollama --exact --accept-package-agreements --accept-source-agreements } "Mise à jour Ollama impossible"
  Refresh-ProcessPath
  $updated = Get-Command ollama -ErrorAction SilentlyContinue
  if (-not $updated) { Fail "Ollama est introuvable après sa mise à jour." }
  $after = Get-OllamaVersion $updated.Source
  if ($null -eq $after -or $after -lt $minimum) {
    $shown = if ($after) { $after.ToString() } else { 'inconnue' }
    Fail "Ollama reste trop ancien après mise à jour (version détectée: $shown; minimum: $minimum)."
  }
  $script:OllamaWasUpgraded = $true
  return $updated
}
function Start-OllamaIfNeeded {
  # The API can be left over from another session while the CLI is absent from
  # PATH.  Installation must own both: it uses the CLI for deterministic model
  # pulls and the API for health checks.
  $ollama = Get-Command ollama -ErrorAction SilentlyContinue
  if (-not $ollama) { Install-WingetPackage "Ollama.Ollama" "Ollama"; $ollama = Get-Command ollama -ErrorAction SilentlyContinue }
  if (-not $ollama) { Fail "Ollama introuvable après installation." }
  $ollama = Ensure-OllamaMinimumVersion $ollama
  if ($script:OllamaWasUpgraded) {
    # A pre-existing server can still be the old binary after winget upgraded
    # the CLI. Restart it once so the following model smoke test proves the
    # required server version, rather than an older process left in memory.
    Write-Host "Redémarrage du service Ollama après mise à jour..." -ForegroundColor Yellow
    Get-Process -Name "ollama" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
  } else {
    try { Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 5 | Out-Null; return } catch {}
  }
  $logDir = Join-Path $StateDir "logs"; New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  Start-Process -FilePath $ollama.Source -ArgumentList "serve" -WorkingDirectory $ProjectRoot -RedirectStandardOutput (Join-Path $logDir "ollama-serve.out.log") -RedirectStandardError (Join-Path $logDir "ollama-serve.err.log") | Out-Null
  Wait-Http "http://127.0.0.1:11434/api/tags" 90 "Ollama"
}
function Test-HuggingFaceToken([string]$Token) {
  # Fail before a long Python/model install when the only user-provided secret
  # is malformed or belongs to an account that has not accepted Pyannote's
  # gated model terms. The later real model-load doctor remains authoritative.
  if ($Token -notmatch '^hf_[A-Za-z0-9_-]{8,}$') { Fail "Le token Hugging Face ne semble pas valide (format attendu hf_...)." }
  $headers = @{ Authorization = "Bearer $Token" }
  try {
    $who = Invoke-RestMethod -Uri "https://huggingface.co/api/whoami-v2" -Headers $headers -TimeoutSec 30
    if (-not $who) { Fail "Hugging Face ne reconnaît pas le token fourni." }
  } catch { Fail "Validation du token Hugging Face impossible: $($_.Exception.Message)" }
  # WhisperX/Pyannote needs both gated repositories. A 401/403 is surfaced
  # before downloading the rest of the stack, with the exact account action.
  foreach ($repo in @("pyannote/segmentation-3.0", "pyannote/speaker-diarization-3.1")) {
    try {
      Invoke-WebRequest -Uri "https://huggingface.co/$repo/resolve/main/config.yaml" -Headers $headers -TimeoutSec 30 -UseBasicParsing | Out-Null
    } catch { Fail "Le token Hugging Face est valide mais $repo n'est pas autorisé. Accepte ses conditions avec ce même compte puis relance." }
  }
}
function Ensure-BridgeFirewallRule {
  # The bridge remains authenticated by its random token. This rule only opens
  # the private/domain Windows profiles; it does not expose the API on Public.
  $name = "MLOmega V18.8 Phone Bridge TCP 8766"
  try {
    $existing = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
    if ($existing) { Set-NetFirewallRule -DisplayName $name -Enabled True -Profile Private,Domain -Direction Inbound -Action Allow | Out-Null }
    else { New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8766 -Profile Private,Domain | Out-Null }
  } catch { Fail "Impossible de configurer la règle pare-feu du Phone Bridge: $($_.Exception.Message)" }
}
function Assert-PhoneBridgeAuthenticated([int]$Port, [string]$Token) {
  if (-not $Token) { Fail "MLOMEGA_PHONE_TOKEN est absent; le Bridge ne peut pas être validé." }
  try {
    $status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/status" -Headers @{ "X-MLOmega-Token" = $Token } -TimeoutSec 10
    if (-not $status.ok) { Fail "Le Phone Bridge répond mais son endpoint authentifié /status n'est pas valide." }
  } catch {
    Fail "Le Phone Bridge n'accepte pas le jeton configuré pour ce projet: $($_.Exception.Message)"
  }
}
function Assert-BridgePortReady {
  # A collision must be caught before long model downloads. Reuse only a bridge
  # proven to point to this project; another listener can silently receive data.
  $port = 8766
  $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
  if (-not $listeners) { return }
  try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 3
    $sameRoot = $health.project_root -and ([IO.Path]::GetFullPath([string]$health.project_root).TrimEnd('\') -eq [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\'))
    if ($health.ok -and $sameRoot -and [bool]$health.allow_post_stop) {
      Write-Host "Phone Bridge V18.8 déjà actif sur le port $port : réutilisation contrôlée pour le smoke test." -ForegroundColor Yellow
      return
    }
  } catch {}
  $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique) -join ', '
  Fail "Le port Phone Bridge $port est déjà occupé (PID: $owners). Arrête ce processus ou corrige son port avant l'installation; V18.8 refuse de risquer un mauvais receiver."
}
function Assert-PhoneNetworkProfile {
  # The firewall intentionally does not expose the authenticated bridge on an
  # untrusted Public network. Surface this before a user expects phone upload.
  try { $profiles = @(Get-NetConnectionProfile -ErrorAction Stop) }
  catch {
    # No active adapter is acceptable for desktop installation. Android pairing
    # later requires an address and is handled by EXPORT_PHONE_CONFIG.
    return
  }
  $usable = @($profiles | Where-Object { $_.NetworkCategory -in @('Private','DomainAuthenticated') })
  if (-not $usable -and $profiles.Count -gt 0) {
    $names = @($profiles | ForEach-Object { "$($_.Name):$($_.NetworkCategory)" }) -join '; '
    Fail "Réseau Windows uniquement Public ($names). Le pare-feu V18.8 n'expose pas le Bridge sur Public. Passe le réseau utilisé par le téléphone en Privé ou utilise Tailscale, puis relance."
  }
}

function Assert-NoActiveBrainLive {
  # Never mutate a venv/.env underneath an active capture process. A dead PID
  # after a power loss is harmless; the eventual V18.8 recovery path will own
  # the persisted work safely after installation.
  $manifest = Join-Path $ProjectRoot ".mlomega_audio_elite\runtime\launcher-v18_8.json"
  if (Test-Path $manifest) {
    try {
      $state = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
      $pid = [int]($state.brainlive_pid)
      if ($pid -gt 0 -and (Get-Process -Id $pid -ErrorAction SilentlyContinue)) {
        Fail "Une capture BrainLive est active (PID $pid). Lance STOP_MLOMEGA_V18_8.ps1, attends la clôture ou arrête la capture avant une mise à niveau."
      }
    } catch { }
  }
}
function Start-TemporaryBridge([string]$VenvPython) {
  # Use an isolated port and state root.  This test proves Android-style upload
  # -> durable queue -> normalized inbox + sidecar, without adding fixtures to
  # the real user's capture inbox or attaching to a bridge that may be running.
  $runner = Join-Path $ProjectRoot "MLOmega_Phone_Bridge_V18_8\pc\run_brainlive_phone_receiver.ps1"
  if (-not (Test-Path $runner)) { Fail "Phone Bridge V18.8 absent du bundle." }
  $port = 8776
  $healthUrl = "http://127.0.0.1:$port/health"
  $smokeRoot = Join-Path $StateDir "bridge-smoke-v18_8"
  if (Test-Path $smokeRoot) { Remove-Item -Recurse -Force $smokeRoot }
  $token = $env:MLOMEGA_PHONE_TOKEN
  $proc = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile","-ExecutionPolicy","Bypass","-File",$runner,"-Token",$token,"-ProjectRoot",$ProjectRoot,"-StateRoot",$smokeRoot,"-Port",$port,"-PersonId",$PersonId,"-AllowPostStopOnSessionStop") -WorkingDirectory $ProjectRoot -PassThru
  try {
    $health = Wait-Http $healthUrl 60 "Phone Bridge de smoke test"
    $sameRoot = $health.project_root -and ([IO.Path]::GetFullPath([string]$health.project_root).TrimEnd('\') -eq [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\'))
    $sameState = $health.state_root -and ([IO.Path]::GetFullPath([string]$health.state_root).TrimEnd('\') -eq [IO.Path]::GetFullPath($smokeRoot).TrimEnd('\'))
    if (-not $health.ok -or -not $sameRoot -or -not $sameState -or -not [bool]$health.allow_post_stop) { Fail "Phone Bridge temporaire démarré mais son contrat /health isolé est invalide." }
    Assert-PhoneBridgeAuthenticated $port $env:MLOMEGA_PHONE_TOKEN
  } catch {
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    throw
  }
  return $proc
}
function Stop-ProcessSafe($Process) {
  if ($null -ne $Process -and -not $Process.HasExited) { Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue }
}

if (-not $HfToken -and $ResumeAfterReboot) { $HfToken = Restore-ResumeToken }
if (-not $HfToken) { Fail "Le jeton Hugging Face est requis au premier lancement. Utilise -HfToken hf_xxx." }
try {
  Set-Location $ProjectRoot
  Write-InstallState "resuming" "validation du package"
  $releaseManifest = Test-ReleaseManifest
  Write-Step "Préflight matériel et outils"
  Assert-NoActiveBrainLive
  Test-HuggingFaceToken $HfToken
  if (-not [Environment]::Is64BitOperatingSystem) { Fail "Windows 64-bit requis." }
  if (-not (Test-IsAdmin)) { Fail "Lance PowerShell en administrateur pour une installation reproductible des prérequis système." }
  New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
  $drive = (Get-Item $ProjectRoot).PSDrive
  $freeGB = [math]::Floor($drive.Free / 1GB)
  if ($freeGB -lt $MinimumFreeGB) { Fail "Espace disque insuffisant sur $($drive.Name): ${freeGB} Go libres; minimum V18.8 = $MinimumFreeGB Go (Python, modèles HF et Ollama)." }
  $ramGB = [math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
  if ($ramGB -lt 16) { Fail "RAM insuffisante: ${ramGB} Go détectés; minimum supporté V18.8 = 16 Go." }
  if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { Fail "GPU NVIDIA/pilote absent: le profil V18.8 requiert CUDA pour WhisperX/Pyannote." }
  $gpuRows = @(& nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader,nounits 2>$null)
  if ($LASTEXITCODE -ne 0 -or -not $gpuRows) { Fail "nvidia-smi ne renvoie pas d'état GPU utilisable." }
  $gpu = $gpuRows -join "; "
  $maxVramMB = 0
  foreach($row in $gpuRows){ $parts=$row -split ','; if($parts.Count -ge 3){ $mb=0; [void][int]::TryParse($parts[2].Trim(), [ref]$mb); if($mb -gt $maxVramMB){$maxVramMB=$mb} } }
  if($maxVramMB -lt ($MinimumVramGB * 1024)){ Fail "VRAM insuffisante: $([math]::Floor($maxVramMB/1024)) Go détectés; minimum supporté = $MinimumVramGB Go." }
  Write-Host "GPU: $gpu" -ForegroundColor Green
  Write-Host "Ressources: ${freeGB} Go disque libre, ${ramGB} Go RAM, $([math]::Floor($maxVramMB/1024)) Go VRAM" -ForegroundColor Green
  if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { Install-WingetPackage "Gyan.FFmpeg" "FFmpeg" }
  if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { Fail "FFmpeg a été installé mais reste introuvable dans cette session. Ferme/réouvre PowerShell administrateur puis relance le même script." }
  Ensure-DockerReady
  $PythonExe = Get-Python311
  Write-Host "Python: $PythonExe" -ForegroundColor Green
  Ensure-BridgeFirewallRule
  Assert-BridgePortReady
  Assert-PhoneNetworkProfile

  Write-InstallState "preflight_passed" "matériel, token HF et outils système validés"
  Write-Step "Configuration V18.8 core (sans Graphiti/Mem0)"
  if (-not (Test-Path $TemplatePath)) { Fail "Template .env V18.8 absent." }
  if (Test-Path $EnvPath) { $ConfigBackup = "$EnvPath.backup.$((Get-Date).ToString('yyyyMMddHHmmss'))"; Copy-Item $EnvPath $ConfigBackup -Force }
  if (-not (Test-Path $EnvPath)) { Copy-Item $TemplatePath $EnvPath -Force }
  $phoneToken = Get-EnvValue $EnvPath "MLOMEGA_PHONE_TOKEN"; if (-not $phoneToken -or $phoneToken -like "__*") { $phoneToken = New-PhoneToken }
  $values = @{
    "MLOMEGA_DEPLOYMENT_PROFILE"="CORE_BRAINLIVE_V18_8_PHONE"; "MLOMEGA_GRAPH_BACKEND"="disabled"; "MLOMEGA_MEM0_ENABLED"="false"; "MLOMEGA_STRICT_ELITE"="true";
    "MLOMEGA_HOME"="$ProjectRoot\.mlomega_audio_elite"; "MLOMEGA_DB"="$ProjectRoot\.mlomega_audio_elite\memory.db"; "MLOMEGA_RAW"="$ProjectRoot\.mlomega_audio_elite\raw"; "MLOMEGA_PROJECT_ROOT"=$ProjectRoot;
    "MLOMEGA_HF_TOKEN"=$HfToken; "MLOMEGA_PHONE_TOKEN"=$phoneToken; "MLOMEGA_PHONE_BRIDGE_URL"="http://127.0.0.1:8766"; "MLOMEGA_PHONE_BRIDGE_REQUIRED"="true";
    "MLOMEGA_QDRANT_URL"="http://127.0.0.1:6333"; "MLOMEGA_OLLAMA_BASE_URL"="http://127.0.0.1:11434"; "MLOMEGA_ENABLE_OLLAMA"="true"; "MLOMEGA_ENABLE_LLM_DEEP"="true";
    "MLOMEGA_OLLAMA_MODEL"="qwen3.5:9b"; "MLOMEGA_VLM_MODEL"="moondream"; "MLOMEGA_OFFLINE_VLM_MODEL"="qwen3-vl:8b"; "MLOMEGA_VLM_HEAVY_MODEL"="qwen3-vl:8b";
    "MLOMEGA_OLLAMA_KEEP_ALIVE_LIVE"="20m"; "MLOMEGA_OLLAMA_KEEP_ALIVE_POSTSTOP"="30m"; "MLOMEGA_OLLAMA_CONNECT_TIMEOUT_S"="45"; "MLOMEGA_OLLAMA_COLD_START_TIMEOUT_S"="900";
    "MLOMEGA_BRAINLIVE_ASR_BACKEND"="faster_or_whispercpp"; "MLOMEGA_FAST_WHISPER_MODEL"="small"; "MLOMEGA_FAST_WHISPER_DEVICE"="cuda"; "MLOMEGA_FAST_WHISPER_COMPUTE"="float16";
    # V18.8.1 live policy. Write these explicitly even over an existing .env so
    # an in-place upgrade cannot retain an unsafe pre-debounce/pre-evidence profile.
    "MLOMEGA_BRAINLIVE_LLM_MIN_INTERVAL_S"="12"; "MLOMEGA_BRAINLIVE_LLM_AUDIO_WINDOW_S"="45"; "MLOMEGA_BRAINLIVE_LLM_MAX_WINDOW_S"="90";
    "MLOMEGA_BRAINLIVE_IMAGE_DHASH_CHANGE_BITS"="8"; "MLOMEGA_BRAINLIVE_IMAGE_LIVE_REFRESH_S"="600"; "MLOMEGA_BRAINLIVE_IMAGE_MIN_VLM_INTERVAL_S"="20"; "MLOMEGA_BRAINLIVE_IMAGE_FORCE_AFTER_S"="90";
    "MLOMEGA_BRAINLIVE_IMAGE_MAX_PENDING"="48"; "MLOMEGA_BRAINLIVE_IMAGE_QUEUE_TARGET"="4"; "MLOMEGA_BRAINLIVE_IMAGE_LEASE_S"="120"; "MLOMEGA_BRAINLIVE_IMAGE_RETRY_DELAY_S"="20"; "MLOMEGA_BRAINLIVE_VLM_TIMEOUT_S"="8";
    "MLOMEGA_BRAINLIVE_VISUAL_SPLIT_MIN_SEPARATION_S"="45"; "MLOMEGA_BRAINLIVE_BUNDLE_DHASH_SPLIT_BITS"="14"; "MLOMEGA_BRAINLIVE_PIXEL_SPLIT_MIN_SEPARATION_S"="90"; "MLOMEGA_BRAINLIVE_MAX_BUNDLE_MINUTES"="25";
    "MLOMEGA_WHISPERX_MODEL"="large-v3"; "MLOMEGA_WHISPERX_DEVICE"="cuda"; "MLOMEGA_WHISPERX_COMPUTE_TYPE"="float16"; "MLOMEGA_WHISPERX_BATCH_SIZE"="4";
    "MLOMEGA_ENABLE_WHISPERX"="true"; "MLOMEGA_ENABLE_PYANNOTE"="true"; "MLOMEGA_ENABLE_SPEECHBRAIN"="true";
    "MLOMEGA_POSTSTOP_LLM_TIMEOUT_S"="900"; "MLOMEGA_POSTSTOP_VLM_TIMEOUT_S"="300"; "MLOMEGA_POSTSTOP_RETRY_MAX"="2"; "MLOMEGA_POSTSTOP_RETRY_BACKOFF_S"="15,60";
    "MLOMEGA_DEEP_AUDIO_RETRY_MAX"="2"; "MLOMEGA_DEEP_AUDIO_FFMPEG_TIMEOUT_S"="300"; "MLOMEGA_DEEP_AUDIO_BUNDLE_MAX_SECONDS"="1800"; "MLOMEGA_STAGE_STALE_AFTER_S"="1800";
    "MLOMEGA_STOP_DRAIN_TIMEOUT_S"="300"; "MLOMEGA_STOP_DRAIN_IDLE_PASSES"="2"; "MLOMEGA_PHONE_DRAIN_BEFORE_POST_STOP"="1"; "MLOMEGA_PHONE_CLEANUP_AFTER_POST_STOP"="1"; "MLOMEGA_CLOSE_DAY_TIMEOUT_S"="7200"; "MLOMEGA_CLOSE_DAY_POLL_S"="2";
    "MLOMEGA_REQUIRE_SELF_VOICE"="false"; "MLOMEGA_VOICE_LEARNING_STRICT"="true"; "MLOMEGA_CLEANUP_REQUIRES_ZERO_PENDING"="true"
  }
  foreach ($key in $values.Keys) { Set-EnvValue $EnvPath $key $values[$key] }
  Export-CoreEnvToProcess

  Write-InstallState "configuring" "écriture du profil core"
  Write-Step "Création transactionnelle de l'environnement Python isolé"
  if (Test-Path $VenvNew) { Remove-Item -Recurse -Force $VenvNew }
  Invoke-Checked { & $PythonExe -m venv $VenvNew } "Création .venv.new impossible"
  $NewPython = Join-Path $VenvNew "Scripts\python.exe"
  Invoke-Checked { & $NewPython -m pip install --upgrade --disable-pip-version-check pip==24.3.1 setuptools==75.6.0 wheel==0.45.1 } "Mise à niveau pip impossible"
  Invoke-Checked { & $NewPython -m pip install -r $LockPath } "Installation des dépendances V18.8 impossible"
  Invoke-Checked { & $NewPython -m pip install --no-deps -e $ProjectRoot } "Installation du paquet local V18.8 impossible"
  Invoke-Checked { & $NewPython -m pip check } "pip check détecte des dépendances incohérentes"

  Write-InstallState "venv_ready" "environnement Python isolé vérifié"
  Write-Step "Démarrage des services locaux et téléchargement des modèles"
  Invoke-Checked { docker compose -f $ComposePath up -d } "Démarrage Qdrant impossible"
  Wait-Http "http://127.0.0.1:6333/collections" 90 "Qdrant" | Out-Null
  Start-OllamaIfNeeded
  $models = @($env:MLOMEGA_OLLAMA_MODEL, $env:MLOMEGA_VLM_MODEL, $env:MLOMEGA_OFFLINE_VLM_MODEL) | Select-Object -Unique
  foreach ($model in $models) { Invoke-Checked { ollama pull $model } "Téléchargement Ollama $model impossible" }

  Write-InstallState "services_ready" "Qdrant, Ollama et modèles téléchargés"
  Write-Step "Activation atomique de V18.8"
  if (Test-Path $VenvOld) { Remove-Item -Recurse -Force $VenvOld }
  if (Test-Path $VenvPath) { Rename-Item -Path $VenvPath -NewName ".venv.previous" }
  Rename-Item -Path $VenvNew -NewName ".venv"
  $VenvWasSwapped = $true
  $VenvPython = Join-Path $VenvPath "Scripts\python.exe"
  Invoke-Checked { & $VenvPython -m mlomega_audio_elite.cli init-db } "Initialisation SQLite impossible"

  Write-InstallState "activation_ready" "venv activé et SQLite initialisée"
  Write-Step "Smoke test réel des modèles, services et Phone Bridge"
  $bridgeProcess = $null
  $savedBridgeUrl = $env:MLOMEGA_PHONE_BRIDGE_URL
  $bridgeSmokeRoot = Join-Path $StateDir "bridge-smoke-v18_8"
  try {
    # The release .env retains production port 8766. Only the child doctor is
    # routed to isolated smoke port 8776.
    $env:MLOMEGA_PHONE_BRIDGE_URL = "http://127.0.0.1:8776"
    $bridgeProcess = Start-TemporaryBridge $VenvPython
    $doctorArgs = @("-m","mlomega_audio_elite.cli","doctor-core-v18-8","--fail","--check-bridge","--check-bridge-delivery")
    if (-not $SkipHeavyModelSmoke) { $doctorArgs += @("--check-models","--check-vectors") }
    Invoke-Checked { & $VenvPython @doctorArgs } "Doctor core V18.8 a échoué"
  } finally {
    Stop-ProcessSafe $bridgeProcess
    $env:MLOMEGA_PHONE_BRIDGE_URL = $savedBridgeUrl
    if (Test-Path $bridgeSmokeRoot) { Remove-Item -Recurse -Force $bridgeSmokeRoot -ErrorAction SilentlyContinue }
  }

  $androidConfig = Write-AndroidConfig
  $envSha256 = (Get-FileHash -LiteralPath $EnvPath -Algorithm SHA256).Hash.ToLowerInvariant()
  $releaseManifestSha256 = (Get-FileHash -LiteralPath $ReleaseManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
  $manifest = [ordered]@{version="18.8.1"; installed_at=(Get-Date).ToString("o"); project_root=$ProjectRoot; python=$VenvPython; gpu=$gpu; profile="CORE_BRAINLIVE_V18_8_PHONE"; hf_token_configured=$true; model_smoke_skipped=[bool]$SkipHeavyModelSmoke; android_config=$androidConfig; env_sha256=$envSha256; release_manifest_sha256=$releaseManifestSha256}
  $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $StateDir "installation-success-v18_8.json") -Encoding UTF8
  Write-InstallState $(if($SkipHeavyModelSmoke){"SYSTEM_READY"}else{"PRODUCTION_READY"}) "doctor core V18.8 terminé"
  if (-not $KeepPreviousVenv -and (Test-Path $VenvOld)) { Remove-Item -Recurse -Force $VenvOld }
  if ($SkipHeavyModelSmoke) {
    Write-Host "`nINSTALLATION TECHNIQUE V18.8 TERMINÉE — les modèles lourds n'ont pas été validés car -SkipHeavyModelSmoke a été demandé." -ForegroundColor Yellow
    Write-Host "Avant une vraie capture, exécute .\DOCTOR_MLOMEGA_V18_8.ps1 -Full -Bridge et corrige toute erreur." -ForegroundColor Yellow
  } else {
    Write-Host "`nINSTALLATION V18.8 RÉUSSIE — lance .\RUN_MLOMEGA_V18_8.ps1 -PersonId $PersonId" -ForegroundColor Green
  }
  if ($androidConfig) { Write-Host "Configuration Android générée: $androidConfig" -ForegroundColor Green }
  else { Write-Host "Bridge PC validé. Pour l'adresse Android, exécute .\EXPORT_PHONE_CONFIG_V18_8.ps1 -Host IP_DU_PC après connexion réseau/Tailscale." -ForegroundColor Yellow }
  Clear-InstallResumeTask
  exit 0
}
catch {
  $errorText = $_.Exception.Message
  if ($errorText -like "MLOMEGA_REBOOT_REQUIRED:*") {
    try {
      Register-InstallResumeTask $errorText
      Write-Host "`nRedémarrage Windows requis. La reprise V18.8 est enregistrée et redémarrera automatiquement à la prochaine ouverture de session." -ForegroundColor Yellow
      Write-Host "Motif: $errorText" -ForegroundColor Yellow
      exit 3010
    } catch {
      Write-Host "`n$errorText" -ForegroundColor Red
      Write-Host "La tâche de reprise automatique n'a pas pu être créée: $($_.Exception.Message)" -ForegroundColor Red
      exit 1
    }
  }
  Write-InstallState "failed" $errorText
  Write-Host "`n$errorText" -ForegroundColor Red
  if ($VenvWasSwapped) {
    try {
      if (Test-Path $VenvPath) { Remove-Item -Recurse -Force $VenvPath }
      if (Test-Path $VenvOld) { Rename-Item -Path $VenvOld -NewName ".venv" }
      Write-Host "Ancien environnement Python restauré." -ForegroundColor Yellow
    } catch { Write-Host "Impossible de restaurer automatiquement l'ancien .venv: $($_.Exception.Message)" -ForegroundColor Yellow }
  }
  if ($ConfigBackup -and (Test-Path $ConfigBackup)) {
    try { Copy-Item $ConfigBackup $EnvPath -Force; Write-Host "Configuration précédente restaurée." -ForegroundColor Yellow }
    catch { Write-Host "Sauvegarde de configuration conservée (restauration manuelle): $ConfigBackup" -ForegroundColor Yellow }
  } elseif (-not $EnvExistedAtStart -and (Test-Path $EnvPath)) {
    try { Remove-Item $EnvPath -Force; Write-Host "Configuration partielle supprimée." -ForegroundColor Yellow } catch {}
  }
  exit 1
}
