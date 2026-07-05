<#
DOCTOR_MLOMEGA_V19 — real health checks for the V19 live services.

Emits [OK]/[WARN]/[FAIL] per check and a non-zero exit code if any FAIL.
WARN never fails the run. Flags select subsets:

  -Full      : run everything below
  -Memory    : delivery queue table + DB checks
  -Xr        : XR readiness (WARN "non testable sans lunettes" — never a fake OK)
  -Vision    : GPU/detector readiness
  -Delivery  : delivery queue table accessible
  -Quota     : storage footprint (DB / models / evidence / day-buffer) vs profile thresholds

With no flags, the base checks always run (Python, .venv-live, contracts,
GPU probe, Qdrant, Ollama, profile).

PowerShell 5.1 compatible: no '&&', no ternary operators.
#>
[CmdletBinding()]
param(
  [switch]$Full, [switch]$Memory, [switch]$Xr, [switch]$Vision, [switch]$World, [switch]$Delivery, [switch]$Quota
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
$doQuota   = $Full -or $Quota

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

# --- Storage quotas subset (E36 §2) ---
if ($doQuota) {
  Section "Stockage / quotas"

  # Thresholds are configurable in the user profile (storage_quota block); the
  # defaults are conservative for a personal RTX-3070 box.
  $warnGb = 8.0
  $failGb = 20.0
  $bufWarnGb = 2.0
  $bufFailGb = 5.0
  if ($Python -and (Test-Path $profilePath)) {
    $q = & $Python -c @"
import sys, yaml
d = yaml.safe_load(open(r'$profilePath', encoding='utf-8')) or {}
sq = (d.get('storage_quota') or {}) if isinstance(d, dict) else {}
def g(k, dflt):
    v = sq.get(k)
    return str(v) if v is not None else str(dflt)
print('WARN_GB=' + g('warn_gb', 8))
print('FAIL_GB=' + g('fail_gb', 20))
print('BUF_WARN_GB=' + g('day_buffer_warn_gb', 2))
print('BUF_FAIL_GB=' + g('day_buffer_fail_gb', 5))
"@ 2>$null
    foreach ($line in $q) {
      if ($line -like 'WARN_GB=*')     { $warnGb = [double]($line -replace 'WARN_GB=','') }
      elseif ($line -like 'FAIL_GB=*') { $failGb = [double]($line -replace 'FAIL_GB=','') }
      elseif ($line -like 'BUF_WARN_GB=*') { $bufWarnGb = [double]($line -replace 'BUF_WARN_GB=','') }
      elseif ($line -like 'BUF_FAIL_GB=*') { $bufFailGb = [double]($line -replace 'BUF_FAIL_GB=','') }
    }
  }

  function Dir-SizeBytes([string]$path) {
    if (-not (Test-Path $path)) { return -1 }
    $sum = (Get-ChildItem -LiteralPath $path -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { return 0 }
    return [long]$sum
  }
  function Fmt-Gb([long]$b) { if ($b -lt 0) { return 'absent' } return ('{0:N2} Go' -f ($b / 1GB)) }

  # DB size (MLOMEGA_DB, else the default core memory.db location).
  $dbPath = $env:MLOMEGA_DB
  if (-not $dbPath) { $dbPath = Join-Path $ProjectRoot 'data\memory.db' }
  if (Test-Path $dbPath) {
    $dbBytes = (Get-Item -LiteralPath $dbPath).Length
    Check-Ok "DB SQLite: $(Fmt-Gb $dbBytes) ($dbPath)"
  } else {
    Check-Warn "DB SQLite absente ($dbPath) - normal avant la premiere capture."
    $dbBytes = 0
  }

  # models/ - pinned ONNX weights (should be stable, informational).
  $modelsBytes = Dir-SizeBytes (Join-Path $ProjectRoot 'models')
  if ($modelsBytes -ge 0) { Check-Ok "models/: $(Fmt-Gb $modelsBytes)" }
  else { Check-Warn "models/ absent (lance scripts\fetch_models_v19.py)" }

  # evidence root (keyframes + clips). MLOMEGA_RAW/evidence, else data\evidence.
  $evRoot = $env:MLOMEGA_EVIDENCE
  if (-not $evRoot) {
    if ($env:MLOMEGA_RAW) { $evRoot = Join-Path $env:MLOMEGA_RAW 'evidence' }
    else { $evRoot = Join-Path $ProjectRoot 'data\evidence' }
  }
  $kfBytes = Dir-SizeBytes (Join-Path $evRoot 'keyframes')
  $clipBytes = Dir-SizeBytes (Join-Path $evRoot 'clips')
  $bufBytes = Dir-SizeBytes (Join-Path $evRoot 'day_buffer')
  $kf = if ($kfBytes -lt 0) { 0 } else { $kfBytes }
  $cl = if ($clipBytes -lt 0) { 0 } else { $clipBytes }
  $bf = if ($bufBytes -lt 0) { 0 } else { $bufBytes }
  Check-Ok "evidence/keyframes: $(Fmt-Gb $kfBytes) | clips: $(Fmt-Gb $clipBytes)"

  # Total tracked footprint (DB + models + evidence) against warn/fail thresholds.
  $totalBytes = [long]$dbBytes + [long]([Math]::Max(0, $modelsBytes)) + [long]$kf + [long]$cl + [long]$bf
  $totalGb = $totalBytes / 1GB
  if ($totalGb -ge $failGb) {
    Check-Fail ("Empreinte totale {0:N2} Go >= seuil FAIL {1} Go. Purge conseillee: close-day (tampon-jour) + rotation evidence/clips." -f $totalGb, $failGb)
  } elseif ($totalGb -ge $warnGb) {
    Check-Warn ("Empreinte totale {0:N2} Go >= seuil WARN {1} Go (FAIL a {2} Go). Surveille evidence/clips." -f $totalGb, $warnGb, $failGb)
  } else {
    Check-Ok ("Empreinte totale {0:N2} Go (WARN {1} Go / FAIL {2} Go)." -f $totalGb, $warnGb, $failGb)
  }

  # Day buffer: the close-day purge already empties it (EvidenceStore.purge_day_buffer);
  # flag it when it grows past its own thresholds so the operator runs a close-day.
  $bufGb = $bf / 1GB
  if ($bufGb -ge $bufFailGb) {
    Check-Fail ("Tampon-jour {0:N2} Go >= FAIL {1} Go. Lance un close-day (purge_day_buffer vide ce tampon)." -f $bufGb, $bufFailGb)
  } elseif ($bufGb -ge $bufWarnGb) {
    Check-Warn ("Tampon-jour {0:N2} Go >= WARN {1} Go. Un close-day le purgera (EvidenceStore.purge_day_buffer)." -f $bufGb, $bufWarnGb)
  } else {
    Check-Ok ("Tampon-jour {0:N2} Go (WARN {1} / FAIL {2} Go) - purge au close-day." -f $bufGb, $bufWarnGb, $bufFailGb)
  }
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
