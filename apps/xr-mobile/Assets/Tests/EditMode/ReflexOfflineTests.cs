// MLOmega V19 — E26 EditMode tests — the key "PC cut, reflexes intact" proof.
//
// Everything here runs with the transport SIMULATED DISCONNECTED (no
// LiveTransportBridge, no PC): the skills emit their UIIntents purely on-device
// through the LocalIntentSource seam. Covered:
//   * StableTrack / LensWindow / MotionProximity / Subtitle / WakeWord all emit
//     their intents with no transport (100% local path proven);
//   * scheduler signal→skill mapping (§9.3) + budget cap (§9.4);
//   * TemplateTracker follows a synthetic pattern moved across a generated texture;
//   * ReflexEvent aggregation: N detections → 1 aggregated event (§8.3).
using System.Collections.Generic;
using MLOmega.Contracts.V19;
using MLOmega.XR.Core;
using MLOmega.XR.Reflex;
using MLOmega.XR.Reflex.Skills;
using MLOmega.XR.Scene;
using MLOmega.XR.UI;
using NUnit.Framework;
using UnityEngine;

namespace MLOmega.XR.Tests
{
    public sealed class ReflexOfflineTests
    {
        // Captures every intent that reaches the broker seam.
        private sealed class CapturingIntents
        {
            public readonly List<UIIntent> Intents = new List<UIIntent>();
            public void Subscribe(LocalIntentSource src) => src.IntentProduced += Intents.Add;
            public bool Any(string component) =>
                Intents.Exists(i => i.Component == component);
            public UIIntent Last(string component) =>
                Intents.FindLast(i => i.Component == component);
        }

        private sealed class CapturingReflex : IReflexEventSink
        {
            public readonly List<ReflexEvent> Events = new List<ReflexEvent>();
            public void Send(ReflexEvent e) => Events.Add(e);
        }

        private readonly List<GameObject> _spawned = new List<GameObject>();

        [TearDown]
        public void TearDown()
        {
            foreach (GameObject go in _spawned) if (go != null) Object.DestroyImmediate(go);
            _spawned.Clear();
        }

        private T Make<T>(string name) where T : Component
        {
            var go = new GameObject(name);
            _spawned.Add(go);
            return go.AddComponent<T>();
        }

        private LocalIntentSource NewSource(CapturingIntents cap)
        {
            var src = Make<LocalIntentSource>("intents");
            cap.Subscribe(src);
            return src;
        }

        private static void Wire(ReflexSkillBase skill, LocalIntentSource src, IReflexEventSink reflex)
        {
            // Inject the shared LocalIntentSource + config via serialized field name.
            typeof(ReflexSkillBase)
                .GetField("_intentSource", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(skill, src);
            skill.Configure("sess-offline", reflex, ReflexConfig.CreateDefault());
            skill.Activate();
        }

        // ------------------------------------------------------------------
        //  100% local: each skill emits its intent with the PC cut.
        // ------------------------------------------------------------------

        [Test]
        public void StableTrack_EmitsOutline_Offline()
        {
            var cap = new CapturingIntents();
            LocalIntentSource src = NewSource(cap);
            var reflex = new CapturingReflex();
            var skill = Make<StableTrackSkill>("stable");
            Wire(skill, src, reflex);

            skill.TrackObject("t1", "phone");

            Assert.IsTrue(cap.Any("object_outline"), "StableTrack must emit an outline offline");
            Assert.AreEqual("t1", cap.Last("object_outline").TargetTrackId);
            Assert.AreEqual("ultralive", cap.Last("object_outline").Producer);
        }

        [Test]
        public void LensWindow_PinchZoom_EmitsLens_Offline()
        {
            var cap = new CapturingIntents();
            LocalIntentSource src = NewSource(cap);
            var reflex = new CapturingReflex();
            var skill = Make<LensWindowSkill>("lens");
            Wire(skill, src, reflex);

            // Pinch begin at a screen point, then update with a higher zoom.
            skill.OnGesture(new GestureEvent(GestureKind.PinchBegin, 2.0f, new Vector2(0.5f, 0.5f), 0));
            skill.OnGesture(new GestureEvent(GestureKind.PinchUpdate, 3.2f, new Vector2(0.5f, 0.5f), 30));

            Assert.IsTrue(cap.Any("lens_window"), "LensWindow must emit offline");
            UIIntent lens = cap.Last("lens_window");
            Assert.AreEqual(true, lens.UiHint["focus"], "lens is an explicit focus intent (priority rung 2)");
            Assert.AreEqual(3.2f, System.Convert.ToSingle(lens.Content["zoom"]), 1e-3f);
            Assert.IsTrue(skill.IsOpen);
        }

        [Test]
        public void MotionProximity_EmitsCue_Offline()
        {
            var cap = new CapturingIntents();
            LocalIntentSource src = NewSource(cap);
            var reflex = new CapturingReflex();
            var skill = Make<MotionProximitySkill>("motion");
            Wire(skill, src, reflex);

            int w = 32, h = 24;
            float[] a = new float[w * h];            // flat frame
            float[] b = new float[w * h];
            // A bright growing blob in one quadrant between frames.
            for (int y = 0; y < h; y++)
                for (int x = 0; x < w; x++)
                    b[y * w + x] = (x > w * 3 / 4 && y > h * 3 / 4) ? 1f : 0f;

            skill.Analyze(a, w, h, 0f, 0f, 0);           // seed prev
            float growth = skill.Analyze(b, w, h, 0f, 0f, 100); // big change, no self-motion

            Assert.Greater(growth, 0f);
            Assert.IsTrue(cap.Any("offscreen_arrow"), "MotionProximity must emit a directional cue offline");
        }

        [Test]
        public void MotionProximity_SelfMotion_IsDiscounted()
        {
            var reflex = new CapturingReflex();
            var cap = new CapturingIntents();
            LocalIntentSource src = NewSource(cap);
            var skill = Make<MotionProximitySkill>("motion2");
            Wire(skill, src, reflex);

            int w = 16, h = 16;
            float[] a = new float[w * h];
            float[] b = new float[w * h];
            for (int i = 0; i < b.Length; i++) b[i] = (i % 3 == 0) ? 1f : 0f;

            skill.Analyze(a, w, h, 0f, 0f, 0);
            float still = skill.Analyze(b, w, h, 0f, 0f, 100);
            skill.Analyze(a, w, h, 0f, 0f, 200);
            // Same visual change but attributed to a large head turn -> discounted.
            float turning = skill.Analyze(b, w, h, 1.2f, 0f, 300);

            Assert.Less(turning, still, "a head turn must discount the growth estimate");
        }

        [Test]
        public void Subtitle_PartialThenFinal_Offline()
        {
            var cap = new CapturingIntents();
            LocalIntentSource src = NewSource(cap);
            var reflex = new CapturingReflex();
            var store = Make<LocalTrackStore>("store");
            var cache = Make<SceneCache>("cache");
            typeof(LocalTrackStore)
                .GetField("_sceneCache", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(store, cache);
            var skill = Make<SubtitleSkill>("subtitle");
            typeof(SubtitleSkill).BaseType
                .GetField("_intentSource", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(skill, src);
            typeof(SubtitleSkill)
                .GetField("_trackStore", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(skill, store);
            skill.Configure("sess-offline", reflex, ReflexConfig.CreateDefault());
            skill.Activate();

            skill.OnTranscript("bonjour", isFinal: false, language: "fr");
            skill.OnTranscript("bonjour tout le monde", isFinal: true, language: "fr");

            var subs = cap.Intents.FindAll(i => i.Component == "subtitle");
            Assert.GreaterOrEqual(subs.Count, 2, "partial + final subtitle offline");
            Assert.AreEqual(false, subs[0].Content["final"]);
            Assert.AreEqual(true, subs[subs.Count - 1].Content["final"]);
            Assert.AreEqual("fr", subs[subs.Count - 1].Content["language"]);
            // A finalised segment produces exactly one aggregated reflex event.
            Assert.AreEqual(1, reflex.Events.Count);
        }

        [Test]
        public void WakeWord_ArmsListening_And_EmitsStatus_Offline()
        {
            var cap = new CapturingIntents();
            LocalIntentSource src = NewSource(cap);
            var gate = Make<WakeWordGate>("wake");
            typeof(WakeWordGate)
                .GetField("_intentSource", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(gate, src);

            bool started = false;
            gate.ListeningStarted += _ => started = true;
            gate.OnWakeWord("hey mlomega", 0);

            Assert.IsTrue(gate.Listening, "wake word must arm command listening");
            Assert.IsTrue(started);
            UIIntent status = cap.Last("status_bar");
            Assert.IsNotNull(status, "wake word shows StatusBar feedback offline");
            Assert.AreEqual(true, status.Content["listening"]);
        }

        // ------------------------------------------------------------------
        //  Scheduler: signal→skill mapping (§9.3) + budget (§9.4).
        // ------------------------------------------------------------------

        [Test]
        public void Scheduler_MapsSignalsToSkills()
        {
            var reflex = new CapturingReflex();
            var cap = new CapturingIntents();
            LocalIntentSource src = NewSource(cap);
            ReflexScheduler sched = BuildScheduler(src, reflex, budget: 5);

            sched.RaiseSignal(ReflexSignal.MultiLanguageConversation);
            sched.Tick(0);
            Assert.IsTrue(sched.IsSkillActive(ReflexSkillId.Subtitle),
                "multi-language conversation → Subtitle (§9.3)");

            sched.RaiseSignal(ReflexSignal.WhereIsCommand);
            sched.Tick(10);
            Assert.IsTrue(sched.IsSkillActive(ReflexSkillId.FocusSearch),
                "\"where is\" command → FocusSearch (§9.3)");

            sched.RaiseSignal(ReflexSignal.FastMotionOrProximity);
            sched.Tick(20);
            Assert.IsTrue(sched.IsSkillActive(ReflexSkillId.MotionProximity),
                "fast motion → MotionProximity (§9.3)");
        }

        [Test]
        public void Scheduler_RespectsBudget()
        {
            var reflex = new CapturingReflex();
            var cap = new CapturingIntents();
            LocalIntentSource src = NewSource(cap);
            ReflexScheduler sched = BuildScheduler(src, reflex, budget: 2);

            // Raise more signals than the budget allows.
            sched.RaiseSignal(ReflexSignal.FastMotionOrProximity);       // MotionProximity (prio 0)
            sched.RaiseSignal(ReflexSignal.ViewCentreOnText);            // LensWindow (prio 1)
            sched.RaiseSignal(ReflexSignal.MultiLanguageConversation);   // Subtitle (prio 2)
            sched.RaiseSignal(ReflexSignal.HandNearObject);              // StableTrack (prio 4) + LensWindow
            sched.Tick(0);

            int active = 0;
            foreach (ReflexSkillId id in new[]
            {
                ReflexSkillId.MotionProximity, ReflexSkillId.LensWindow,
                ReflexSkillId.Subtitle, ReflexSkillId.FocusSearch, ReflexSkillId.StableTrack
            })
            {
                if (sched.IsSkillActive(id)) active++;
            }
            Assert.AreEqual(2, active, "budget of 2 must cap simultaneous skills (§9.4)");
            // The highest-priority signals win.
            Assert.IsTrue(sched.IsSkillActive(ReflexSkillId.MotionProximity));
            Assert.IsTrue(sched.IsSkillActive(ReflexSkillId.LensWindow));
        }

        private ReflexScheduler BuildScheduler(LocalIntentSource src, IReflexEventSink reflex, int budget)
        {
            var config = ReflexConfig.CreateDefault();
            typeof(ReflexConfig)
                .GetField("_maxSimultaneousSkills", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(config, budget);
            typeof(ReflexConfig)
                .GetField("_skillLingerMs", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(config, 0L); // no linger, deterministic per-tick

            var sched = Make<ReflexScheduler>("sched");
            var stable = Make<StableTrackSkill>("s_stable");
            var lens = Make<LensWindowSkill>("s_lens");
            var motion = Make<MotionProximitySkill>("s_motion");
            var focus = Make<FocusSearchSkill>("s_focus");
            var subtitle = Make<SubtitleSkill>("s_sub");
            foreach (ReflexSkillBase sk in new ReflexSkillBase[] { stable, lens, motion, focus, subtitle })
            {
                typeof(ReflexSkillBase)
                    .GetField("_intentSource", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                    .SetValue(sk, src);
            }
            SetField(sched, "_config", config);
            SetField(sched, "_stableTrack", stable);
            SetField(sched, "_lensWindow", lens);
            SetField(sched, "_motionProximity", motion);
            SetField(sched, "_focusSearch", focus);
            SetField(sched, "_subtitle", subtitle);
            // Re-run Awake registration now the fields are set.
            typeof(ReflexScheduler).GetMethod("Awake", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .Invoke(sched, null);
            sched.ConfigureSkills("sess-offline", reflex);
            return sched;
        }

        private static void SetField(object target, string field, object value)
        {
            target.GetType()
                .GetField(field, System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(target, value);
        }

        // ------------------------------------------------------------------
        //  TemplateTracker: follow a synthetic pattern moved across a texture.
        // ------------------------------------------------------------------

        [Test]
        public void TemplateTracker_FollowsMovingPattern()
        {
            const int W = 96, H = 72;
            // A distinctive 9x9 cross pattern we can move deterministically.
            Vector2Int start = new Vector2Int(30, 30);
            float[] frame0 = MakeFrameWithPattern(W, H, start);

            var tracker = new TemplateTracker(templateSize: 11, searchRadius: 10, acceptScore: 0.5f);
            tracker.Acquire(frame0, W, H, new Vector2((start.x + 0.5f) / W, (start.y + 0.5f) / H));

            // Move the pattern by (+6,+4) and track it.
            Vector2Int moved = new Vector2Int(36, 34);
            float[] frame1 = MakeFrameWithPattern(W, H, moved);
            TemplateTracker.Result r = tracker.Track(frame1, W, H);

            Assert.IsTrue(r.Found, "tracker must re-find the pattern after motion");
            float expX = (moved.x + 0.5f) / W;
            float expY = (moved.y + 0.5f) / H;
            Assert.AreEqual(expX, r.Center.x, 2f / W, "x within ~2px");
            Assert.AreEqual(expY, r.Center.y, 2f / H, "y within ~2px");
            Assert.Greater(r.Score, 0.8f, "strong correlation on an exact pattern");
        }

        private static float[] MakeFrameWithPattern(int w, int h, Vector2Int center)
        {
            // Deterministic textured background + a bright cross at 'center'.
            var f = new float[w * h];
            for (int y = 0; y < h; y++)
                for (int x = 0; x < w; x++)
                    f[y * w + x] = 0.2f + 0.1f * Mathf.Sin(x * 0.3f) * Mathf.Cos(y * 0.2f);
            for (int d = -4; d <= 4; d++)
            {
                Plot(f, w, h, center.x + d, center.y, 1f);
                Plot(f, w, h, center.x, center.y + d, 1f);
            }
            return f;
        }

        private static void Plot(float[] f, int w, int h, int x, int y, float v)
        {
            if (x < 0 || y < 0 || x >= w || y >= h) return;
            f[y * w + x] = v;
        }

        // ------------------------------------------------------------------
        //  ReflexEvent aggregation: N detections → 1 aggregated event (§8.3).
        // ------------------------------------------------------------------

        [Test]
        public void ReflexAggregator_CollapsesManyDetectionsIntoOne()
        {
            var reflex = new CapturingReflex();
            var agg = new ReflexEventAggregator(reflex, windowMs: 1000);

            // 20 detections inside the window -> nothing flushed yet.
            for (int i = 0; i < 20; i++)
            {
                agg.Observe("sess", "f_" + i, "motion_proximity", "motion",
                    new Dictionary<string, object> { { "growth", 0.2 } },
                    400, 0.5, "info", null, i * 10);
            }
            Assert.AreEqual(0, reflex.Events.Count, "no event before the window elapses");

            // One more past the window flushes a single aggregated event with the count.
            ReflexEvent flushed = agg.Observe("sess", "f_last", "motion_proximity", "motion",
                new Dictionary<string, object> { { "growth", 0.2 } },
                400, 0.5, "info", null, 1100);

            Assert.AreEqual(1, reflex.Events.Count, "N detections collapse into 1 aggregated event");
            Assert.IsNotNull(flushed);
            Assert.AreEqual("motion", flushed.AggregateKey);
            Assert.AreEqual(21, System.Convert.ToInt32(flushed.Prediction["count"]));
        }

        [Test]
        public void ReflexAggregator_CriticalEscalatesImmediately()
        {
            var reflex = new CapturingReflex();
            var agg = new ReflexEventAggregator(reflex, windowMs: 5000);

            agg.Observe("sess", "f0", "motion_proximity", "motion",
                new Dictionary<string, object>(), 400, 0.9, "critical", null, 0);

            Assert.AreEqual(1, reflex.Events.Count, "a critical severity flushes immediately, not after the window");
            Assert.AreEqual("critical", reflex.Events[0].Severity);
        }
    }
}
