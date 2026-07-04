// MLOmega V19 — E26
// WakeWordGate: the configurable wake word (from MLOmegaConfig, spotted by the
// on-device sherpa-onnx KeywordSpotter) arms command listening for a bounded
// window and shows StatusBar feedback ("listening…"). While listening it disarms
// the spotter (so the wake phrase inside a command doesn't re-trigger) and re-arms
// when the window closes or a command is captured. Runs fully on-device (handoff
// §3.2). Emits its StatusBar UIIntent through the shared LocalIntentSource seam.
using System;
using System.Collections.Generic;
using MLOmega.Contracts.V19;
using MLOmega.XR.Core;
using MLOmega.XR.UI;
using UnityEngine;

namespace MLOmega.XR.Reflex.Skills
{
    public sealed class WakeWordGate : MonoBehaviour
    {
        [SerializeField] private LocalIntentSource _intentSource;
        [SerializeField] private AsrBridge _asr;

        [Tooltip("How long command listening stays armed after the wake word (seconds).")]
        [Min(1f)]
        [SerializeField] private float _listenWindowSeconds = 6f;

        /// <summary>Raised when the wake word arms command listening (for the FocusSearch / command router).</summary>
        public event Action<long> ListeningStarted;

        /// <summary>Raised when the listening window closes without a command.</summary>
        public event Action ListeningStopped;

        /// <summary>Whether command listening is currently armed.</summary>
        public bool Listening { get; private set; }

        private float _listenUntil;

        private void Awake()
        {
            if (_intentSource == null) _intentSource = FindAnyObjectByType<LocalIntentSource>();
            if (_asr == null) _asr = FindAnyObjectByType<AsrBridge>();
        }

        private void OnEnable()
        {
            if (_asr != null) _asr.WakeWordSpotted += OnWakeWord;
        }

        private void OnDisable()
        {
            if (_asr != null) _asr.WakeWordSpotted -= OnWakeWord;
        }

        private void Update()
        {
            if (Listening && Time.unscaledTime >= _listenUntil)
            {
                StopListening();
            }
        }

        /// <summary>Handle a spotted wake word (also the injection point for tests).</summary>
        public void OnWakeWord(string keyword, long tsMs)
        {
            _listenUntil = Time.unscaledTime + _listenWindowSeconds;
            if (Listening)
            {
                EmitStatus(true); // refresh the window
                return;
            }
            Listening = true;
            _asr?.SetWakeWordArmed(false); // don't re-trigger on the command's words
            EmitStatus(true);
            ListeningStarted?.Invoke(tsMs);
        }

        /// <summary>Called by the command router once a command was captured (re-arm early).</summary>
        public void CommandCaptured() => StopListening();

        private void StopListening()
        {
            if (!Listening) return;
            Listening = false;
            _asr?.SetWakeWordArmed(true);
            EmitStatus(false);
            ListeningStopped?.Invoke();
        }

        private void EmitStatus(bool listening)
        {
            if (_intentSource == null) return;
            var intent = new UIIntent
            {
                ContractsVersion = ContractDefaults.Version,
                UiIntentId = "ul_wakeword_status",
                Producer = "ultralive",
                Component = "status_bar",
                TruthLevel = "observed",
                Confidence = 1.0,
                Priority = 1.0,
                TtlMs = listening ? (long)(_listenWindowSeconds * 1000f) : 1200,
                Content = new Dictionary<string, object>
                {
                    { "text", listening ? "listening…" : "" },
                    { "listening", listening }
                },
                UiHint = new Dictionary<string, object> { { "channel", "mic" } },
                Anchor = new Dictionary<string, object>(),
                EvidenceRefs = new List<string>()
            };
            _intentSource.Emit(intent);
        }
    }
}
