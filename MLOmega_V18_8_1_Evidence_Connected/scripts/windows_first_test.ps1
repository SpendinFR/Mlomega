<# Compatibility health gate for V18.8; use RUN_MLOMEGA_V18_8.ps1 for capture. #>
[CmdletBinding()]
param([switch]$CheckModels,[switch]$CheckVectors,[switch]$CheckBridge)
$ErrorActionPreference="Stop"
$root=(Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$envPath=Join-Path $root ".env"; $python=Join-Path $root ".venv\Scripts\python.exe"
if(!(Test-Path $python)){throw ".venv absent : lance INSTALL_MLOMEGA_V18_8_WINDOWS.ps1"}
Get-Content $envPath | ForEach-Object { if($_ -notmatch '^\s*#' -and $_ -match '='){ $p=$_ -split '=',2; Set-Item "Env:$($p[0].Trim())" $p[1].Trim() } }
$env:MLOMEGA_PROJECT_ROOT=$root
$args=@("-m","mlomega_audio_elite.cli","doctor-core-v18-8","--fail")
if($CheckModels){$args+="--check-models"};if($CheckVectors){$args+="--check-vectors"};if($CheckBridge){$args+="--check-bridge"}
& $python @args
exit $LASTEXITCODE
