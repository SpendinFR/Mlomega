// MLOmega V19 — E24
// Unity-side bridge to the native Android live transport (LiveTransportPlugin,
// GetStream webrtc-android). Owns the AndroidJavaObject, feeds the eye/phone
// texture from EyeCaptureSource.OnFrame (E23) up the WebRTC video track, relays
// contract messages (UIIntent down / UIReceipt up) over the reliable DataChannel,
// and re-emits the native connection state as C# events for the StatusBar (E25).
//
// Platform matrix (documented decision, DECISIONS.md §E24):
//   - Android device build: DIRECT_ANDROID — the real Kotlin plugin runs.
//   - Editor / Windows dev: DIRECT_PYTHON — there is no Android plugin, so the
//     transport is a no-op here; the PC side is exercised by fake_xr_device
//     (SimulatedDeviceAdapter path) talking to the same /webrtc/offer endpoint.
//     This bridge still parses/echoes contract messages so UI wiring can be
//     developed in the editor against a locally injected message stream.
using System;
using System.Collections.Generic;
using MLOmega.Contracts.V19;
using MLOmega.XR.Core;
using Newtonsoft.Json;
using UnityEngine;

namespace MLOmega.XR.Transport
{
    /// <summary>Connection state surfaced to Unity; mirrors the Kotlin TransportState.</summary>
    public enum LiveTransportState
    {
        Disconnected = 0,
        Connecting = 1,
        Connected = 2,
        Degraded = 3,
        Reconnecting = 4
    }

    /// <summary>
    /// MonoBehaviour wrapper around the native transport. Assign the session
    /// credentials (from <see cref="SessionPairing"/>) and an
    /// <see cref="EyeCaptureSource"/>; call <see cref="StartTransport"/> once the
    /// session token is available.
    /// </summary>
    public sealed class LiveTransportBridge : MonoBehaviour
    {
        [SerializeField] private SessionPairing _pairing;
        [SerializeField] private EyeCaptureSource _capture;

        [Tooltip("Nominal capture width/height/fps advertised to the encoder.")]
        [SerializeField] private int _width = 1280;
        [SerializeField] private int _height = 720;
        [SerializeField] private int _fps = 30;

        [Tooltip("Feed frames as OES textures (zero-copy) vs I420 CPU readback.")]
        [SerializeField] private bool _textureBacked = true;

        /// <summary>Raised on the main thread when the transport state changes.</summary>
        public event Action<LiveTransportState, string> StateChanged;

        /// <summary>Raised on the main thread for each UIIntent received downlink.</summary>
        public event Action<UIIntent> UiIntentReceived;

        /// <summary>Raised on the main thread with the raw downlink JSON before typed
        /// parsing — lets the DeviceCommandHandler (E33 §4) claim `device_command`
        /// messages, which are NOT UIIntents.</summary>
        public event Action<string> MessageReceived;

        /// <summary>Raised on the main thread with a raw stats JSON snapshot.</summary>
        public event Action<string> StatsReceived;

        /// <summary>Latest known state.</summary>
        public LiveTransportState State { get; private set; } = LiveTransportState.Disconnected;

#if UNITY_ANDROID && !UNITY_EDITOR
        private AndroidJavaObject _plugin;
        private AndroidJavaObject _feeder;
        private NativeCallbackProxy _proxy;
#endif

        // Main-thread dispatch: native callbacks arrive on background threads.
        private readonly Queue<Action> _mainThreadQueue = new Queue<Action>();
        private readonly object _queueLock = new object();

        private void Awake()
        {
            if (_pairing == null) _pairing = FindAnyObjectByType<SessionPairing>();
            if (_capture == null) _capture = FindAnyObjectByType<EyeCaptureSource>();
        }

        private void OnEnable()
        {
            if (_capture != null) _capture.OnFrame += HandleFrame;
        }

        private void OnDisable()
        {
            if (_capture != null) _capture.OnFrame -= HandleFrame;
        }

        private void Update()
        {
            // Drain native callbacks onto the Unity main thread.
            while (true)
            {
                Action work = null;
                lock (_queueLock)
                {
                    if (_mainThreadQueue.Count > 0) work = _mainThreadQueue.Dequeue();
                }
                if (work == null) break;
                try { work(); } catch (Exception ex) { Debug.LogError($"[LiveTransport] {ex}"); }
            }
        }

        /// <summary>
        /// Start the native transport. Requires a paired session (session id +
        /// token). No-op in editor/Windows (DIRECT_PYTHON): the PC-side loop is
        /// driven by fake_xr_device against the same signaling endpoint.
        /// </summary>
        public void StartTransport()
        {
            if (_pairing == null || string.IsNullOrEmpty(_pairing.SessionId))
            {
                Debug.LogWarning("[LiveTransport] no paired session; cannot start.");
                return;
            }
#if UNITY_ANDROID && !UNITY_EDITOR
            StartAndroid();
#else
            Debug.Log("[LiveTransport] editor/Windows: DIRECT_PYTHON mode, native transport skipped. " +
                      "Drive the PC side with simulators/fake_xr_device against /webrtc/offer.");
            SetState(LiveTransportState.Disconnected, "editor-noop");
#endif
        }

        /// <summary>Stop and release the native transport.</summary>
        public void StopTransport()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            _plugin?.Call("stop");
#endif
            SetState(LiveTransportState.Disconnected, "stopped");
        }

        /// <summary>
        /// Send a UIReceipt back up the reliable DataChannel (the device's ack of
        /// a UIIntent). No-op if the channel is not open.
        /// </summary>
        public bool SendReceipt(UIReceipt receipt)
        {
            string json = JsonConvert.SerializeObject(receipt);
#if UNITY_ANDROID && !UNITY_EDITOR
            return _plugin != null && _plugin.Call<bool>("sendContractMessage", json);
#else
            Debug.Log($"[LiveTransport] (editor) would send receipt: {json}");
            return false;
#endif
        }

        // --- frame feeding --------------------------------------------------------

        private void HandleFrame(Texture texture, FrameEnvelope envelope)
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            if (_feeder == null || texture == null) return;
            long tsNs = envelope != null ? envelope.CaptureMonotonicNs : 0L;
            long rotation = envelope != null ? envelope.Rotation : 0L;
            if (_textureBacked)
            {
                // GetNativeTexturePtr() -> GL texture name for the OES feeder path.
                int texId = (int)texture.GetNativeTexturePtr();
                // Identity transform; capture-only rotation is carried separately.
                float[] identity = { 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1 };
                _feeder.Call("pushTextureFrame", texId, identity,
                    texture.width, texture.height, (int)rotation, tsNs);
            }
            // I420 readback path is wired by the capture pipeline when
            // _textureBacked is false; omitted here to avoid a per-frame GPU sync
            // on the hot path (see DECISIONS §E24).
#endif
        }

        // --- Android plumbing -----------------------------------------------------

#if UNITY_ANDROID && !UNITY_EDITOR
        private void StartAndroid()
        {
            var config = _pairing.Config;
            string offerUrl = config != null ? config.WebrtcOfferUrl
                : "http://192.168.1.10:8710/webrtc/offer";

            using var context = new AndroidJavaClass("com.unity3d.player.UnityPlayer")
                .GetStatic<AndroidJavaObject>("currentActivity");

            // Build the Kotlin LiveTransportConfig (data class) via its constructor.
            var cfg = BuildConfig(offerUrl);
            _feeder = new AndroidJavaObject(
                "com.mlomega.xr.livetransport.UnityPushVideoFeeder",
                _width, _height, _fps, _textureBacked);
            _proxy = new NativeCallbackProxy(this);

            _plugin = new AndroidJavaObject(
                "com.mlomega.xr.livetransport.LiveTransportPlugin",
                context, cfg, _feeder, _proxy);
            _plugin.Call("start");
        }

        private AndroidJavaObject BuildConfig(string offerUrl)
        {
            // LiveTransportConfig has many defaulted fields; use the all-defaults
            // secondary path by constructing the nested defaults explicitly is
            // verbose, so we rely on the primary constructor with the required
            // args and Kotlin defaults filled by a small companion factory added
            // for JNI (LiveTransportConfig.forUnity). See DECISIONS §E24.
            return new AndroidJavaClass("com.mlomega.xr.livetransport.LiveTransportConfigFactory")
                .CallStatic<AndroidJavaObject>("forUnity",
                    offerUrl, _pairing.SessionId, _pairing.Token, _width, _height, _fps);
        }

        internal void EnqueueMainThread(Action action)
        {
            lock (_queueLock) { _mainThreadQueue.Enqueue(action); }
        }
#endif

        internal void OnNativeState(string stateName, string detail)
        {
            LiveTransportState mapped = stateName switch
            {
                "CONNECTING" => LiveTransportState.Connecting,
                "CONNECTED" => LiveTransportState.Connected,
                "DEGRADED" => LiveTransportState.Degraded,
                "RECONNECTING" => LiveTransportState.Reconnecting,
                _ => LiveTransportState.Disconnected
            };
            Enqueue(() => SetState(mapped, detail));
        }

        internal void OnNativeMessage(string json)
        {
            Enqueue(() =>
            {
                // Raw hook first: device_command messages (E33 §4) are claimed here
                // and must NOT be parsed as UIIntents.
                MessageReceived?.Invoke(json);
                if (json != null && json.IndexOf("\"device_command\"", StringComparison.Ordinal) >= 0)
                {
                    return;
                }
                try
                {
                    var intent = JsonConvert.DeserializeObject<UIIntent>(json);
                    if (intent != null) UiIntentReceived?.Invoke(intent);
                }
                catch (Exception ex)
                {
                    Debug.LogWarning($"[LiveTransport] bad downlink json: {ex.Message}");
                }
            });
        }

        internal void OnNativeStats(string json) => Enqueue(() => StatsReceived?.Invoke(json));

        internal void OnNativeError(string message) =>
            Debug.LogWarning($"[LiveTransport] native error: {message}");

        private void Enqueue(Action action)
        {
            lock (_queueLock) { _mainThreadQueue.Enqueue(action); }
        }

        private void SetState(LiveTransportState next, string detail)
        {
            State = next;
            StateChanged?.Invoke(next, detail);
        }

#if UNITY_ANDROID && !UNITY_EDITOR
        /// <summary>
        /// AndroidJavaProxy implementing the Kotlin LiveTransportCallbacks interface.
        /// Marshals native callbacks back into the bridge (which re-dispatches to
        /// the Unity main thread).
        /// </summary>
        private sealed class NativeCallbackProxy : AndroidJavaProxy
        {
            private readonly LiveTransportBridge _bridge;

            public NativeCallbackProxy(LiveTransportBridge bridge)
                : base("com.mlomega.xr.livetransport.LiveTransportCallbacks")
            {
                _bridge = bridge;
            }

            // enum TransportState arrives as an AndroidJavaObject; read .name().
            void onStateChanged(AndroidJavaObject state, string detail)
            {
                string name = state != null ? state.Call<string>("name") : "DISCONNECTED";
                _bridge.OnNativeState(name, detail);
            }

            void onDataChannelMessage(string json) => _bridge.OnNativeMessage(json);
            void onStats(string json) => _bridge.OnNativeStats(json);
            void onError(string message) => _bridge.OnNativeError(message);
        }
#endif
    }
}
