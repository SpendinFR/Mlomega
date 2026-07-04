// MLOmega V19 — E25 EditMode tests
// The §13.1 design-system mapping: every contract `component` string must resolve
// to the correct concrete component type, and unknown components must resolve to
// null (so the runtime logs and skips rather than mis-rendering). Pure static
// registry, no GameObjects required.
using MLOmega.XR.UI;
using MLOmega.XR.UI.Components;
using NUnit.Framework;

namespace MLOmega.XR.Tests
{
    public sealed class UIComponentRegistryTests
    {
        [Test]
        public void AllTenComponents_MapToTheirType()
        {
            Assert.AreEqual(typeof(ObjectOutline), UIComponentRegistry.ResolveType("object_outline"));
            Assert.AreEqual(typeof(PersonTag), UIComponentRegistry.ResolveType("person_tag"));
            Assert.AreEqual(typeof(Subtitle), UIComponentRegistry.ResolveType("subtitle"));
            Assert.AreEqual(typeof(LensWindow), UIComponentRegistry.ResolveType("lens_window"));
            Assert.AreEqual(typeof(OffscreenArrow), UIComponentRegistry.ResolveType("offscreen_arrow"));
            Assert.AreEqual(typeof(ContextCard), UIComponentRegistry.ResolveType("context_card"));
            Assert.AreEqual(typeof(TaskCard), UIComponentRegistry.ResolveType("task_card"));
            Assert.AreEqual(typeof(VirtualScreen), UIComponentRegistry.ResolveType("virtual_screen"));
            Assert.AreEqual(typeof(CorrectionChip), UIComponentRegistry.ResolveType("correction_chip"));
        }

        [Test]
        public void Mapping_IsCaseAndSeparatorInsensitive()
        {
            Assert.AreEqual(typeof(ObjectOutline), UIComponentRegistry.ResolveType("ObjectOutline"));
            Assert.AreEqual(typeof(ObjectOutline), UIComponentRegistry.ResolveType("object outline"));
            Assert.AreEqual(typeof(TaskCard), UIComponentRegistry.ResolveType("TaskCard"));
            Assert.AreEqual(typeof(Subtitle), UIComponentRegistry.ResolveType("SUBTITLE"));
        }

        [Test]
        public void KnownAliases_MapToCanonical()
        {
            // A live translation renders on the subtitle surface (§14.4).
            Assert.AreEqual(typeof(Subtitle), UIComponentRegistry.ResolveType("translation"));
            Assert.AreEqual(typeof(LensWindow), UIComponentRegistry.ResolveType("lens"));
            Assert.AreEqual(typeof(OffscreenArrow), UIComponentRegistry.ResolveType("arrow"));
        }

        [Test]
        public void UnknownComponent_ResolvesToNull()
        {
            Assert.IsNull(UIComponentRegistry.KeyFor("teleporter"));
            Assert.IsNull(UIComponentRegistry.ResolveType("teleporter"));
            Assert.IsNull(UIComponentRegistry.ResolveType(""));
            Assert.IsNull(UIComponentRegistry.ResolveType(null));
        }

        [Test]
        public void StatusBar_IsNotAnAdmittedComponent()
        {
            // StatusBar is a permanent standalone surface, never intent-admitted,
            // so it must not be in the runtime dispatch registry (§13.1 source = S25).
            Assert.IsNull(UIComponentRegistry.ResolveType("status_bar"));
        }
    }
}
