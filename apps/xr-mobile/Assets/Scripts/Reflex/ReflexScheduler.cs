// MLOmega V19 — E26
// ReflexScheduler: activation-by-signal, EXACTLY per GUIDE_V19_REFERENCE §9.3 —
// there are NO visible "modes". Environmental signals raised each frame map to
// the skills (and native detectors) that should be warm:
//   centre of view on text     → LensWindow (+ OCR ROI on the PC)
//   hand + near object         → HandAction/StableTrack
//   multi-language conversation→ Subtitle
//   fast motion / proximity    → MotionProximity
//   "where is …" command       → FocusSearch
// It respects a budget (§9.4 — never all detectors in parallel): at most N skills
// active at once; when a higher-priority signal needs a slot the lowest-priority
// active skill is dropped. It also drives the Kotlin detectors on demand: the
// GesturePipeline only runs while a gesture-relevant signal is up, the AsrKws
// service only while a speech-relevant signal (or the wake gate) is up — battery.
using System;
using System.Collections.Generic;
using MLOmega.XR.Reflex.Skills;
using UnityEngine;

namespace MLOmega.XR.Reflex
{
    public sealed class ReflexScheduler : MonoBehaviour
    {
        [SerializeField] private ReflexConfig _config;
        [SerializeField] private GestureBridge _gestureBridge;
        [SerializeField] private AsrBridge _asrBridge;

        [Header("Skills")]
        [SerializeField] private StableTrackSkill _stableTrack;
        [SerializeField] private LensWindowSkill _lensWindow;
        [SerializeField] private MotionProximitySkill _motionProximity;
        [SerializeField] private FocusSearchSkill _focusSearch;
        [SerializeField] private SubtitleSkill _subtitle;

        /// <summary>Signal priority for budget eviction: lower value = keep first.</summary>
        private static readonly Dictionary<ReflexSkillId, int> SkillPriority =
            new Dictionary<ReflexSkillId, int>
            {
                { ReflexSkillId.MotionProximity, 0 }, // safety-adjacent, keep first
                { ReflexSkillId.LensWindow, 1 },      // explicit focus
                { ReflexSkillId.Subtitle, 2 },
                { ReflexSkillId.FocusSearch, 3 },
                { ReflexSkillId.StableTrack, 4 }
            };

        // signal -> which skills it wants active.
        private readonly Dictionary<ReflexSignal, ReflexSkillId[]> _signalMap =
            new Dictionary<ReflexSignal, ReflexSkillId[]>
            {
                { ReflexSignal.ViewCentreOnText, new[] { ReflexSkillId.LensWindow } },
                { ReflexSignal.HandNearObject, new[] { ReflexSkillId.StableTrack, ReflexSkillId.LensWindow } },
                { ReflexSignal.MultiLanguageConversation, new[] { ReflexSkillId.Subtitle } },
                { ReflexSignal.FastMotionOrProximity, new[] { ReflexSkillId.MotionProximity } },
                { ReflexSignal.WhereIsCommand, new[] { ReflexSkillId.FocusSearch } },
                // ZoneChange is a WorldBrain/keyframe concern, not an on-device skill.
                { ReflexSignal.ZoneChange, Array.Empty<ReflexSkillId>() }
            };

        // Last time each skill was requested by a signal (for linger).
        private readonly Dictionary<ReflexSkillId, long> _lastRequestedMs =
            new Dictionary<ReflexSkillId, long>();

        private readonly HashSet<ReflexSignal> _activeSignals = new HashSet<ReflexSignal>();

        public IReadOnlyDictionary<ReflexSkillId, ReflexSkillBase> Skills => _skills;
        private readonly Dictionary<ReflexSkillId, ReflexSkillBase> _skills =
            new Dictionary<ReflexSkillId, ReflexSkillBase>();

        private void Awake()
        {
            if (_config == null) _config = ReflexConfig.CreateDefault();
            Register(_stableTrack);
            Register(_lensWindow);
            Register(_motionProximity);
            Register(_focusSearch);
            Register(_subtitle);
        }

        private void Register(ReflexSkillBase skill)
        {
            if (skill != null) _skills[skill.SkillId] = skill;
        }

        /// <summary>
        /// Raise a signal for this frame. Signals are edge-cleared each Tick, so a
        /// caller re-raises them while the condition holds; the linger keeps a skill
        /// warm briefly after its signal stops (anti-flap).
        /// </summary>
        public void RaiseSignal(ReflexSignal signal) => _activeSignals.Add(signal);

        private void Update() => Tick((long)(Time.unscaledTimeAsDouble * 1000.0));

        /// <summary>
        /// Reconcile active skills + native detectors against the raised signals,
        /// within budget. Deterministic (takes now) for EditMode tests.
        /// </summary>
        public void Tick(long nowMs)
        {
            // 1) collect desired skills from raised signals.
            var desired = new HashSet<ReflexSkillId>();
            foreach (ReflexSignal s in _activeSignals)
            {
                if (_signalMap.TryGetValue(s, out ReflexSkillId[] ids))
                {
                    foreach (ReflexSkillId id in ids)
                    {
                        if (_skills.ContainsKey(id))
                        {
                            desired.Add(id);
                            _lastRequestedMs[id] = nowMs;
                        }
                    }
                }
            }
            _activeSignals.Clear();

            // 2) keep skills whose linger has not elapsed even without a fresh signal.
            long linger = _config != null ? _config.SkillLingerMs : 1500;
            foreach (KeyValuePair<ReflexSkillId, long> kv in _lastRequestedMs)
            {
                if (nowMs - kv.Value <= linger) desired.Add(kv.Key);
            }

            // 3) enforce the budget (§9.4): keep the highest-priority desired skills.
            int budget = _config != null ? _config.MaxSimultaneousSkills : 3;
            List<ReflexSkillId> ordered = new List<ReflexSkillId>(desired);
            ordered.Sort((a, b) => Prio(a).CompareTo(Prio(b)));
            var keep = new HashSet<ReflexSkillId>();
            for (int i = 0; i < ordered.Count && keep.Count < budget; i++) keep.Add(ordered[i]);

            // 4) apply: activate kept, deactivate the rest.
            foreach (KeyValuePair<ReflexSkillId, ReflexSkillBase> kv in _skills)
            {
                bool shouldRun = keep.Contains(kv.Key);
                if (shouldRun && !kv.Value.IsActive) kv.Value.Activate();
                else if (!shouldRun && kv.Value.IsActive) kv.Value.Deactivate();
            }

            // 5) drive native detectors on demand (battery — §9.4).
            DriveDetectors(keep);
        }

        private void DriveDetectors(HashSet<ReflexSkillId> keep)
        {
            // Gestures are needed when LensWindow (pinch zoom) or StableTrack (hand) run.
            bool wantGestures = keep.Contains(ReflexSkillId.LensWindow) ||
                                keep.Contains(ReflexSkillId.StableTrack);
            if (_gestureBridge != null)
            {
                if (wantGestures && !_gestureBridge.IsRunning) _gestureBridge.Activate();
                else if (!wantGestures && _gestureBridge.IsRunning) _gestureBridge.Deactivate();
            }

            // ASR is needed when Subtitle runs. (The WakeWordGate manages the mic
            // independently for wake-word-only listening.)
            bool wantAsr = keep.Contains(ReflexSkillId.Subtitle);
            if (_asrBridge != null)
            {
                if (wantAsr && !_asrBridge.IsRunning) _asrBridge.Activate();
                else if (!wantAsr && _asrBridge.IsRunning) _asrBridge.Deactivate();
            }
        }

        private static int Prio(ReflexSkillId id) =>
            SkillPriority.TryGetValue(id, out int p) ? p : 99;

        /// <summary>Configure every registered skill with the session + reflex sink.</summary>
        public void ConfigureSkills(string sessionId, IReflexEventSink reflexSink)
        {
            foreach (ReflexSkillBase skill in _skills.Values)
            {
                skill.Configure(sessionId, reflexSink, _config);
            }
        }

        /// <summary>Test/introspection: is a given skill currently active?</summary>
        public bool IsSkillActive(ReflexSkillId id) =>
            _skills.TryGetValue(id, out ReflexSkillBase s) && s.IsActive;
    }
}
