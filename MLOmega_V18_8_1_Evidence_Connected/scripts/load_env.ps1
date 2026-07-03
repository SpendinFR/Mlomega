param(
  [string]$EnvPath = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")).Path ".env")
)

if (-not (Test-Path $EnvPath)) {
  throw ".env introuvable: $EnvPath"
}

Get-Content $EnvPath | ForEach-Object {
  $line = $_.Trim()
  if (-not $line -or $line.StartsWith("#")) { return }
  $idx = $line.IndexOf("=")
  if ($idx -lt 1) { return }
  $name = $line.Substring(0, $idx).Trim()
  $value = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
  [Environment]::SetEnvironmentVariable($name, $value, "Process")
}

Write-Host "Variables chargees depuis $EnvPath"
