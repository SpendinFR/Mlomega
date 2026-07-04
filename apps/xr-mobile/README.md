# apps/xr-mobile — MLOmega V19 XR client (Unity 6 LTS)

Unity 6 LTS (`6000.0.23f1`) app for the XREAL One + Eye glasses, with a first-class
phone-only path and an in-editor simulator. Covers gate **G1** (E22) and the **core
capture/session runtime** (E23).

## Prerequisites

- Unity **6000.0.23f1** (Unity 6 LTS), Android build support (IL2CPP, ARM64).
- **XREAL SDK 3.1.0** — proprietary, **not committed**. Download `com.xreal.xr.tar.gz`
  from <https://developer.xreal.com/download> and place it at
  `Packages/xreal-sdk/com.xreal.xr.tar.gz` (git-ignored). `Packages/manifest.json`
  resolves it via UPM `file:`.
- **Newtonsoft.Json** — added as `com.unity.nuget.newtonsoft-json` in
  `Packages/manifest.json` (official Unity package, restored automatically).

To activate the real XREAL capture path, add the scripting define
`XREAL_SDK_PRESENT` (Player Settings → Android → Scripting Define Symbols).

## Project layout

```
Assets/Scripts/
  Contracts/    synced V19 contract POCOs (Newtonsoft) — MLOmega.Contracts asmdef
  Core/         device adapters, session, capture, pose, clock-sync — MLOmega.XR.Core
  Editor/       G1 scene builder + contract sync tool — MLOmega.XR.Editor
Assets/Tests/EditMode/   Unity Test Framework EditMode suite
Assets/Shaders/          YUV420ToRGB (Eye YUV_420_888 -> RGB)
Assets/Plugins/Android/  AndroidManifest (permissions)
```

## Contracts (E23)

`Assets/Scripts/Contracts/*.cs` are **synchronised copies** of
`packages/contracts/csharp/*.cs` (the generated source of truth). They are rewritten
for Unity: `System.Text.Json` → **Newtonsoft.Json**, C# 10 file-scoped namespace →
block-scoped, and the duplicate nested `ReflexEvent` in `HotSceneContext.cs` dropped.

Re-sync after a schema change: menu **MLOmega → Contracts → Sync from repo**
(`Editor/SyncContracts.cs`). Do not hand-edit the copies. Requires a full monorepo
checkout so `../../packages/contracts/csharp` resolves.

## Configuration

Create an **MLOmegaConfig** asset: `Assets → Create → MLOmega → Config → MLOmega Config`
(or `Create → MLOmega/Config/MLOmega Config`). It declares:

- PC SessionHub host / port (V19 uses the **87xx** range, never 8766) and device id.
- **Adapter** (`Auto | Xreal | Simulated | PhoneOnly`) — mirrors
  `configs/user_profile.yaml` `display`/`capture` (handoff §3.5):
  - `Xreal` ⟵ `display: xreal_one_pro` + `capture: xreal_eye`
  - `PhoneOnly` ⟵ `display: phone_only` + `capture: phone_camera`
  - `Simulated` ⟵ editor / `companion_web` dev
  - `Auto` ⟵ editor→Simulated, device→Xreal
- Clock-sync interval / burst size / retries, token renew lead, capture fps, pose Hz.

Assign the config to `XrSessionController` (adapter selection) and `SessionPairing`.

## Runtime pieces (E23)

- **IXRDeviceAdapter** (+`IsStereo`, `FrameSource`) — `XrealDeviceAdapter`,
  `SimulatedDeviceAdapter`, `PhoneOnlyAdapter` (rear camera, identity pose,
  `IsStereo=false` → flat 2D). `AdapterSelector` builds the one the config names.
- **SessionPairing** — creates a SessionHub session, holds the ephemeral token,
  renews it before expiry, drives periodic **ClockSync**. State: unpaired / pairing /
  paired / expired / error.
- **ClockSync** — client half of `services/live-pc/sessionhub.py`. Offset/RTT math is
  identical to `SessionHub.complete_clock_sync` (floor-div-by-2 like Python `// 2`);
  keeps the lowest-RTT sample of a burst. Bounded retries; `Unsynced` on failure.
- **EyeCaptureSource** — builds contract `FrameEnvelope`s (`f_<n>` frame_id,
  `capture_monotonic_ns`, ISO-8601 `captured_at_utc`, pose sampled **at capture**,
  rotation field for capture-only, per-adapter `source`) and raises
  `OnFrame(Texture, FrameEnvelope)` for the E24 transport. Allocation-free steady
  state.
- **PosePublisher** — samples 6DoF at capture and on its own cadence; converts to the
  contract `Pose`; shares the session monotonic clock.

## Tests (EditMode)

`Assets/Tests/EditMode/` runs in **Window → General → Test Runner → EditMode**:
ClockSync numeric symmetry with `tests/v19/test_sessionhub.py`, FrameEnvelope field /
monotonicity / frame_id formatting, and Newtonsoft JSON round-trips (snake_case keys).
They require no hardware and pass on the first click.

## Gate G1 hardware checklist (E22, validated on device)

Run on a real Samsung S25 + XREAL One (+ Eye) and confirm, via the always-on
`G1StatusOverlay`:

1. Session starts, a timestamped `session_id` appears.
2. 6DoF **pose** reads OK and updates as you move your head.
3. **Eye** capture OK: the preview quad shows live RGB, frame counter climbs, fps > 0.
4. **Permissions** `CAMERA` / `RECORD_AUDIO` / `FOREGROUND_SERVICE_MEDIA_PROJECTION`
   granted.
5. Unplug/replug the glasses mid-session → the session **suspends then resumes**.

**Plan B** (if the Eye is inaccessible on this unit — the doc says "One series"
without naming One Pro): fall back to pose via `one-xr` (MIT, Kotlin) + the S25
camera through `PhoneOnlyAdapter`, same pipeline. See `docs/DECISIONS.md`.

> Note: the C# is written for doc fidelity and reviewed, but the environment that
> produced it has no Unity/.NET SDK, so it is **not compiler-verified**. Final
> validation is on hardware (this checklist) coupled to the G1 gate.
