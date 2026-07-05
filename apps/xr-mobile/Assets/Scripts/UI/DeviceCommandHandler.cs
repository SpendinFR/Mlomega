// MLOmega V19 — E33
// DeviceCommandHandler: executes `device_command` messages the PC IntentRouter
// pushes over the same reliable DataChannel as UIIntents (§4). One execution path
// for BOTH voice (PC router) and the on-glasses menu (MenuPanel emits the same
// command locally) — nothing here is voice- or menu-specific.
//
// Actions:
//   * set_ui_mode {hide_all|minimal|normal|freeguy} -> UIIntentBroker.SetDensity
//     (hide_all leaves only the standalone StatusBar + privacy, §13.2-1);
//   * privacy_pause                                  -> StatusBar.PrivacyPaused toggle;
//   * open_app {maps|youtube|package,...}            -> Kotlin AppLauncher bridge;
//   * open_menu                                      -> raises MenuRequested (MenuPanel);
//   * replay {time}                                  -> raises ReplayRequested.
//
// Each executed command raises CommandExecuted so the app can send a UIReceipt
// (delivered) back to the PC. Lives in the UI assembly (which references Transport,
// Scene and Contracts) — a Transport->UI dependency would be a cycle.
using System;
using MLOmega.Contracts.V19;
using MLOmega.XR.Transport;
using MLOmega.XR.UI.Components;
using Newtonsoft.Json;
using UnityEngine;

namespace MLOmega.XR.UI
{
    /// <summary>A parsed device_command message (contract-lite, PC->device §4).</summary>
    public sealed class DeviceCommand
    {
        [JsonProperty("type")] public string Type { get; set; }
        [JsonProperty("action")] public string Action { get; set; }
        [JsonProperty("ui_mode")] public string UiMode { get; set; }
        [JsonProperty("app")] public string App { get; set; }
        [JsonProperty("destination")] public string Destination { get; set; }
        [JsonProperty("query")] public string Query { get; set; }
        [JsonProperty("package")] public string Package { get; set; }
        [JsonProperty("time")] public string Time { get; set; }

        public static bool IsDeviceCommand(string json)
        {
            if (string.IsNullOrEmpty(json)) return false;
            return json.IndexOf("\"device_command\"", StringComparison.Ordinal) >= 0;
        }
    }

    public sealed class DeviceCommandHandler : MonoBehaviour
    {
        [SerializeField] private UIIntentBroker _broker;
        [SerializeField] private StatusBar _statusBar;
        [SerializeField] private AppLauncherBridge _appLauncher;
        [SerializeField] private LiveTransportBridge _transport;

        /// <summary>Raised when a "menu" command arrives (MenuPanel opens the panel).</summary>
        public event Action MenuRequested;

        /// <summary>Raised when a "replay {time}" command arrives (VirtualScreen replay).</summary>
        public event Action<string> ReplayRequested;

        /// <summary>Raised for every executed command with its (action, ok) result.</summary>
        public event Action<string, bool> CommandExecuted;

        private void Awake()
        {
            if (_broker == null) _broker = FindAnyObjectByType<UIIntentBroker>();
            if (_statusBar == null) _statusBar = FindAnyObjectByType<StatusBar>();
            if (_appLauncher == null) _appLauncher = FindAnyObjectByType<AppLauncherBridge>();
            if (_transport == null) _transport = FindAnyObjectByType<LiveTransportBridge>();
        }

        private void OnEnable()
        {
            if (_transport != null) _transport.MessageReceived += OnTransportMessage;
        }

        private void OnDisable()
        {
            if (_transport != null) _transport.MessageReceived -= OnTransportMessage;
        }

        private void OnTransportMessage(string json) => TryHandleRaw(json);

        /// <summary>Parse and execute a raw DataChannel message if it is a device_command.
        /// Returns true when it was a device command (handled), false otherwise (so the
        /// caller can route it as a normal UIIntent). Never throws.</summary>
        public bool TryHandleRaw(string json)
        {
            if (!DeviceCommand.IsDeviceCommand(json)) return false;
            DeviceCommand cmd;
            try { cmd = JsonConvert.DeserializeObject<DeviceCommand>(json); }
            catch (Exception ex) { Debug.LogWarning($"[DeviceCommand] bad json: {ex.Message}"); return true; }
            if (cmd != null) Execute(cmd);
            return true;
        }

        /// <summary>Execute a parsed device command. Idempotent and null-safe.</summary>
        public bool Execute(DeviceCommand cmd)
        {
            if (cmd == null) return false;
            bool ok;
            switch ((cmd.Action ?? string.Empty).ToLowerInvariant())
            {
                case "set_ui_mode":
                    ok = SetUiMode(cmd.UiMode);
                    break;
                case "privacy_pause":
                    ok = PrivacyPause();
                    break;
                case "open_app":
                    ok = OpenApp(cmd);
                    break;
                case "open_menu":
                    MenuRequested?.Invoke();
                    ok = true;
                    break;
                case "replay":
                    ReplayRequested?.Invoke(cmd.Time);
                    ok = true;
                    break;
                default:
                    Debug.LogWarning($"[DeviceCommand] unknown action: {cmd.Action}");
                    ok = false;
                    break;
            }
            CommandExecuted?.Invoke(cmd.Action, ok);
            return ok;
        }

        private bool SetUiMode(string uiMode)
        {
            UIDensityMode mode = UIIntentBroker.ParseDensity(uiMode);
            if (_broker != null) _broker.SetDensity(mode);
            if (_statusBar != null)
            {
                _statusBar.UiMode = mode == UIDensityMode.Normal ? "live" : uiMode;
            }
            return true;
        }

        private bool PrivacyPause()
        {
            if (_statusBar == null) return false;
            _statusBar.PrivacyPaused = !_statusBar.PrivacyPaused;
            // Privacy pause cuts the sensors (the actual capture toggle is owned by
            // the capture source, which reads the StatusBar flag).
            _statusBar.CameraOn = !_statusBar.PrivacyPaused;
            _statusBar.MicOn = !_statusBar.PrivacyPaused;
            return true;
        }

        private bool OpenApp(DeviceCommand cmd)
        {
            if (_appLauncher == null)
            {
                Debug.Log($"[DeviceCommand] (no launcher) open_app {cmd.App} {cmd.Destination}{cmd.Query}{cmd.Package}");
                return false;
            }
            switch ((cmd.App ?? string.Empty).ToLowerInvariant())
            {
                case "maps": return _appLauncher.OpenMaps(cmd.Destination);
                case "youtube": return _appLauncher.OpenYouTube(cmd.Query);
                default: return _appLauncher.OpenPackage(cmd.Package);
            }
        }
    }
}
