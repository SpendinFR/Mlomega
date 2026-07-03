<#
DOCTOR_MLOMEGA_V19 — real health checks for the V19 live services.

Emits [OK]/[WARN]/[FAIL] per check and a non-zero exit code if any FAIL.
WARN never fails the run. Flags select subsets:

  -Full      : run everything below
  -Memory    : delivery queue table + DB checks
  -Xr        : XR readiness (WARN "non testable sans lunettes" — never a fake OK)
  -Vision    : GPU/detector readiness
  -Delivery  : delivery queue table accessible

With no flags, the base checks always run (Python, .venv-live, contracts,
GPU probe, Qdrant, Ollama, profile).

PowerShell 5.1 compatible: no '&&', no ternary operators.
#>
[CmdletBinding()]
param(
  [switch]$Full, [switch]$Memory, [switch]$Xr, [switch]$Vision, [switch]$World, [switch]$Delivery
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
Set-Location $ProjectRoot

$script:Failures = 0
$script:Warnings = 0
function Check-Ok([string]$m)   { Write-Host "[OK]   $m" -ForegroundColor Green }
function Check-Warn([string]$m) { Write-Host "[WARN] $m" -ForegroundColor Yellow; $script:Warnings++ }
function Check-Fail([string]$m) { Write-Host "[FAIL] $m" -ForegroundColor Red; $script:Failures++ }
function Section([string]$m)    { Write-Host "`n== $m ==" -ForegroundColor Cyan }

$runAll = $Full
$doVision  = $Full -or $Vision
$doMemory  = $Full -or $Memory -or $Delivery
$doDelivery = $Full -or $Delivery -or $Memory
$doXr      = $Full -or $Xr

# Resolve the python interpreter: prefer .venv-live, else .venv, else PATH.
$LivePython = Join-Path $ProjectRoot ".venv-live\Scripts\python.exe"
$CorePython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Python = $null
if (Test-Path $LivePython) { $Python = $LivePython }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $Python = "python" }

Section "Base"

# --- Python + version ---
if ($Python) {
  $ver = (& $Python -c "import sys;print('.'.join(map(str,sys.version_info[:3])))" 2>$null | Select-Object -First 1)
  if ($ver -like "3.11*") { Check-Ok "Python $ver" } else { Check-Warn "Python $ver (3.11 recommande pour la parite avec le coeur)" }
} else {
  Check-Fail "Aucun interpreteur Python trouve (.venv-live ni PATH)."
}

# --- .venv-live importable ---
if (Test-Path $LivePython) {
  $probe = & $LivePython -c "import importlib; mods=['fastapi','pydantic','pynvml']; miss=[m for m in mods if importlib.util.find_spec(m) is None]; import importlib.util as u; opt='aiortc' if u.find_spec('aiortc') else ''; print('MISS='+','.join(miss)); print('AIORTC='+('yes' if opt else 'no'))" 2>$null
  $missLine = ($probe | Where-Object { $_ -like "MISS=*" }) -replace "MISS=",""
  $aiortcLine = ($probe | Where-Object { $_ -like "AIORTC=*" }) -replace "AIORTC=",""
  if ([string]::IsNullOrWhiteSpace($missLine)) { Check-Ok ".venv-live importable (fastapi, pydantic, pynvml)" }
  else { Check-Fail ".venv-live: modules manquants: $missLine" }
  if ($aiortcLine -eq "yes") { Check-Ok "aiortc present" } else { Check-Warn "aiortc absent (transport WebRTC indisponible; simulateur/contrats restent OK)" }
} else {
  Check-Warn ".venv-live absent: lance scripts\INSTALL_MLOMEGA_V19_WINDOWS.ps1 (checks contrats via python systeme)."
}

# --- Contracts round-trip (8 schemas) ---
if ($Python) {
  & $Python (Join-Path $ScriptDir "validate_contracts_v19.py") | Out-Null
  if ($LASTEXITCODE -eq 0) { Check-Ok "Contrats V19: 8 schemas round-trip" }
  else { Check-Fail "Contrats V19: le round-trip a echoue (relance scripts\validate_contracts_v19.py pour le detail)" }
} else {
  Check-Fail "Contrats V19 non verifiables sans interpreteur Python."
}

# --- GPU via nvidia-smi ---
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
  $g = @(& nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader,nounits 2>$null)
  if ($LASTEXITCODE -eq 0 -and $g) {
    foreach ($row in $g) {
      $p = $row -split ','
      if ($p.Count -ge 3) { Check-Ok "GPU $($p[0].Trim()): $($p[1].Trim()) Mo total, $($p[2].Trim()) Mo libres" }
    }
  } else { Check-Warn "nvidia-smi present mais sans etat GPU utilisable (mode CPU degrade)" }
} else {
  Check-Warn "nvidia-smi absent: mode CPU degrade (pas de VisionRT/VLM GPU)"
}

# --- Qdrant on 6333 ---
try {
  Invoke-RestMethod -Uri "http://127.0.0.1:6333/collections" -TimeoutSec 4 | Out-Null
  Check-Ok "Qdrant joignable sur 6333"
} catch { Check-Warn "Qdrant injoignable sur 6333 (memoire vectorielle indisponible; demarre docker compose Qdrant)" }

# --- Ollama on 11434 ---
try {
  Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 4 | Out-Null
  Check-Ok "Ollama joignable sur 11434"
} catch { Check-Warn "Ollama injoignable sur 11434 (LLM live/deep indisponible)" }

# --- user_profile.yaml present + valid ---
$profilePath = Join-Path $ProjectRoot "configs\user_profile.yaml"
if (Test-Path $profilePath) {
  if ($Python) {
    $vp = & $Python -c "import sys,yaml; d=yaml.safe_load(open(r'$profilePath',encoding='utf-8')); req=['display','capture','llm','vision','asr','cloud_data_policy']; miss=[k for k in req if k not in (d or {})]; print('MISS='+','.join(miss))" 2>$null
    $pMiss = ($vp | Where-Object { $_ -like "MISS=*" }) -replace "MISS=",""
    if ([string]::IsNullOrWhiteSpace($pMiss)) { Check-Ok "configs\user_profile.yaml present et valide" }
    else { Check-Fail "configs\user_profile.yaml incomplet (cles manquantes: $pMiss). Relance scripts\setup_profile.ps1" }
  } else { Check-Ok "configs\user_profile.yaml present (validation YAML sautee sans Python)" }
} else {
  Check-Warn "configs\user_profile.yaml absent. Lance: scripts\setup_profile.ps1 (ou -Defaults)"
}

# --- Vision subset ---
if ($doVision) {
  Section "Vision"
  $rtx = Join-Path $ProjectRoot "configs\profiles\rtx3070.yaml"
  if (Test-Path $rtx) { Check-Ok "Profil VisionRT configs\profiles\rtx3070.yaml present" }
  else { Check-Fail "configs\profiles\rtx3070.yaml absent (cadences detecteur/queue introuvables)" }
}

# --- Delivery / Memory subset ---
if ($doMemory -or $doDelivery) {
  Section "Delivery / Memory"
  if ($Python) {
    $dbProbe = & $Python -c @"
import os, sqlite3, sys
db = os.environ.get('MLOMEGA_DB')
if not db or not os.path.exists(db):
    print('NODB'); sys.exit(0)
try:
    con = sqlite3.connect(db)
    row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='brainlive_intervention_delivery_queue'").fetchone()
    print('TABLE_OK' if row else 'TABLE_MISSING')
except Exception as e:
    print('ERR:'+str(e))
"@ 2>$null
    if ($dbProbe -like "TABLE_OK*") { Check-Ok "Table brainlive_intervention_delivery_queue accessible" }
    elseif ($dbProbe -like "NODB*") { Check-Warn "MLOMEGA_DB absent/non initialise: table delivery non verifiable (normal avant premiere capture)" }
    elseif ($dbProbe -like "TABLE_MISSING*") { Check-Warn "DB presente mais table delivery absente (sera creee par ensure_delivery_schema au premier usage)" }
    else { Check-Warn "Verification table delivery: $dbProbe" }
  } else { Check-Warn "Table delivery non verifiable sans Python." }
}

# --- XR subset (never a fake OK) ---
if ($doXr) {
  Section "XR"
  Check-Warn "XR non testable sans lunettes (XREAL/S25 requis; gate G1 = Lot 3). Utilise le mode -SimOnly / phone_only."
}

# --- Summary ---
Write-Host ""
if ($script:Failures -gt 0) {
  Write-Host "DOCTOR V19: $($script:Failures) FAIL, $($script:Warnings) WARN." -ForegroundColor Red
  exit 1
} else {
  Write-Host "DOCTOR V19: OK ($($script:Warnings) WARN, 0 FAIL)." -ForegroundColor Green
  exit 0
}
