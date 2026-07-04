// MLOmega V19 — E25
// Tiny typed readers over the loosely-typed contract dictionaries (UIIntent.content
// / anchor / ui_hint are Dictionary<string,object> because they come from JSON).
// Centralised so every component parses them identically and no component invents
// its own casting rules. Mirrors the same helpers SceneCache uses internally.
using System.Collections.Generic;
using System.Globalization;
using MLOmega.Contracts.V19;
using UnityEngine;

namespace MLOmega.XR.UI.Components
{
    public static class IntentRead
    {
        public static string Str(Dictionary<string, object> d, string key, string fallback = null)
        {
            if (d != null && d.TryGetValue(key, out object v) && v != null)
            {
                return v as string ?? v.ToString();
            }
            return fallback;
        }

        public static string Content(UIIntent intent, string key, string fallback = null) =>
            Str(intent?.Content, key, fallback);

        public static string Anchor(UIIntent intent, string key, string fallback = null) =>
            Str(intent?.Anchor, key, fallback);

        public static string Hint(UIIntent intent, string key, string fallback = null) =>
            Str(intent?.UiHint, key, fallback);

        public static double Num(Dictionary<string, object> d, string key, double fallback)
        {
            if (d != null && d.TryGetValue(key, out object v) && v != null)
            {
                switch (v)
                {
                    case double dv: return dv;
                    case float fv: return fv;
                    case long lv: return lv;
                    case int iv: return iv;
                    default:
                        if (double.TryParse(v.ToString(), NumberStyles.Float,
                            CultureInfo.InvariantCulture, out double p)) return p;
                        break;
                }
            }
            return fallback;
        }

        public static bool Flag(Dictionary<string, object> d, string key, bool fallback = false)
        {
            if (d != null && d.TryGetValue(key, out object v) && v != null)
            {
                if (v is bool b) return b;
                if (bool.TryParse(v.ToString(), out bool p)) return p;
            }
            return fallback;
        }

        /// <summary>Read a 2D screen point [x,y] (0..1 normalised) from an anchor/content list.</summary>
        public static bool TryPoint(Dictionary<string, object> d, string key, out Vector2 point)
        {
            point = Vector2.zero;
            if (d != null && d.TryGetValue(key, out object v) && v is IList<object> list && list.Count >= 2)
            {
                point = new Vector2(ToFloat(list[0]), ToFloat(list[1]));
                return true;
            }
            return false;
        }

        private static float ToFloat(object o)
        {
            if (o == null) return 0f;
            if (double.TryParse(o.ToString(), NumberStyles.Float, CultureInfo.InvariantCulture, out double d))
                return (float)d;
            return 0f;
        }
    }
}
