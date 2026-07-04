// MLOmega V19 — E25 EditMode tests
// §17.2 "Vérité UI": a probable intent must carry a discreet "probable" badge; a
// remembered/last-seen intent must expose its age; an inferred intent must be
// labelled as a hypothesis and never as observation; an observed intent shows none
// of those. Exercised through the pure TruthDescriptor so no GameObjects are built.
using System.Collections.Generic;
using MLOmega.Contracts.V19;
using MLOmega.XR.UI.Components;
using NUnit.Framework;

namespace MLOmega.XR.Tests
{
    public sealed class UITruthTests
    {
        private static UIIntent Intent(string truth, Dictionary<string, object> content = null,
            Dictionary<string, object> hint = null)
        {
            return new UIIntent
            {
                UiIntentId = "t-1",
                Component = "context_card",
                TruthLevel = truth,
                Content = content,
                UiHint = hint
            };
        }

        [Test]
        public void Probable_ShowsProbableBadge_NoHypothesis()
        {
            TruthDescriptor d = TruthDescriptor.From(Intent("probable"), null);
            Assert.IsTrue(d.ShowProbableBadge);
            Assert.IsFalse(d.ShowHypothesisLabel);
            Assert.AreEqual("probable", ContextCard.TruthChipText(d));
        }

        [Test]
        public void Inferred_ShowsHypothesisLabel_NotObservation()
        {
            TruthDescriptor d = TruthDescriptor.From(Intent("inferred"), null);
            Assert.IsTrue(d.ShowHypothesisLabel);
            Assert.IsFalse(d.ShowProbableBadge);
            Assert.AreEqual("hypothesis", ContextCard.TruthChipText(d));
        }

        [Test]
        public void Observed_HasNoBadgeNoAge()
        {
            TruthDescriptor d = TruthDescriptor.From(Intent("observed"), null);
            Assert.IsFalse(d.ShowProbableBadge);
            Assert.IsFalse(d.ShowHypothesisLabel);
            Assert.IsNull(d.AgeText);
            Assert.AreEqual(string.Empty, ContextCard.TruthChipText(d));
        }

        [Test]
        public void Remembered_WithAgeMs_ShowsHumanReadableAge()
        {
            var content = new Dictionary<string, object> { { "age_ms", 240000.0 } }; // 4 min
            TruthDescriptor d = TruthDescriptor.From(Intent("remembered", content), null);
            Assert.IsNotNull(d.AgeText);
            StringAssert.Contains("last seen", d.AgeText);
            StringAssert.Contains("4m", d.AgeText);
            Assert.AreEqual(d.AgeText, ContextCard.TruthChipText(d));
        }

        [Test]
        public void Remembered_WithoutAge_StillLabelledAsLastSeen()
        {
            TruthDescriptor d = TruthDescriptor.From(Intent("remembered"), null);
            Assert.IsNotNull(d.AgeText, "remembered must always signal it is not fresh");
        }

        [Test]
        public void Age_ReadFromUiHint_WhenNotInContent()
        {
            var hint = new Dictionary<string, object> { { "last_seen_ms", 3600000.0 } }; // 1 h
            TruthDescriptor d = TruthDescriptor.From(Intent("remembered", null, hint), null);
            StringAssert.Contains("1h", d.AgeText);
        }
    }
}
