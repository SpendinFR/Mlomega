<# Compatibility entrypoint for the canonical V18.8 core installer. #>
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$HfToken,
  [string]$PersonId="me",
  [switch]$SkipAutoInstallPrerequisites,
  [switch]$KeepPreviousVenv,
  [switch]$SkipHeavyModelSmoke
)
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
& (Join-Path $root "INSTALL_MLOMEGA_V18_8_WINDOWS.ps1") @PSBoundParameters
exit $LASTEXITCODE
