param(
  [string]$ProjectRoot = (Get-Location).Path
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (!(Test-Path $venvPython)) {
  throw "Je ne trouve pas $venvPython. Lance ce script depuis la racine du projet MLOmega ou passe -ProjectRoot C:\MLOmega"
}

& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install fastapi uvicorn python-multipart pillow

Write-Host "OK. Dépendances API téléphone installées dans la venv du projet." -ForegroundColor Green
