# MLOmega Phone Bridge V18.8 — reprise sûre de clôture

The receiver keeps the familiar PowerShell switch:

```powershell
.\run_brainlive_phone_receiver.ps1 -Token "..." -ProjectRoot C:\MLOmega -AllowPostStopOnSessionStop
```

In V18.8 this no longer means "legacy post-stop then immediate purge". A phone `/session/stop` now requests the full gated sequence:

```text
drain -> BrainLive stop -> session post-stop -> V18.8 day longitudinal
-> coordination -> Life Model -> live-ready -> cleanup gate -> raw-media purge only if eligible
```

The Android sidecars include a stable `source_event_id` based on the device, capture kind and capture id. A network retry of the same capture reuses the queue item and inbox target instead of creating a second raw record.

Do not call `/cleanup-media` to bypass the flow: it returns a conflict until `brainlive-close-day-status --person-id me` reports a cleanup-eligible completed day.


## V18.8 resilience

The bridge only purges acknowledged media after the V18.8 close-day retention gate. A retryable close-day result does not purge sources; it remains resumable through `RESUME_MLOMEGA_V18_8.ps1`.
