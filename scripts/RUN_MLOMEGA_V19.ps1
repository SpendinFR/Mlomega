param([switch]$SimOnly,[switch]$Xr)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

if ($SimOnly) {
  Write-Host "Starting MLOmega V19 SimOnly checkpoint demo: fake device -> UIIntent -> companion-web simulator -> UIReceipt."
  python (Join-Path $PSScriptRoot "simonly_demo_v19.py")
  exit $LASTEXITCODE
}

if ($Xr) {
  Write-Host "XR hardware launch is not available in this checkpoint container. Run with -SimOnly for the Lot 1 validated path."
  exit 2
}

Write-Host "Usage: ./scripts/RUN_MLOMEGA_V19.ps1 -SimOnly"
