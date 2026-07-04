// MLOmega V19 — E26
// Unity-side bridge to the native Android speech pipeline
// (com.mlomega.xr.reflexvision.AsrKwsService, sherpa-onnx VAD + streaming
// zipformer ASR + KeywordSpotter). Owns the AndroidJavaObject, activates on
// demand (§9.4), and re-emits transcripts + wake-word hits as C# events on the
// main thread.
//
// Editor / Windows dev has no Android plugin, so a REAL editor input path runs
// instead: it captures the Windows microphone via Unity's Microphone API and
// runs a basic energy VAD, so the wake-word gate and subtitle skill can be
// developed without a device. When no microphone is present it falls back to a
// keyboard trigger (K = wake word). Same DIRECT_ANDROID / editor split as
// LiveTransportBridge (DECISIONS §E24/§E26).
using System;
using System.Collections.Generic;
using MLOmega.XR.Core;
using UnityEngine;

namespace MLOmega.XR.Reflex
{
    /// <summary>A streaming ASR result surfaced to the reflex layer (main thread).</summary>
    public readonly struct TranscriptEvent
    {
        public readonly string Text;
        public readonly bool IsFinal;
        public readonly string Language;
        public readonly long StartMs;
        public readonly long EndMs;

        public TranscriptEvent(string text, bool isFinal, string language, long startMs, long endMs)
        {
            Text = text;
            IsFinal = isFinal;
            Language = language;
            StartMs = startMs;
            EndMs = endMs;
        }
    }

    public sealed class AsrBridge : MonoBehaviour
    {
        [SerializeField] private MLOmegaConfig _config;

        [Tooltip("Relative dir (under app files dir) of the streaming ASR model for the selected language.")]
        [SerializeField] private string _asrModelRelativeDir = "reflex/asr";

        [Tooltip("Relative path of the Silero VAD model.")]
        [SerializeField] private string _vadRelativePath = "reflex/silero_vad.onnx";

        [Tooltip("Relative dir of the KeywordSpotter model.")]
        [SerializeField] private string _kwsModelRelativeDir = "reflex/kws";

        /// <summary>Raised on the main thread for each partial/final transcript.</summary>
        public event Action<TranscriptEvent> Transcript;

        /// <summary>Raised on the main thread when the configured wake word is spotted.</summary>
        public event Action<string, long> WakeWordSpotted;

        public bool IsRunning { get; private set; }

        private readonly Queue<Action> _mainThreadQueue = new Queue<Action>();
        private readonly object _queueLock = new object();

#if UNITY_ANDROID && !UNITY_EDITOR
        private AndroidJavaObject _service;
        private AsrProxy _proxy;
#endif

        private void Awake()
        {
            if (_config == null) _config = FindAnyObjectByType<SessionPairing>()?.Config;
        }

        private void Update()
        {
            DrainMainThread();
#if UNITY_EDITOR
            if (IsRunning) SimulateFromMicrophone();
#endif
        }

        /// <summary>Activate the speech pipeline (mic + models). Called by scheduler/WakeWordGate. Idempotent.</summary>
        public void Activate()
        {
            if (IsRunning) return;
            IsRunning = true;
#if UNITY_ANDROID && !UNITY_EDITOR
            StartAndroid();
#else
            StartEditorMic();
#endif
        }

        /// <summary>Deactivate the pipeline (release mic + models — §9.4). Idempotent.</summary>
        public void Deactivate()
        {
            if (!IsRunning) return;
            IsRunning = false;
#if UNITY_ANDROID && !UNITY_EDITOR
            _service?.Call("stop");
#else
            StopEditorMic();
#endif
        }

        /// <summary>Arm/disarm the wake-word spotter without tearing down ASR.</summary>
        public void SetWakeWordArmed(bool armed)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            _service?.Call("setWakeWordArmed", armed);
#else
            _editorKwsArmed = armed;
#endif
        }

        private void OnDisable() => Deactivate();

        // --- native plumbing ------------------------------------------------------

#if UNITY_ANDROID && !UNITY_EDITOR
        private void StartAndroid()
        {
            using var activity = new AndroidJavaClass("com.unity3d.player.UnityPlayer")
                .GetStatic<AndroidJavaObject>("currentActivity");
            using var ctx = activity.Call<AndroidJavaObject>("getApplicationContext");
            string filesDir = ctx.Call<AndroidJavaObject>("getFilesDir").Call<string>("getAbsolutePath");

            string lang = (_config != null && _config.AsrLanguage == ReflexAsrLanguage.Fr) ? "FR" : "EN";
            string wake = _config != null ? _config.WakeWord : "hey mlomega";
            using var langEnum = new AndroidJavaClass("com.mlomega.xr.reflexvision.AsrLanguage")
                .CallStatic<AndroidJavaObject>("valueOf", lang);

            // Build the wake-word list (single phrase) as a java.util.ArrayList<String>.
            using var wakeList = new AndroidJavaObject("java.util.ArrayList");
            wakeList.Call<bool>("add", wake);

            var cfg = new AndroidJavaObject(
                "com.mlomega.xr.reflexvision.AsrKwsConfig",
                langEnum,
                filesDir + "/" + _asrModelRelativeDir,
                filesDir + "/" + _vadRelativePath,
                filesDir + "/" + _kwsModelRelativeDir,
                wakeList);

            _proxy = new AsrProxy(this);
            _service = new AndroidJavaObject(
                "com.mlomega.xr.reflexvision.AsrKwsService", ctx, cfg, _proxy);
            _service.Call("start");
        }
#endif

        internal void OnNativeTranscript(string text, bool isFinal, string lang, long startMs, long endMs) =>
            Enqueue(() => Transcript?.Invoke(new TranscriptEvent(text, isFinal, lang, startMs, endMs)));

        internal void OnNativeWakeWord(string keyword, long tsMs) =>
            Enqueue(() => WakeWordSpotted?.Invoke(keyword, tsMs));

        internal void OnNativeError(string message) =>
            Debug.LogWarning($"[AsrBridge] native error: {message}");

        private void Enqueue(Action a) { lock (_queueLock) { _mainThreadQueue.Enqueue(a); } }

        private void DrainMainThread()
        {
            while (true)
            {
                Action work = null;
                lock (_queueLock) { if (_mainThreadQueue.Count > 0) work = _mainThreadQueue.Dequeue(); }
                if (work == null) break;
                try { work(); } catch (Exception ex) { Debug.LogError($"[AsrBridge] {ex}"); }
            }
        }

        /// <summary>
        /// Inject a transcript directly (EditMode tests / demo driver): proves the
        /// SubtitleSkill path without any device/native pipeline.
        /// </summary>
        public void InjectTranscript(TranscriptEvent ev) => Transcript?.Invoke(ev);

        /// <summary>Inject a wake-word hit directly (EditMode tests / demo driver).</summary>
        public void InjectWakeWord(string keyword, long tsMs) => WakeWordSpotted?.Invoke(keyword, tsMs);

        // --- editor microphone VAD (real, not a stub) -----------------------------

#if UNITY_EDITOR
        private AudioClip _micClip;
        private string _micDevice;
        private int _lastMicPos;
        private bool _editorSpeaking;
        private bool _editorKwsArmed = true;
        private float[] _micBuffer;
        private const int EditorSampleRate = 16000;
        private const float VadEnergyThreshold = 0.0025f;

        private void StartEditorMic()
        {
            if (Microphone.devices.Length == 0)
            {
                Debug.Log("[AsrBridge] editor: no microphone; press K to simulate the wake word.");
                return;
            }
            _micDevice = Microphone.devices[0];
            _micClip = Microphone.Start(_micDevice, true, 1, EditorSampleRate);
            _micBuffer = new float[EditorSampleRate / 10];
            _lastMicPos = 0;
        }

        private void StopEditorMic()
        {
            if (!string.IsNullOrEmpty(_micDevice)) Microphone.End(_micDevice);
            _micClip = null;
            _micDevice = null;
        }

        private void SimulateFromMicrophone()
        {
            if (Input.GetKeyDown(KeyCode.K) && _editorKwsArmed)
            {
                long now = (long)(Time.unscaledTimeAsDouble * 1000.0);
                WakeWordSpotted?.Invoke(_config != null ? _config.WakeWord : "hey mlomega", now);
                return;
            }
            if (_micClip == null) return;

            int pos = Microphone.GetPosition(_micDevice);
            if (pos < 0 || pos == _lastMicPos) return;
            int count = pos - _lastMicPos;
            if (count < 0) count += _micClip.samples; // wrapped
            if (count < _micBuffer.Length) return;

            _micClip.GetData(_micBuffer, _lastMicPos % _micClip.samples);
            _lastMicPos = pos;

            // Basic energy VAD: RMS over the last 100 ms window.
            float sum = 0f;
            for (int i = 0; i < _micBuffer.Length; i++) sum += _micBuffer[i] * _micBuffer[i];
            float rms = Mathf.Sqrt(sum / _micBuffer.Length);

            long nowMs = (long)(Time.unscaledTimeAsDouble * 1000.0);
            bool speaking = rms > VadEnergyThreshold;
            if (speaking && !_editorSpeaking)
            {
                _editorSpeaking = true;
                Transcript?.Invoke(new TranscriptEvent("…", false,
                    _config != null && _config.AsrLanguage == ReflexAsrLanguage.Fr ? "fr" : "en",
                    nowMs, nowMs));
            }
            else if (!speaking && _editorSpeaking)
            {
                _editorSpeaking = false;
                Transcript?.Invoke(new TranscriptEvent("(speech segment)", true,
                    _config != null && _config.AsrLanguage == ReflexAsrLanguage.Fr ? "fr" : "en",
                    nowMs, nowMs));
            }
        }
#endif

#if UNITY_ANDROID && !UNITY_EDITOR
        private sealed class AsrProxy : AndroidJavaProxy
        {
            private readonly AsrBridge _bridge;
            public AsrProxy(AsrBridge b)
                : base("com.mlomega.xr.reflexvision.AsrKwsCallbacks") { _bridge = b; }

            void onTranscript(string text, bool isFinal, string language, long startMs, long endMs) =>
                _bridge.OnNativeTranscript(text, isFinal, language, startMs, endMs);
            void onWakeWord(string keyword, long tsMs) => _bridge.OnNativeWakeWord(keyword, tsMs);
            void onError(string message) => _bridge.OnNativeError(message);
        }
#endif
    }
}
