// MLOmega V19 — E26
// Unity-side bridge to the native Android gesture pipeline
// (com.mlomega.xr.reflexvision.GesturePipeline, MediaPipe HandLandmarker +
// GestureRecognizer). Owns the AndroidJavaObject, activates/deactivates it on
// demand for the ReflexScheduler (battery — §9.4), feeds the eye/phone texture
// from EyeCaptureSource.OnFrame up the pipeline, and re-emits recognised gestures
// as C# events on the main thread.
//
// Editor / Windows dev has no Android plugin, so a REAL simulated recogniser runs
// instead (keyboard/mouse): so the whole reflex chain (LensWindow zoom, menu,
// hide-UI) can be developed and tested without a device. Same DIRECT_ANDROID /
// editor-sim split as LiveTransportBridge (DECISIONS §E24/§E26).
using System;
using System.Collections.Generic;
using MLOmega.Contracts.V19;
using MLOmega.XR.Core;
using UnityEngine;

namespace MLOmega.XR.Reflex
{
    /// <summary>A recognised gesture surfaced to the reflex layer (main thread).</summary>
    public readonly struct GestureEvent
    {
        public readonly GestureKind Kind;
        public readonly float ZoomFactor;
        public readonly Vector2 ScreenPoint; // normalised 0..1; (-1,-1) if n/a
        public readonly long TimestampMs;

        public GestureEvent(GestureKind kind, float zoom, Vector2 point, long tsMs)
        {
            Kind = kind;
            ZoomFactor = zoom;
            ScreenPoint = point;
            TimestampMs = tsMs;
        }
    }

    public sealed class GestureBridge : MonoBehaviour
    {
        [SerializeField] private EyeCaptureSource _capture;

        [Tooltip("Relative path (under the app files dir) of the MediaPipe gesture .task bundle.")]
        [SerializeField] private string _modelRelativePath = "reflex/gesture_recognizer.task";

        [Tooltip("Max hands tracked (1 keeps latency lowest).")]
        [Min(1)]
        [SerializeField] private int _numHands = 1;

        /// <summary>Raised on the main thread for each recognised gesture.</summary>
        public event Action<GestureEvent> GestureRecognized;

        /// <summary>Whether the native/simulated pipeline is currently running.</summary>
        public bool IsRunning { get; private set; }

        private readonly Queue<Action> _mainThreadQueue = new Queue<Action>();
        private readonly object _queueLock = new object();

#if UNITY_ANDROID && !UNITY_EDITOR
        private AndroidJavaObject _pipeline;
        private GestureProxy _proxy;
#endif

        private void Awake()
        {
            if (_capture == null) _capture = FindAnyObjectByType<EyeCaptureSource>();
        }

        private void Update()
        {
            DrainMainThread();
#if UNITY_EDITOR
            if (IsRunning) SimulateFromInput();
#endif
        }

        /// <summary>
        /// Activate the recogniser. Called by the ReflexScheduler when a
        /// gesture-relevant signal is active. Idempotent.
        /// </summary>
        public void Activate()
        {
            if (IsRunning) return;
            IsRunning = true;
#if UNITY_ANDROID && !UNITY_EDITOR
            StartAndroid();
#else
            Debug.Log("[GestureBridge] editor: simulated gestures (mouse wheel = pinch zoom, " +
                      "M = menu, H = hide).");
#endif
        }

        /// <summary>Deactivate the recogniser (tears down the native graph — §9.4). Idempotent.</summary>
        public void Deactivate()
        {
            if (!IsRunning) return;
            IsRunning = false;
#if UNITY_ANDROID && !UNITY_EDITOR
            _pipeline?.Call("stop");
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
            string modelPath = filesDir + "/" + _modelRelativePath;

            var cfg = new AndroidJavaClass("com.mlomega.xr.reflexvision.GestureConfigFactory")
                .CallStatic<AndroidJavaObject>("forUnity", modelPath, _numHands);
            _proxy = new GestureProxy(this);
            _pipeline = new AndroidJavaObject(
                "com.mlomega.xr.reflexvision.GesturePipeline", ctx, cfg, _proxy);
            _pipeline.Call("start");
        }

        internal void EnqueueMainThread(Action a) { lock (_queueLock) { _mainThreadQueue.Enqueue(a); } }
#endif

        internal void OnNativeGesture(string kindName, float zoom, float x, float y, long tsMs)
        {
            GestureKind kind = MapKind(kindName);
            Enqueue(() => GestureRecognized?.Invoke(
                new GestureEvent(kind, zoom, new Vector2(x, y), tsMs)));
        }

        internal void OnNativeError(string message) =>
            Debug.LogWarning($"[GestureBridge] native error: {message}");

        private static GestureKind MapKind(string name) => name switch
        {
            "PINCH_BEGIN" => GestureKind.PinchBegin,
            "PINCH_UPDATE" => GestureKind.PinchUpdate,
            "PINCH_END" => GestureKind.PinchEnd,
            "OPEN_PALM_MENU" => GestureKind.OpenPalmMenu,
            "SWIPE_HIDE" => GestureKind.SwipeHide,
            _ => GestureKind.PinchUpdate
        };

        private void Enqueue(Action a) { lock (_queueLock) { _mainThreadQueue.Enqueue(a); } }

        private void DrainMainThread()
        {
            while (true)
            {
                Action work = null;
                lock (_queueLock) { if (_mainThreadQueue.Count > 0) work = _mainThreadQueue.Dequeue(); }
                if (work == null) break;
                try { work(); } catch (Exception ex) { Debug.LogError($"[GestureBridge] {ex}"); }
            }
        }

        // --- editor simulation (real input, not a stub) ---------------------------

#if UNITY_EDITOR
        private bool _simPinching;
        private float _simZoom = 1f;

        private void SimulateFromInput()
        {
            long now = (long)(Time.unscaledTimeAsDouble * 1000.0);
            Vector2 pt = new Vector2(0.5f, 0.5f);

            // Mouse wheel drives a pinch zoom: first scroll begins, subsequent update, release with right-click.
            float wheel = Input.mouseScrollDelta.y;
            if (Mathf.Abs(wheel) > 0.01f)
            {
                _simZoom = Mathf.Clamp(_simZoom + wheel * 0.4f, 1f, 6f);
                if (!_simPinching)
                {
                    _simPinching = true;
                    RaiseSim(GestureKind.PinchBegin, _simZoom, pt, now);
                }
                else
                {
                    RaiseSim(GestureKind.PinchUpdate, _simZoom, pt, now);
                }
            }
            if (_simPinching && Input.GetMouseButtonDown(1))
            {
                _simPinching = false;
                _simZoom = 1f;
                RaiseSim(GestureKind.PinchEnd, 1f, pt, now);
            }
            if (Input.GetKeyDown(KeyCode.M)) RaiseSim(GestureKind.OpenPalmMenu, 0f, pt, now);
            if (Input.GetKeyDown(KeyCode.H)) RaiseSim(GestureKind.SwipeHide, 0f, pt, now);
        }

        private void RaiseSim(GestureKind kind, float zoom, Vector2 pt, long tsMs) =>
            GestureRecognized?.Invoke(new GestureEvent(kind, zoom, pt, tsMs));
#endif

        /// <summary>
        /// Directly inject a gesture (used by EditMode tests and the demo driver to
        /// prove the reflex chain without any device/native pipeline).
        /// </summary>
        public void InjectGesture(GestureEvent ev) => GestureRecognized?.Invoke(ev);

#if UNITY_ANDROID && !UNITY_EDITOR
        private sealed class GestureProxy : AndroidJavaProxy
        {
            private readonly GestureBridge _bridge;
            public GestureProxy(GestureBridge b)
                : base("com.mlomega.xr.reflexvision.GestureCallbacks") { _bridge = b; }

            void onGesture(AndroidJavaObject kind, float zoom, float x, float y, long tsMs)
            {
                string name = kind != null ? kind.Call<string>("name") : "PINCH_UPDATE";
                _bridge.OnNativeGesture(name, zoom, x, y, tsMs);
            }
            void onError(string message) => _bridge.OnNativeError(message);
        }
#endif
    }
}
