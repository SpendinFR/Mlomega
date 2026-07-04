// MLOmega V19 — E25 EditMode tests
// §13.3 receipt semantics driven deterministically through UIComponentBase.Tick:
//   * "displayed" is emitted once, as soon as the panel becomes visible (fade-in);
//   * "seen" is emitted only after the prudent dwell timer, never on load;
//   * "dismissed" is emitted on an explicit user dismissal command;
//   * "acted"/"corrected" are emitted on the explicit hooks.
// A tiny concrete stub avoids building the world-space glass panels, so the test
// focuses purely on the base lifecycle + receipt timeline.
using System.Collections.Generic;
using MLOmega.Contracts.V19;
using MLOmega.XR.UI;
using MLOmega.XR.UI.Components;
using NUnit.Framework;
using UnityEngine;

namespace MLOmega.XR.Tests
{
    public sealed class UIReceiptLifecycleTests
    {
        // Concrete component with no visuals; only the base lifecycle/receipts run.
        private sealed class StubComponent : UIComponentBase
        {
            public override string ComponentKey => "stub";
            protected override void Bind(UIIntent intent) { }
            protected override void ApplyVisual() { } // skip transform scaling for headless
        }

        private sealed class CapturingSink : IReceiptSink
        {
            public readonly List<UIReceipt> Receipts = new List<UIReceipt>();
            public void Send(UIReceipt receipt) => Receipts.Add(receipt);
            public List<string> Events()
            {
                var e = new List<string>();
                foreach (UIReceipt r in Receipts) e.Add(r.Event);
                return e;
            }
        }

        private static UIIntent NewIntent() => new UIIntent
        {
            ContractsVersion = "v19.0",
            UiIntentId = "intent-1",
            DeliveryId = "del-1",
            Component = "stub",
            TruthLevel = "observed"
        };

        private GameObject _go;

        [TearDown]
        public void TearDown()
        {
            if (_go != null) Object.DestroyImmediate(_go);
        }

        private StubComponent MakeComponent(CapturingSink sink)
        {
            _go = new GameObject("stub");
            var comp = _go.AddComponent<StubComponent>();
            comp.Configure(new UIComponentContext(null, null, null), null);
            comp.Admit(NewIntent(), sink, _ => { });
            return comp;
        }

        // Drive the component forward by 'seconds' in small steps.
        private static void Advance(UIComponentBase comp, float from, float seconds, float step = 0.05f)
        {
            float t = from;
            for (float elapsed = 0f; elapsed < seconds; elapsed += step)
            {
                t += step;
                comp.Tick(t, step);
            }
        }

        [Test]
        public void Displayed_EmittedOnceOnFirstVisibleFrame()
        {
            var sink = new CapturingSink();
            StubComponent comp = MakeComponent(sink);

            // One small tick is enough to cross the visible-alpha threshold.
            comp.Tick(0.02f, 0.02f);

            CollectionAssert.Contains(sink.Events(), "displayed");
            Assert.AreEqual(1, sink.Receipts.FindAll(r => r.Event == "displayed").Count);
            Assert.AreEqual("intent-1", sink.Receipts[0].UiIntentId);
            Assert.AreEqual("del-1", sink.Receipts[0].DeliveryId);
        }

        [Test]
        public void Seen_NotEmittedBeforeDwell_ThenEmittedAfter()
        {
            var sink = new CapturingSink();
            StubComponent comp = MakeComponent(sink);

            // Reach full visibility quickly (fade-in <=0.2s), well before dwell.
            Advance(comp, 0f, 0.3f);
            Assert.IsFalse(sink.Events().Contains("seen"),
                "seen must not fire before the prudent dwell (default 1.2s)");

            // Advance past the dwell timer.
            Advance(comp, 0.3f, 1.5f);
            CollectionAssert.Contains(sink.Events(), "seen");
            Assert.AreEqual(1, sink.Receipts.FindAll(r => r.Event == "seen").Count,
                "seen must be emitted exactly once");
        }

        [Test]
        public void Dismissed_EmittedOnUserDismissal()
        {
            var sink = new CapturingSink();
            StubComponent comp = MakeComponent(sink);
            comp.Tick(0.02f, 0.02f); // displayed

            comp.BeginFadeOut(userDismissed: true);
            CollectionAssert.Contains(sink.Events(), "dismissed");
        }

        [Test]
        public void SilentFadeOut_DoesNotEmitDismissed()
        {
            var sink = new CapturingSink();
            StubComponent comp = MakeComponent(sink);
            comp.Tick(0.02f, 0.02f); // displayed

            comp.BeginFadeOut(userDismissed: false); // TTL/track-loss/eviction
            CollectionAssert.DoesNotContain(sink.Events(), "dismissed");
        }

        [Test]
        public void Acted_And_Corrected_EmittedOnHooks()
        {
            var sink = new CapturingSink();
            StubComponent comp = MakeComponent(sink);

            comp.RaiseActed(new Dictionary<string, object> { { "kind", "confirm" } });
            comp.RaiseCorrected(new Dictionary<string, object> { { "kind", "correction" } });

            CollectionAssert.Contains(sink.Events(), "acted");
            CollectionAssert.Contains(sink.Events(), "corrected");
        }
    }
}
