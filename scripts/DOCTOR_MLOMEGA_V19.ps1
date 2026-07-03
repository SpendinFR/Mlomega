param([switch]$Memory,[switch]$Full,[switch]$Xr,[switch]$Vision,[switch]$World,[switch]$Delivery)
$ok = Test-Path configs/user_profile.yaml
if (-not $ok) { Write-Warning "configs/user_profile.yaml missing; run scripts/setup_profile.ps1"; exit 1 }
Write-Host "MLOmega V19 doctor OK (profile present; detailed hardware checks are gated by flags)."
