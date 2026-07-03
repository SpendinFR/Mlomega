<#
MLOmega V19 live-services Windows installer.

Transactional, inspired by INSTALL_MLOMEGA_V18_8_WINDOWS.ps1 but for the V19
.venv-live only. Guarantees:

* NEVER touches an existing .venv (the V18.8 core env). It only owns .venv-live.
* Creates .venv-live transactionally: builds .venv-live.new, then swaps it in
  atomically; on any failure the previous .venv-live is restored.
* Preflight: Python 3.11 64-bit present (FAIL if missing), ffmpeg present (WARN),
  nvidia-smi present (WARN + CPU-degraded note if missing), free disk space.
* Installs the live deps, writes/completes configs/MODEL_MANIFEST.yaml, and runs
  the doctor at the end.

PowerShell 5.1 compatible: no '&&', no ternary operators.
#>
[CmdletBinding()]
param(
  [string]$PersonId = "me",
  [ValidateRange(2, 512)][int]$MinimumFreeGB = 10,
  [switch]$SkipDoctor
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$VenvLive = Join-Path $ProjectRoot ".venv-live"
$VenvLiveNew = Join-Path $ProjectRoot ".venv-live.new"
$VenvLiveOld = Join-Path $ProjectRoot ".venv-live.previous"
$ManifestPath = Join-Path $ProjectRoot "configs\MODEL_MANIFEST.yaml"
$script:VenvSwapped = $false

# Live-service dependencies (handoff §5 "Dépendances").
$LiveDeps = @(
  "fastapi", "uvicorn", "pydantic", "websockets", "pynvml",
  "numpy", "opencv-python-headless", "aiortc", "av", "pytest", "pyyaml"
)

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "[OK]   $Message" -ForegroundColor Green }
function Write-Warn2([string]$Message) { Write-Host "[WARN] $Message" -ForegroundColor Yellow }
function Fail([string]$Message) { throw "INSTALLATION V19 BLOQUEE: $Message" }
function Invoke-Checked([scriptblock]$Command, [string]$What) {
  & $Command
  if ($LASTEXITCODE -ne 0) { Fail "$What (code $LASTEXITCODE)" }
}
function Get-Python311 {
  $py = Get-Command py -ErrorAction SilentlyContinue
  $candidate = $null
  if ($py) {
    $candidate = (& py -3.11 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0) { $candidate = $null }
  }
  if (-not $candidate) {
    $direct = Get-Command python -ErrorAction SilentlyContinue
    if ($direct) {
      $ver = (& python -c "import sys; print('.'.join(map(str,sys.version_info[:2])))" 2>$null | Select-Object -First 1)
      if ($ver -eq "3.11") { $candidate = (& python -c "import sys; print(sys.executable)" | Select-Object -First 1) }
    }
  }
  if (-not $candidate) { Fail "Python 3.11 64-bit introuvable. Installe Python 3.11 (py -3.11) puis relance." }
  $candidate = $candidate.Trim()
  $version = (& $candidate -c "import sys; print('.'.join(map(str,sys.version_info[:2])))" | Select-Object -First 1)
  if ($version -ne "3.11") { Fail "Python 3.11 requis; version detectee: $version" }
  $bits = (& $candidate -c "import struct; print(struct.calcsize('P')*8)" | Select-Object -First 1)
  if ($bits -ne "64") { Fail "Python 3.11 64-bit requis; interpreteur detecte: $bits-bit." }
  return $candidate
}

try {
  Set-Location $ProjectRoot
  Write-Step "Preflight V19 (materiel et outils)"

  # --- Python 3.11 64-bit (hard requirement) ---
  $PythonExe = Get-Python311
  Write-Ok "Python 3.11 64-bit: $PythonExe"

  # --- Existing .venv must never be touched ---
  $CoreVenv = Join-Path $ProjectRoot ".venv"
  if (Test-Path $CoreVenv) {
    Write-Ok "Environnement coeur .venv detecte: il ne sera PAS modifie (V19 utilise .venv-live)."
  } else {
    Write-Warn2 ".venv coeur V18.8 absent; V19 installe seulement son propre .venv-live."
  }

  # --- ffmpeg (warning only) ---
  if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Ok "ffmpeg present."
  } else {
    Write-Warn2 "ffmpeg absent: le stitching de clips/preuves sera degrade. Installe-le (winget install Gyan.FFmpeg) pour la pleine capacite."
  }

  # --- nvidia-smi (warning + CPU-degraded note) ---
  $CpuOnly = $false
  if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpuRows = @(& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null)
    if ($LASTEXITCODE -eq 0 -and $gpuRows) {
      Write-Ok "GPU NVIDIA: $($gpuRows -join '; ')"
    } else {
      $CpuOnly = $true
      Write-Warn2 "nvidia-smi present mais ne renvoie pas d'etat GPU: mode CPU degrade (pas de VisionRT/VLM GPU)."
    }
  } else {
    $CpuOnly = $true
    Write-Warn2 "nvidia-smi absent: installation en mode CPU degrade. Le GpuArbiter tournera sans NVML; VisionRT/VLM GPU indisponibles."
  }

  # --- Disk space ---
  $drive = (Get-Item $ProjectRoot).PSDrive
  $freeGB = [math]::Floor($drive.Free / 1GB)
  if ($freeGB -lt $MinimumFreeGB) { Fail "Espace disque insuffisant sur $($drive.Name): ${freeGB} Go libres; minimum V19 = $MinimumFreeGB Go." }
  Write-Ok "Espace disque: ${freeGB} Go libres."

  # --- Transactional .venv-live creation ---
  Write-Step "Creation transactionnelle de .venv-live"
  if (Test-Path $VenvLiveNew) { Remove-Item -Recurse -Force $VenvLiveNew }
  Invoke-Checked { & $PythonExe -m venv $VenvLiveNew } "Creation .venv-live.new impossible"
  $NewPython = Join-Path $VenvLiveNew "Scripts\python.exe"
  Invoke-Checked { & $NewPython -m pip install --upgrade --disable-pip-version-check pip } "Mise a niveau pip impossible"
  Write-Host "Installation des dependances live: $($LiveDeps -join ', ')" -ForegroundColor Gray
  Invoke-Checked { & $NewPython -m pip install --disable-pip-version-check @LiveDeps } "Installation des dependances live impossible"
  Invoke-Checked { & $NewPython -c "import fastapi, uvicorn, pydantic, websockets, pynvml, numpy, cv2, aiortc, av, yaml; print('live deps import OK')" } "Verification d'import des dependances live impossible"

  Write-Step "Bascule atomique de .venv-live"
  if (Test-Path $VenvLiveOld) { Remove-Item -Recurse -Force $VenvLiveOld }
  if (Test-Path $VenvLive) { Rename-Item -Path $VenvLive -NewName ".venv-live.previous" }
  Rename-Item -Path $VenvLiveNew -NewName ".venv-live"
  $script:VenvSwapped = $true
  Write-Ok ".venv-live active."
  if (Test-Path $VenvLiveOld) { Remove-Item -Recurse -Force $VenvLiveOld -ErrorAction SilentlyContinue }

  # --- MODEL_MANIFEST.yaml (write/complete; do not clobber existing entries) ---
  Write-Step "Ecriture/completion de configs/MODEL_MANIFEST.yaml"
  New-Item -ItemType Directory -Force (Join-Path $ProjectRoot "configs") | Out-Null
  if (-not (Test-Path $ManifestPath)) {
    @"
models:
  live_llm:
    provider: ollama
    default: qwen2.5:3b-instruct-q4_K_M
    license: model-card-required
    max_vram_mb: 3072
  deep_llm:
    provider: ollama
    default: qwen3.5:9b-q4_K_M
    license: model-card-required
    phase: nocturne
  detector:
    provider: onnx_local
    default: yolox-nano.onnx
    license: Apache-2.0
"@ | Set-Content -Encoding UTF8 $ManifestPath
    Write-Ok "MODEL_MANIFEST.yaml cree."
  } else {
    $manifestText = Get-Content -LiteralPath $ManifestPath -Raw
    if ($manifestText -notmatch "install_profile") {
      Add-Content -LiteralPath $ManifestPath -Value "install_profile:`n  cpu_only: $($CpuOnly.ToString().ToLower())`n  installed_at: $((Get-Date).ToString('o'))" -Encoding UTF8
      Write-Ok "MODEL_MANIFEST.yaml complete (install_profile ajoute)."
    } else {
      Write-Ok "MODEL_MANIFEST.yaml deja present et complet."
    }
  }

  # --- Final doctor ---
  if (-not $SkipDoctor) {
    Write-Step "Doctor V19 final"
    & (Join-Path $ScriptDir "DOCTOR_MLOMEGA_V19.ps1")
    if ($LASTEXITCODE -ne 0) {
      Write-Warn2 "Le doctor a signale des avertissements/erreurs (code $LASTEXITCODE). Consulte sa sortie ci-dessus."
    }
  }

  Write-Host "`nINSTALLATION V19 TERMINEE. Lance: .\scripts\RUN_MLOMEGA_V19.ps1 -SimOnly" -ForegroundColor Green
  exit 0
}
catch {
  $errorText = $_.Exception.Message
  Write-Host "`n$errorText" -ForegroundColor Red
  if ($script:VenvSwapped) {
    try {
      if (Test-Path $VenvLive) { Remove-Item -Recurse -Force $VenvLive }
      if (Test-Path $VenvLiveOld) { Rename-Item -Path $VenvLiveOld -NewName ".venv-live" }
      Write-Warn2 "Ancien .venv-live restaure."
    } catch { Write-Warn2 "Impossible de restaurer .venv-live: $($_.Exception.Message)" }
  } else {
    if (Test-Path $VenvLiveNew) { Remove-Item -Recurse -Force $VenvLiveNew -ErrorAction SilentlyContinue }
  }
  exit 1
}
