# apps/xr-mobile — MLOmega V19 XR (Gate G1)

Minimal Unity 6 LTS app for **XREAL** glasses that satisfies build gate **G1**:
a permanent status bar (device / pose / fps / battery / permissions), a live
**Eye RGB** camera preview with `frame_id` + a monotonic clock, a **6DoF pose**
readout, and **stereo** rendering.

> **This project cannot be compiled in the automation container** (no Unity, no
> Android SDK installed). It was authored for fidelity to the official XREAL
> documentation and C# rigor. The real validation is **on hardware** — follow
> the checklist at the end of this file on your Samsung S25 + XREAL glasses.

The proprietary **XREAL SDK 3.1.0** is **not** committed (license). You download
it and drop the tarball in one documented location (step 3 below); it is
git-ignored.

---

## 0. What you need

- Samsung **S25** (Android 10+), USB-C.
- **XREAL One** series glasses, ideally with the **Eye** RGB camera accessory
  (see the hardware caveat in the checklist — plan B is documented).
- A PC with **Unity Hub**.

---

## 1. Install Unity 6 LTS + Android Build Support

1. Install **Unity Hub** from <https://unity.com/download>.
2. In Unity Hub → *Installs* → *Install Editor* → pick **Unity 6 LTS**
   (this project pins `6000.0.23f1` in `ProjectSettings/ProjectVersion.txt`; any
   `6000.0.x` LTS is fine — XREAL SDK 3.1.0 supports Unity 6000.0.X LTS).
3. In the module selection, **check**:
   - **Android Build Support**
   - **OpenJDK**
   - **Android SDK & NDK Tools**
4. Finish the install.

## 2. Download the XREAL SDK 3.1.0

1. Go to <https://developer.xreal.com/download> (official) and download the
   **XREAL SDK 3.1.0** Unity package. It ships as a tarball named
   **`com.xreal.xr.tar.gz`**.
   - Docs for reference: <https://docs.xreal.com/Getting%20Started%20with%20XREAL%20SDK>

## 3. Place the SDK tarball (exact path)

Put the downloaded file here (create the folder if needed):

```
apps/xr-mobile/Packages/xreal-sdk/com.xreal.xr.tar.gz
```

This exact path is referenced by `Packages/manifest.json`
(`"com.xreal.xr": "file:xreal-sdk/com.xreal.xr.tar.gz"`) and is **git-ignored**
(`.gitignore`), so the proprietary SDK is never committed. If you keep it
elsewhere, either move it here or update that manifest line.

## 4. Open the project

1. Unity Hub → *Open* → select the `apps/xr-mobile/` folder.
2. On first open Unity resolves packages, including the XREAL tarball via UPM.
   - If Unity reports the XREAL package cannot be found, re-check step 3.
   - Alternatively, import it manually: *Window → Package Manager → + → Add
     package from tarball…* → select `Packages/xreal-sdk/com.xreal.xr.tar.gz`
     (this is the method the XREAL docs describe).

## 5. Enable the XREAL XR provider

1. *Edit → Project Settings → XR Plug-in Management*.
2. Select the **Android** tab.
3. **Check “XREAL”** as the plug-in provider.
   (`ProjectSettings/XRPackageSettings.asset` records the intended provider, but
   Unity re-authors this file when you tick the box — that is expected.)

## 6. Enable real Eye/pose code (scripting define)

The device adapter (`Assets/Scripts/Core/XrealDeviceAdapter.cs`) guards all
XREAL SDK calls behind the `XREAL_SDK_PRESENT` define so the project compiles
before the SDK is installed. After the SDK is imported:

1. *Edit → Project Settings → Player → Android → Other Settings → Script
   Compilation → Scripting Define Symbols*.
2. Add **`XREAL_SDK_PRESENT`** and Apply.

Without this define the app still runs but the XREAL adapter reports
`Disconnected` (use the simulator in the editor — see below).

## 7. Confirm the required Android Project Settings

These are pre-set in `ProjectSettings/ProjectSettings.asset` (per the XREAL
docs). Verify under *Project Settings → Player → Android*:

| Setting | Value |
|---|---|
| Scripting Backend | **IL2CPP** |
| Target Architectures | **ARM64** only |
| Minimum API Level | **Android 10 (API 29)** |
| Auto Graphics API | **off**, Graphics API = **OpenGLES3** |
| VSync Count | **Don't Sync** |
| Default Orientation | **Landscape Left** |

## 8. Build the G1 scene

1. *MLOmega → Build G1 Gate Scene* (menu added by
   `Assets/Scripts/Editor/G1SceneBuilder.cs`). This generates and saves
   `Assets/Scenes/G1Gate.unity` and adds it to Build Settings.
   - We generate the scene from a script rather than committing a hand-written
     `.unity` — see `docs/DECISIONS.md` (“E22 G1 Unity”).
2. Press **Play** in the editor to smoke-test with the **SimulatedDeviceAdapter**
   (real webcam frames + synthetic pose; no glasses needed). The overlay should
   show a moving pose, incrementing `frame_id`, and fps.

## 9. Build & install the debug APK

1. *File → Build Settings → Android → Switch Platform* (if not already).
2. Ensure `Scenes/G1Gate` is checked in the scene list.
3. **Development Build** on → **Build** → save the APK.
4. Connect the S25 by USB (enable *Developer options → USB debugging*).
5. Install:
   ```
   adb install -r path/to/G1Gate.apk
   ```
   or copy the APK to the phone and tap to install.

## 10. Run with the glasses

1. Plug the XREAL glasses into the S25 (USB-C). Attach the **Eye** accessory if
   available.
2. Launch **MLOmega XR G1 Gate** on the phone.
3. Grant the permission prompts (camera / microphone). The overlay reflects each.

---

## G1 exit checklist (run on real S25 + XREAL)

Tick every box before marking E22 `[x]` in `docs/EXECUTOR_BUILD_GUIDE.md`.

- [ ] **Stereo render OK** — the scene displays correctly in both eyes through
      the glasses (no mono / no double image).
- [ ] **Pose moves with your head** — the `pose` readout position/rotation
      change as you move; `pose: OK`, sample rate > 0 Hz.
- [ ] **Eye image visible** — the preview quad shows the live Eye RGB feed;
      `eye: OK`, `frame_id` increments, fps shown and > 0.
- [ ] **frame_id + monotonic clock** — `frame #` rises monotonically and the
      `t=… ns` timestamp advances.
- [ ] **Permissions granted** — overlay shows `cam:OK mic:OK proj:OK`.
- [ ] **Battery reported** — overlay shows a battery `%` (not `n/a`).
- [ ] **Unplug → replug resumes** — disconnect the glasses; session goes
      `Suspended`; reconnect; session returns to `Running` on its own.
- [ ] **Battery / temperature noted** — record the battery drain and glasses/
      phone temperature over ~10 min of use (for the thermal budget).
- [ ] **No crash / no permission loop** over a 5-minute run.
- [ ] **Session id stable** — the `session:` line keeps one `xrs-…` id for the
      whole run (a new one only after a full restart).

### Plan B — if the Eye camera is inaccessible

The XREAL docs say RGB Eye capture is supported on the **One series** but do not
explicitly confirm **One Pro**; raw sensors are Enterprise-only. If `eye` stays
`KO`/`waiting` and no frames arrive on your unit:

1. The app already degrades gracefully to **pose-only** (the session stays
   `Running`, the overlay shows `eye: KO`). Pose + stereo can still pass G1
   partially.
2. For a full pipeline without the Unity Eye path, use **`one-xr`**
   (<https://github.com/Skarian/one-xr>, MIT, native Kotlin) to read the XREAL
   **IMU / 6DoF pose**, and use the **S25 rear camera** as the RGB source (same
   downstream pipeline). Record this fallback as an ADR in `docs/DECISIONS.md`
   and continue the Lot 3 work against the simulator.

---

## Developing without glasses (editor)

`XrSessionController.UseSimulator` returns true in the editor, so **Play** always
uses `SimulatedDeviceAdapter`:

- If a webcam is present it streams **real `WebCamTexture` frames**.
- Otherwise it renders an animated procedural texture (still live, changing
  frames) so the preview/fps path is exercised.
- Pose is a smooth synthetic 6DoF sweep so `PoseReadout` visibly moves.

This is a first-class development path defined by the build plan, not a stub.

## File map

```
apps/xr-mobile/
  Packages/manifest.json                 URP, XR Mgmt, Input System, TMP, XREAL (file:)
  Packages/xreal-sdk/                     <- you drop com.xreal.xr.tar.gz here (git-ignored)
  ProjectSettings/                        Unity 6 LTS + Android (ARM64/IL2CPP/GLES3/minSdk29)
  Assets/Plugins/Android/AndroidManifest.xml   doc-exact permissions
  Assets/Shaders/YUV420ToRGB.shader       Eye YUV_420_888 -> RGB blit
  Assets/Scripts/Core/
    IXRDeviceAdapter.cs                   device contract
    XrealDeviceAdapter.cs                 real XREAL SDK 3.1.0 impl
    SimulatedDeviceAdapter.cs             webcam + synthetic pose (editor)
    XrSessionController.cs                session_id / suspend / resume
    EyeCapturePreview.cs                  frame_id + monotonic ns + fps
    PoseReadout.cs                        6DoF readout + Hz
    PermissionGate.cs                     runtime Android permissions
    G1StatusOverlay.cs                    the permanent status panel
  Assets/Scripts/Editor/G1SceneBuilder.cs builds Assets/Scenes/G1Gate.unity
```
