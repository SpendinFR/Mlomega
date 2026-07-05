// MLOmega V19 — E25
// The §13.1 design-system mapping: contract `component` string -> concrete
// UIComponentBase type. Kept as a static, side-effect-free registry so the
// UIRuntime's dispatch is auditable and the mapping is unit-testable without a
// running player (the EditMode tests assert all ten map correctly).
//
// Component names are normalised (lower, non-alnum stripped) so both snake_case
// contract values ("object_outline") and camel/space variants map to the same key.
using System.Collections.Generic;
using System.Text;
using MLOmega.XR.UI.Components;

namespace MLOmega.XR.UI
{
    public static class UIComponentRegistry
    {
        // Canonical key -> component type.
        private static readonly Dictionary<string, System.Type> ByKey =
            new Dictionary<string, System.Type>
            {
                { "objectoutline", typeof(ObjectOutline) },
                { "persontag", typeof(PersonTag) },
                { "subtitle", typeof(Subtitle) },
                { "lenswindow", typeof(LensWindow) },
                { "offscreenarrow", typeof(OffscreenArrow) },
                { "contextcard", typeof(ContextCard) },
                { "taskcard", typeof(TaskCard) },
                { "virtualscreen", typeof(VirtualScreen) },
                { "correctionchip", typeof(CorrectionChip) },
                { "menupanel", typeof(MenuPanel) },
            };

        // A few common aliases -> canonical key (translation is a subtitle surface).
        private static readonly Dictionary<string, string> Aliases =
            new Dictionary<string, string>
            {
                { "translation", "subtitle" },
                { "lens", "lenswindow" },
                { "arrow", "offscreenarrow" },
                { "outline", "objectoutline" },
                { "person", "persontag" },
                { "context", "contextcard" },
                { "task", "taskcard" },
                { "screen", "virtualscreen" },
                { "correction", "correctionchip" },
                { "menu", "menupanel" },
            };

        /// <summary>Normalise a contract component string to a canonical key, or null if unknown.</summary>
        public static string KeyFor(string component)
        {
            string norm = Normalise(component);
            if (string.IsNullOrEmpty(norm)) return null;
            if (ByKey.ContainsKey(norm)) return norm;
            if (Aliases.TryGetValue(norm, out string aliased)) return aliased;
            return null;
        }

        /// <summary>Concrete component type for a canonical key, or null.</summary>
        public static System.Type TypeFor(string key)
        {
            if (key == null) return null;
            return ByKey.TryGetValue(key, out System.Type t) ? t : null;
        }

        /// <summary>Concrete type directly from a contract component string, or null.</summary>
        public static System.Type ResolveType(string component) => TypeFor(KeyFor(component));

        private static string Normalise(string s)
        {
            if (string.IsNullOrEmpty(s)) return null;
            var sb = new StringBuilder(s.Length);
            foreach (char c in s)
            {
                if (char.IsLetterOrDigit(c)) sb.Append(char.ToLowerInvariant(c));
            }
            return sb.ToString();
        }
    }
}
