param([string]$Display='phone_only',[string]$Capture='phone_camera',[string]$Llm='ollama_local')
New-Item -ItemType Directory -Force configs | Out-Null
@"
display: $Display
capture: $Capture
llm: $Llm
vision: onnx_local
asr: local
cloud_data_policy: local_only
"@ | Set-Content -Encoding UTF8 configs/user_profile.yaml
Write-Host "Wrote configs/user_profile.yaml"
