<# Compatibility entrypoint. V18.7 deliberately forwards legacy names to the verified core installer. #>
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$HfToken,
  [string]$PersonId="me",
  [switch]$SkipAutoInstallPrerequisites,
  [switch]$KeepPreviousVenv,
  [switch]$SkipHeavyModelSmoke
)
$target = Join-Path $PSScriptRoot "INSTALL_MLOMEGA_V18_7_WINDOWS.ps1"
& $target @PSBoundParameters
exit $LASTEXITCODE
