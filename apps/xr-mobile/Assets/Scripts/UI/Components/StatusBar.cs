// MLOmega V19 — E25
// StatusBar (§13.1, §15.2 step 6): the permanent head-locked, discreet bar showing
// camera, mic, network/PC (transport), privacy and UI mode. Unlike the other ten
// components it is not admitted per-intent — the spec makes it a permanent surface
// (priority rung 1, never counted or capped by the broker), so it is a standalone
// MonoBehaviour that head-locks to the camera and reads live state directly.
//
// It refactors/extends the existing G1StatusOverlay WITHOUT breaking it: the G1
// gate keeps its verbose diagnostic panel; this StatusBar is the shipping,
// glass-styled, glanceable version driven by the live transport + session +
// privacy state. Both can coexist (G1 for the gate, StatusBar for normal runtime).
using System.Text;
using MLOmega.XR.Core;
using MLOmega.XR.Transport;
using TMPro;
using UnityEngine;

namespace MLOmega.XR.UI.Components
{
    public sealed class StatusBar : MonoBehaviour
    {
        [SerializeField] private UITheme _theme;
        [SerializeField] private Material _glassMaterial;
        [SerializeField] private Camera _camera;
        [SerializeField] private LiveTransportBridge _transport;
        [SerializeField] private XrSessionController _session;

        [Header("Head-locked placement")]
        [Tooltip("Offset from the camera, in camera space (top-centre of the FOV).")]
        [SerializeField] private Vector3 _headOffset = new Vector3(0f, 0.24f, 1.0f);
        [SerializeField] private Vector2 _size = new Vector2(0.7f, 0.07f);
        [SerializeField] private float _refreshInterval = 0.25f;

        [Header("Privacy / mode (set by the app)")]
        [SerializeField] private bool _cameraOn = true;
        [SerializeField] private bool _micOn = true;
        [SerializeField] private bool _privacyPaused;
        [SerializeField] private string _uiMode = "live";

        [Tooltip("Capture-only: glasses hung vertically, frames rotated. Driven by OrientationGuard (E29 §3b).")]
        [SerializeField] private bool _captureOnly;

        private GlassPanel _panel;
        private float _nextRefresh;
        private readonly StringBuilder _sb = new StringBuilder(128);

        /// <summary>App-facing setters for the privacy/mode state (voice/gesture toggles).</summary>
        public bool CameraOn { get => _cameraOn; set => _cameraOn = value; }
        public bool MicOn { get => _micOn; set => _micOn = value; }
        public bool PrivacyPaused { get => _privacyPaused; set => _privacyPaused = value; }
        public string UiMode { get => _uiMode; set => _uiMode = value; }
        /// <summary>Capture-only badge (glasses vertical, frames rotated). Set by OrientationGuard.</summary>
        public bool CaptureOnly { get => _captureOnly; set => _captureOnly = value; }

        private void Awake()
        {
            if (_camera == null) _camera = Camera.main;
            if (_transport == null) _transport = FindAnyObjectByType<LiveTransportBridge>();
            if (_session == null) _session = FindAnyObjectByType<XrSessionController>();
        }

        private void Start()
        {
            _panel = new GlassPanel(transform, _size, _theme, _glassMaterial,
                withTitle: false, withBody: true, withTruthChip: false);
            if (_panel.Body != null)
            {
                _panel.Body.alignment = TextAlignmentOptions.Midline;
                _panel.Body.fontSize = 0.036f;
                _panel.Body.textWrappingMode = TextWrappingModes.NoWrap;
            }
            _panel.SetAlpha(1f);
        }

        private void LateUpdate()
        {
            HeadLock();
            if (Time.unscaledTime >= _nextRefresh)
            {
                _nextRefresh = Time.unscaledTime + _refreshInterval;
                Refresh();
            }
        }

        private void HeadLock()
        {
            if (_camera == null) return;
            transform.SetPositionAndRotation(
                _camera.transform.TransformPoint(_headOffset),
                Quaternion.LookRotation(transform.position - _camera.transform.position, Vector3.up));
        }

        private void Refresh()
        {
            if (_panel == null || _panel.Body == null) return;
            _sb.Clear();

            // Privacy pause overrides: make it unmistakable the sensors are off.
            if (_privacyPaused)
            {
                _panel.Body.text = "<color=#FFD24A>⏸ PRIVACY PAUSED — camera & mic off</color>";
                return;
            }

            _sb.Append(Glyph("cam", _cameraOn)).Append("  ")
               .Append(Glyph("mic", _micOn)).Append("  ")
               .Append(NetGlyph()).Append("  ")
               .Append(PcGlyph()).Append("  ")
               .Append("mode:").Append(_uiMode).Append("  ");
            if (_captureOnly)
            {
                _sb.Append("<color=#FFD24A>capture-only</color>  ");
            }
            _sb.Append(Battery());

            _panel.Body.text = _sb.ToString();
        }

        private string NetGlyph()
        {
            LiveTransportState s = _transport != null ? _transport.State : LiveTransportState.Disconnected;
            switch (s)
            {
                case LiveTransportState.Connected: return "<color=#7CE0A0>net:ok</color>";
                case LiveTransportState.Degraded: return "<color=#FFD24A>net:degraded</color>";
                case LiveTransportState.Reconnecting: return "<color=#FFD24A>net:reconnect</color>";
                case LiveTransportState.Connecting: return "<color=#B0C4DE>net:connecting</color>";
                default: return "<color=#FF8A8A>net:off</color>";
            }
        }

        private string PcGlyph()
        {
            // PC presence is inferred from the transport being connected (the PC is
            // the WebRTC peer); local-only shows pc:local so the user knows there is
            // no PC round-trip (Ultra-Live still works — §15.2 step 5).
            bool pcUp = _transport != null && _transport.State == LiveTransportState.Connected;
            return pcUp ? "<color=#7CE0A0>pc:ok</color>" : "<color=#B0C4DE>pc:local</color>";
        }

        private string Battery()
        {
            float b = SystemInfo.batteryLevel;
            return b < 0f ? "bat:n/a" : $"bat:{Mathf.RoundToInt(b * 100f)}%";
        }

        private static string Glyph(string label, bool on) =>
            on ? $"<color=#7CE0A0>{label}:on</color>" : $"<color=#FF8A8A>{label}:off</color>";
    }
}
