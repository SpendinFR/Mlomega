$ErrorActionPreference = "Stop"
Write-Host "Running MLOmega V19 simulated ingress bench (no hardware/H.264 decode in this container)."
python (Join-Path $PSScriptRoot "bench_v19_sim.py")
exit $LASTEXITCODE
