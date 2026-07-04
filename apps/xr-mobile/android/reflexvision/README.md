# reflexvision — MLOmega V19 Ultra-Live sensing (E26)

On-device Android library for the Ultra-Live reflex path. **Contains NO LLM and
NO VLM** (handoff §3.2) — only small specialised calculators that run in
< 100 ms on the device:

| Back-end | Library | Job |
|---|---|---|
| `GesturePipeline` | MediaPipe Tasks Vision `GestureRecognizer` (bundles HandLandmarker), `LIVE_STREAM` | pinch → continuous zoom (begin/update/end), open palm held → menu, lateral swipe → hide UI |
| `AsrKwsService` | sherpa-onnx: Silero VAD + streaming zipformer ASR + `KeywordSpotter` | FR/EN live subtitles (partial/final + timestamps) and a configurable wake word |

Produces an `.aar` vendored into the Unity app (`Assets/Plugins/Android`) and
driven from C# via `GestureBridge.cs` / `AsrBridge.cs`. It is only activated on
demand by the Unity `ReflexScheduler` (GUIDE_V19 §9.4 — never all detectors in
parallel; battery). Same conventions as the E24 `livetransport` module.

> **Build status:** this module cannot be compiled in the authoring environment
> (no Android SDK). It is written against the pinned APIs below; the real compile
> + on-device validation is the S25 gate (ADR `docs/DECISIONS.md` §E26). The pure
> logic (`GestureStateMachine`, `KeywordEncoder`) is covered by JVM unit tests in
> `src/test` and runs with plain `./gradlew test`.

## Pinned dependencies

| Dependency | Version | License | Source |
|---|---|---|---|
| `com.google.mediapipe:tasks-vision` | `0.10.29` | Apache-2.0 | Maven Central |
| `com.github.k2-fsa:sherpa-onnx-android` | `1.12.10` | Apache-2.0 | JitPack (or vendored static AAR, below) |
| `org.jetbrains.kotlinx:kotlinx-coroutines-android` | `1.8.1` | Apache-2.0 | Maven Central |

If a LAN-only build cannot reach JitPack, download the pre-built static AAR from
the sherpa-onnx GitHub release (`sherpa-onnx-v1.12.10-android.tar.bz2`), drop it
in `libs/`, and swap the dependency to
`implementation(files("libs/sherpa-onnx.aar"))`.

## Models (NOT committed — download to app storage at first run)

Weights are never checked in. Install them under the app's files dir and pass the
absolute directories in `AsrKwsConfig` / `GestureConfig`.

### Gestures (MediaPipe `.task` bundle)

- `gesture_recognizer.task`
  <https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/latest/gesture_recognizer.task>
  → install to `<filesDir>/reflex/gesture_recognizer.task`
  → `GestureConfig.modelAssetPath`

### ASR — streaming zipformer (choose by language)

- **EN**: `sherpa-onnx-streaming-zipformer-en-2023-06-26`
  <https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2>
- **FR**: `sherpa-onnx-streaming-zipformer-fr-2023-04-14`
  <https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-fr-2023-04-14.tar.bz2>

Extract to `<filesDir>/reflex/asr-<lang>/` so that `encoder.onnx`, `decoder.onnx`,
`joiner.onnx`, `tokens.txt` sit directly inside → `AsrKwsConfig.asrModelDir`.

### VAD (Silero)

- `silero_vad.onnx`
  <https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx>
  → `<filesDir>/reflex/silero_vad.onnx` → `AsrKwsConfig.vadModelPath`

### Wake word — KeywordSpotter (streaming zipformer KWS)

- `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01` (English wake words)
  <https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2>

Extract to `<filesDir>/reflex/kws/` → `AsrKwsConfig.kwsModelDir`. The wake phrase
itself is **configurable** (from `MLOmegaConfig` on the Unity side): it is encoded
at runtime by `KeywordEncoder` into the sherpa keywords file the spotter loads, so
changing the wake word needs no rebuild and no new model.

> The KWS model ships a `bpe.model`; for phrases outside its whole-word vocabulary,
> pre-tokenise once with `sherpa-onnx-cli text2token --tokens tokens.txt
> --tokens-type bpe --bpe-model bpe.model "hey mlomega" out.txt` and pass the
> resulting token string as the wake word (it is passed through verbatim).

## Permissions

The library manifest declares `RECORD_AUDIO`, `FOREGROUND_SERVICE` and
`FOREGROUND_SERVICE_MICROPHONE`, and the `MicForegroundService` that holds the
background-mic slot with a privacy-visible notification (GUIDE_V19 §15.2). Camera
frames for MediaPipe come from the Unity capture path, so this library does not
request `CAMERA`.
