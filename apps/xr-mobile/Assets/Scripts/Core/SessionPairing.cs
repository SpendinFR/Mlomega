// MLOmega V19 — E23
// Pairs the XR client with the PC SessionHub: creates a session, holds the
// ephemeral token, renews it before expiry, and drives periodic clock-sync. The
// PC address / device id come from an MLOmegaConfig asset. Exposes a simple
// paired/unpaired/expired state plus the live clock offset for the capture path.
using System;
using System.Collections;
using UnityEngine;
using UnityEngine.Networking;

namespace MLOmega.XR.Core
{
    public enum PairingState
    {
        Unpaired = 0,
        Pairing = 1,
        Paired = 2,
        Expired = 3,
        Error = 4
    }

    /// <summary>
    /// Owns the session_id + ephemeral token lifecycle and the <see cref="ClockSync"/>
    /// loop. Attach to the session GameObject; other components read
    /// <see cref="SessionId"/> / <see cref="Clock"/> once <see cref="State"/> is
    /// <see cref="PairingState.Paired"/>.
    /// </summary>
    public sealed class SessionPairing : MonoBehaviour
    {
        [Tooltip("PC address, adapter and cadence configuration.")]
        [SerializeField] private MLOmegaConfig _config;

        [Tooltip("Retry delay (seconds) after a failed create/renew before trying again.")]
        [Min(0.5f)]
        [SerializeField] private float _retryDelaySeconds = 3f;

        [Tooltip("Short per-endpoint /health probe timeout (seconds) during resolution.")]
        [Min(0.5f)]
        [SerializeField] private float _healthProbeTimeoutSeconds = 2f;

        public PairingState State { get; private set; } = PairingState.Unpaired;
        public string SessionId { get; private set; }
        public string Token { get; private set; }
        public string LastError { get; private set; }

        /// <summary>E36 §1 — the endpoint the client is currently reaching the PC
        /// through (LAN or a VPN tunnel), or null when the PC is unreachable.</summary>
        public PcEndpoint ActiveEndpoint { get; private set; }

        /// <summary>Base URL of the active endpoint, or null when unreachable.</summary>
        public string ActiveBaseUrl => ActiveEndpoint != null ? ActiveEndpoint.BaseUrl : null;

        /// <summary>The monotonic clock shared with capture/pose publishers.</summary>
        public IMonotonicClock MonotonicClock { get; private set; }

        /// <summary>Clock-sync client; null until pairing starts.</summary>
        public ClockSync Clock { get; private set; }

        public event Action<PairingState> StateChanged;

        private SessionHubClient _hub;
        private float _tokenIssuedAtRealtime;
        private Coroutine _lifecycle;

        public MLOmegaConfig Config => _config;

        private void Awake()
        {
            MonotonicClock = new StopwatchMonotonicClock();
        }

        private void OnEnable()
        {
            if (_config == null)
            {
                SetState(PairingState.Error);
                LastError = "MLOmegaConfig not assigned";
                Debug.LogError("[SessionPairing] No MLOmegaConfig assigned; cannot pair.");
                return;
            }
            // E36 §1: the SessionHubClient is (re)built against the resolved active
            // endpoint inside Lifecycle, after the /health probe picks LAN-or-tunnel.
            _lifecycle = StartCoroutine(Lifecycle());
        }

        private void OnDisable()
        {
            if (_lifecycle != null)
            {
                StopCoroutine(_lifecycle);
                _lifecycle = null;
            }
            SetState(PairingState.Unpaired);
        }

        private void SetState(PairingState next)
        {
            if (State == next)
            {
                return;
            }
            State = next;
            StateChanged?.Invoke(next);
        }

        /// <summary>
        /// E36 §1 — probe the ordered endpoints (LAN first, then tunnel) and set
        /// <see cref="ActiveEndpoint"/> to the first whose <c>/health</c> answers.
        /// Always starts from the top of the list so a return to the LAN reclaims the
        /// preferred endpoint. On success it (re)builds <see cref="_hub"/>/<see cref="Clock"/>
        /// against that endpoint. Sets <see cref="ActiveEndpoint"/> to null when the
        /// PC is unreachable (the device reflex layer keeps running regardless).
        /// </summary>
        private IEnumerator ResolveActiveEndpoint()
        {
            var endpoints = _config.ResolvedEndpoints;
            foreach (var ep in endpoints)
            {
                bool healthy = false;
                using (var req = UnityWebRequest.Get(ep.HealthUrl))
                {
                    req.timeout = Mathf.Max(1, Mathf.CeilToInt(_healthProbeTimeoutSeconds));
                    yield return req.SendWebRequest();
#if UNITY_2020_2_OR_NEWER
                    healthy = req.result == UnityWebRequest.Result.Success;
#else
                    healthy = !req.isNetworkError && !req.isHttpError;
#endif
                }

                if (healthy)
                {
                    bool switched = ActiveEndpoint == null || ActiveEndpoint.BaseUrl != ep.BaseUrl;
                    ActiveEndpoint = ep;
                    if (switched || _hub == null)
                    {
                        _hub = new SessionHubClient(ep.BaseUrl, _config.HttpTimeoutSeconds);
                        Clock = new ClockSync(
                            new HttpClockSyncTransport(_hub),
                            MonotonicClock,
                            _config.ClockSyncSamplesPerBurst,
                            _config.ClockSyncMaxRetries);
                        Debug.Log($"[SessionPairing] Active endpoint: '{ep.Name}' ({ep.BaseUrl}).");
                    }
                    yield break;
                }
            }

            // No endpoint answered: PC unreachable. Reflex-only device mode stays live.
            ActiveEndpoint = null;
            LastError = "PC unreachable (no endpoint /health answered)";
            Debug.LogWarning("[SessionPairing] " + LastError + " — reflex-only until an endpoint returns.");
        }

        private IEnumerator Lifecycle()
        {
            // 0. Resolve which PC endpoint to use (LAN → tunnel), then create the
            //    session (retrying on failure). Re-resolve before each create attempt
            //    so a tunnel that comes up mid-retry is picked up automatically.
            while (State != PairingState.Paired)
            {
                yield return ResolveActiveEndpoint();
                if (ActiveEndpoint == null)
                {
                    // Nothing reachable yet — wait and retry the whole resolution.
                    yield return new WaitForSeconds(_retryDelaySeconds);
                    continue;
                }
                SetState(PairingState.Pairing);
                bool done = false;
                bool ok = false;
                yield return _hub.CreateSession(_config.DeviceId,
                    creds =>
                    {
                        SessionId = creds.SessionId;
                        Token = creds.Token;
                        _tokenIssuedAtRealtime = Time.realtimeSinceStartup;
                        ok = true;
                        done = true;
                    },
                    err =>
                    {
                        LastError = err;
                        done = true;
                    });

                while (!done) yield return null;

                if (ok)
                {
                    SetState(PairingState.Paired);
                    Debug.Log($"[SessionPairing] Paired: session '{SessionId}'.");
                    break;
                }

                Debug.LogWarning($"[SessionPairing] Create failed ({LastError}); retrying.");
                yield return new WaitForSeconds(_retryDelaySeconds);
            }

            // 2. Steady state: run clock-sync bursts and renew the token on schedule.
            float nextClockSync = 0f; // sync immediately after pairing
            while (true)
            {
                // Token renewal.
                float age = Time.realtimeSinceStartup - _tokenIssuedAtRealtime;
                float renewAt = _config.AssumedTokenLifetimeSeconds - _config.TokenRenewLeadSeconds;
                if (age >= renewAt)
                {
                    yield return RenewOnce();
                }

                // Clock-sync burst.
                if (Time.realtimeSinceStartup >= nextClockSync && State == PairingState.Paired)
                {
                    yield return Clock.RunBurst(SessionId, Token);
                    nextClockSync = Time.realtimeSinceStartup + _config.ClockSyncIntervalSeconds;
                }

                yield return null;
            }
        }

        private IEnumerator RenewOnce()
        {
            bool done = false;
            bool ok = false;
            yield return _hub.RenewToken(SessionId, Token,
                creds =>
                {
                    Token = creds.Token;
                    _tokenIssuedAtRealtime = Time.realtimeSinceStartup;
                    ok = true;
                    done = true;
                },
                err =>
                {
                    LastError = err;
                    done = true;
                });
            while (!done) yield return null;

            if (ok)
            {
                Debug.Log("[SessionPairing] Token renewed.");
            }
            else
            {
                // Token likely expired on the server; drop to Expired and re-create.
                Debug.LogWarning($"[SessionPairing] Renew failed ({LastError}); re-pairing.");
                SetState(PairingState.Expired);
                SessionId = null;
                Token = null;
                if (_lifecycle != null)
                {
                    StopCoroutine(_lifecycle);
                }
                _lifecycle = StartCoroutine(Lifecycle());
            }
        }

        /// <summary>
        /// Convenience for consumers: the current session/token snapshot, or false
        /// when not paired.
        /// </summary>
        public bool TryGetActiveSession(out string sessionId, out string token)
        {
            sessionId = SessionId;
            token = Token;
            return State == PairingState.Paired && !string.IsNullOrEmpty(sessionId);
        }
    }
}
