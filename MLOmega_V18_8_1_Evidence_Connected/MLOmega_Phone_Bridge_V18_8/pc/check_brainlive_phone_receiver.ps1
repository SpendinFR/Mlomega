param(
  [string]$BaseUrl = "http://127.0.0.1:8766",
  [Parameter(Mandatory=$true)][string]$Token
)

$ErrorActionPreference = "Stop"
Write-Host "Health:" -ForegroundColor Cyan
Invoke-RestMethod -Uri "$BaseUrl/health" -Method GET | ConvertTo-Json -Depth 8
Write-Host "Status:" -ForegroundColor Cyan
Invoke-RestMethod -Uri "$BaseUrl/status" -Method GET -Headers @{"X-MLomega-Token"=$Token} | ConvertTo-Json -Depth 8
