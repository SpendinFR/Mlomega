# MLOmega V18.7 — Core Windows Release

## Supported profile

This release supports one intentionally narrow production path:

```text
Android / Phone Bridge
→ BrainLive live capture
→ safe final inbox drain
→ deep audio (WhisperX + Pyannote + SpeechBrain)
→ deep image vision (offline VLM)
→ Brain2
→ day-level longitudinal / Life Model / live-ready
→ cleanup gate
```

Qdrant is the only infrastructure service. Graphiti, Neo4j and Mem0 are deliberately excluded from installation, startup, health checks and normal post-stop work.

## Canonical commands

From an elevated PowerShell in the release folder:

```powershell
.\INSTALL_MLOMEGA_V18_7_WINDOWS.ps1 -HfToken "hf_..."
.\RUN_MLOMEGA_V18_7.ps1 -PersonId me
```

Stop normally:

```powershell
.\STOP_MLOMEGA_V18_7.ps1 -PersonId me
```

Resume after a timeout, service crash or PC shutdown:

```powershell
.\RESUME_MLOMEGA_V18_7.ps1 -PersonId me
```

`RESUME` first acknowledges any retained inbox, then resumes the exact same close-day checkpoint. It does not create a new day or redo completed stages.

## Installation contract

The installer creates a separate `.venv`, so it does not overwrite a global Python or PyTorch installation. It only returns success after all of the following have passed:

- Windows 64-bit, administrator rights, free disk/RAM/VRAM checks, NVIDIA driver and Docker readiness;
- isolated pinned Python environment plus `pip check`;
- Qdrant health;
- Ollama downloads for `qwen3.5:9b`, `moondream` and `qwen3-vl:8b`;
- actual LLM and VLM responses;
- actual WhisperX, alignment, Pyannote, Silero, SpeechBrain, embedding and reranker loads;
- SQLite schema initialization;
- temporary Phone Bridge health, matching project root and `allow_post_stop=true`.

On a failed install, the previous virtual environment and previous `.env` are restored where they existed. No failed preflight is reported as success.

## Recovery contract

- Local model transport errors use bounded retries and phase-aware, long post-stop timeouts.
- A failed deep-audio bundle, deep-vision frame or Brain2 conversation retains its own durable checkpoint.
- Completed units are skipped on the next post-stop invocation.
- A PID-aware service run turns into `orphaned` immediately after a confirmed dead local process, instead of waiting for a stale heartbeat.
- A pending inbox blocks post-stop and cleanup. Recovery drains it in `drain_only` mode: no nightly or hot LLM work is launched while simply acknowledging final media.
- Source deletion is only permitted after the post-stop and close-day cleanup gates both complete.

## Phone side

The installer generates `MLOmega_Phone_Bridge_V18_7/android/mlomega_android_config.env.v18_7.generated` when it can detect a Tailscale or LAN address. The bundled Android template uses port `8766`.

Windows cannot grant Android microphone/camera/GPS/battery permissions or copy a file into Termux. Those physical device permissions and the first Android configuration transfer remain outside the PC installer. The PC-side bridge itself is validated during installation and again before each `RUN`.
