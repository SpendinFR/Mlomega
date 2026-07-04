// MLOmega V19 — E26
// LocalTrackStore: the source of the very-short LocalTracks that anchor Ultra-Live
// UI to the present. Two feeds (handoff §3.2 / §8.4):
//   (a) PC SceneDeltas when connected — entity bboxes carried on the DataChannel
//       become/refresh local tracks, so a PC detection anchors immediately;
//   (b) pure local anchoring when the PC is cut — a TemplateTracker follows a
//       seeded patch on the sub-sampled camera texture with no network at all,
//       which is what keeps outlines/zoom alive offline.
// All produced tracks are pushed into SceneCache.tracks (the very-short sub-cache),
// so age/visibility follow the §9.1 track TTL rules automatically.
using System;
using System.Collections.Generic;
using MLOmega.Contracts.V19;
using MLOmega.XR.Core;
using UnityEngine;

namespace MLOmega.XR.Scene
{
    /// <summary>
    /// Maintains local tracks and mirrors them into the SceneCache. Attach next to
    /// a SceneCache; feed it SceneDeltas (from transport) and/or camera frames (for
    /// the offline TemplateTracker path). Deterministic Tick() so EditMode tests
    /// drive it without a running player loop.
    /// </summary>
    public sealed class LocalTrackStore : MonoBehaviour
    {
        [SerializeField] private SceneCache _sceneCache;

        [Tooltip("Sub-sampled greyscale frame width used by the TemplateTracker (px).")]
        [Min(16)]
        [SerializeField] private int _trackFrameWidth = 128;
        [Tooltip("Sub-sampled greyscale frame height used by the TemplateTracker (px).")]
        [Min(16)]
        [SerializeField] private int _trackFrameHeight = 96;

        private readonly Dictionary<string, LocalAnchor> _anchors = new Dictionary<string, LocalAnchor>();
        private string _sessionId = "local";

        public SceneCache SceneCache => _sceneCache;
        public int LocalAnchorCount => _anchors.Count;

        private void Awake()
        {
            if (_sceneCache == null) _sceneCache = FindAnyObjectByType<SceneCache>();
        }

        /// <summary>Set the session id stamped on produced LocalTracks.</summary>
        public void SetSession(string sessionId)
        {
            if (!string.IsNullOrEmpty(sessionId)) _sessionId = sessionId;
        }

        // ------------------------------------------------------------------
        //  (a) PC-connected feed: SceneDelta entities -> local tracks
        // ------------------------------------------------------------------

        /// <summary>
        /// Ingest a SceneDelta from the PC (transport connected). Each entity that
        /// carries a bbox + track_id becomes/refreshes a LocalTrack in the cache.
        /// The SceneCache itself still reconciles entities_hot separately; this only
        /// mirrors the trackable geometry into the very-short tracks sub-cache.
        /// </summary>
        public void SubmitSceneDelta(SceneDelta delta)
        {
            if (delta == null || delta.Entities == null || _sceneCache == null) return;
            long nowMs = _sceneCache.NowMs;
            foreach (Dictionary<string, object> e in delta.Entities)
            {
                string trackId = Str(e, "track_id") ?? Str(e, "last_track");
                if (string.IsNullOrEmpty(trackId)) continue;
                Dictionary<string, object> bbox = ExtractBbox(e);
                if (bbox == null) continue;
                var track = BuildTrack(trackId, delta.SourceFrameId, bbox,
                    Num(e, "confidence", 0.6), nowMs);
                _sceneCache.SubmitLocalTrack(track);
            }
        }

        // ------------------------------------------------------------------
        //  (b) Offline feed: pure local anchoring via TemplateTracker
        // ------------------------------------------------------------------

        /// <summary>
        /// Begin locally anchoring a patch centred on a normalised point (e.g. the
        /// centre of view for a lens, or a hand-near object). Seeds a TemplateTracker
        /// from the current greyscale frame; subsequent <see cref="TrackLocalFrame"/>
        /// calls follow it with no PC involvement.
        /// </summary>
        public string BeginLocalAnchor(float[] greyFrame, Vector2 centerNorm, string kind = "object")
        {
            string trackId = "lt_" + Guid.NewGuid().ToString("N").Substring(0, 8);
            var tracker = new TemplateTracker();
            tracker.Acquire(greyFrame, _trackFrameWidth, _trackFrameHeight, centerNorm);
            _anchors[trackId] = new LocalAnchor(tracker, kind, centerNorm);
            PushTrack(trackId, centerNorm, 1f, kind);
            return trackId;
        }

        /// <summary>
        /// Advance every local anchor against a fresh greyscale frame. Lost anchors
        /// are dropped; surviving ones refresh their LocalTrack in the cache.
        /// Returns the number of anchors still alive.
        /// </summary>
        public int TrackLocalFrame(float[] greyFrame)
        {
            if (_anchors.Count == 0) return 0;
            List<string> lost = null;
            foreach (KeyValuePair<string, LocalAnchor> kv in _anchors)
            {
                TemplateTracker.Result r = kv.Value.Tracker.Track(greyFrame, _trackFrameWidth, _trackFrameHeight);
                if (r.Found)
                {
                    kv.Value.LastCenter = r.Center;
                    kv.Value.LastScore = r.Score;
                    PushTrack(kv.Key, r.Center, Mathf.Clamp01(r.Score), kv.Value.Kind);
                }
                else
                {
                    (lost ??= new List<string>()).Add(kv.Key);
                }
            }
            if (lost != null) foreach (string id in lost) _anchors.Remove(id);
            return _anchors.Count;
        }

        /// <summary>Stop anchoring a local patch (e.g. lens closed).</summary>
        public void EndLocalAnchor(string trackId)
        {
            if (!string.IsNullOrEmpty(trackId)) _anchors.Remove(trackId);
        }

        public bool TryGetLocalCenter(string trackId, out Vector2 center)
        {
            if (_anchors.TryGetValue(trackId, out LocalAnchor a))
            {
                center = a.LastCenter;
                return true;
            }
            center = new Vector2(0.5f, 0.5f);
            return false;
        }

        // ------------------------------------------------------------------

        private void PushTrack(string trackId, Vector2 center, float confidence, string kind)
        {
            if (_sceneCache == null) return;
            long nowMs = _sceneCache.NowMs;
            var bbox = new Dictionary<string, object>
            {
                { "x", center.x - 0.06 }, { "y", center.y - 0.06 },
                { "w", 0.12 }, { "h", 0.12 }
            };
            _sceneCache.SubmitLocalTrack(BuildTrack(trackId, null, bbox, confidence, nowMs, kind));
        }

        private LocalTrack BuildTrack(string trackId, string sourceFrameId,
            Dictionary<string, object> bbox, double confidence, long nowMs, string kind = "object")
        {
            return new LocalTrack
            {
                ContractsVersion = ContractDefaults.Version,
                SessionId = _sessionId,
                TrackId = trackId,
                SourceFrameId = sourceFrameId,
                Kind = kind,
                BboxOrMask = bbox,
                VelocityScreen = new List<double> { 0, 0 },
                Visibility = 1.0,
                Confidence = confidence,
                ObservedAtMonotonicNs = nowMs * 1_000_000L
            };
        }

        private static Dictionary<string, object> ExtractBbox(Dictionary<string, object> e)
        {
            if (e.TryGetValue("bbox", out object v) && v is Dictionary<string, object> b) return b;
            if (e.ContainsKey("x") && e.ContainsKey("y")) return e;
            return null;
        }

        private static string Str(Dictionary<string, object> d, string key)
        {
            if (d != null && d.TryGetValue(key, out object v) && v != null) return v as string ?? v.ToString();
            return null;
        }

        private static double Num(Dictionary<string, object> d, string key, double fallback)
        {
            if (d != null && d.TryGetValue(key, out object v) && v != null &&
                double.TryParse(v.ToString(), System.Globalization.NumberStyles.Float,
                    System.Globalization.CultureInfo.InvariantCulture, out double r)) return r;
            return fallback;
        }

        private sealed class LocalAnchor
        {
            public readonly TemplateTracker Tracker;
            public readonly string Kind;
            public Vector2 LastCenter;
            public float LastScore;

            public LocalAnchor(TemplateTracker tracker, string kind, Vector2 center)
            {
                Tracker = tracker;
                Kind = kind;
                LastCenter = center;
                LastScore = 1f;
            }
        }
    }
}
