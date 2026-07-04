param(
    [switch]$Webrtc,
    [switch]$Vision,
    [int]$Frames = 0
)
$ErrorActionPreference = "Stop"
$bench = Join-Path $PSScriptRoot "bench_v19_sim.py"
$args = @()
if ($Vision) {
    Write-Host "Running MLOmega V19 vision bench (real YOLOX detector + ByteTrack)."
    $args += "--vision"
} elseif ($Webrtc) {
    Write-Host "Running MLOmega V19 WebRTC loopback bench (real aiortc H.264 decode)."
    $args += "--webrtc"
} else {
    Write-Host "Running MLOmega V19 simulated ingress bench (queue path only)."
    $args += "--sim"
}
if ($Frames -gt 0) { $args += @("--frames", "$Frames") }
python $bench @args
exit $LASTEXITCODE
