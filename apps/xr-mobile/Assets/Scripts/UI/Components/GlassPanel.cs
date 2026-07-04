// MLOmega V19 — E25
// Shared builder + controller for a single liquid-glass UGUI panel. Concrete
// components compose one or more of these instead of each re-wiring a Canvas,
// Image, LiquidGlass material and TMP labels by hand. It:
//   * creates a world-space RectTransform with an Image driven by the
//     LiquidGlass material (per-instance MaterialPropertyBlock, so tint/accent/
//     blur come straight from UITheme and the truth accent);
//   * exposes a title + body + a small "truth chip" TMP label row;
//   * carries the animated alpha from UIComponentBase into the vertex colour of
//     the Image and the alpha of the labels (glass fades as one).
// UGUI world-space is mandated by the spec ("UGUI world-space"); building it in
// code (not prefabs) keeps the whole design system reviewable in one place and
// avoids unvalidatable .prefab YAML, consistent with the scene-builder decision.
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace MLOmega.XR.UI.Components
{
    public sealed class GlassPanel
    {
        private static readonly int PanelTintId = Shader.PropertyToID("_PanelTint");
        private static readonly int RimColorId = Shader.PropertyToID("_RimColor");
        private static readonly int AccentColorId = Shader.PropertyToID("_AccentColor");
        private static readonly int BlurStrengthId = Shader.PropertyToID("_BlurStrength");
        private static readonly int GrainId = Shader.PropertyToID("_Grain");
        private static readonly int RimWidthId = Shader.PropertyToID("_RimWidth");
        private static readonly int AccentMixId = Shader.PropertyToID("_AccentMix");

        public RectTransform Root { get; }
        public Image Background { get; }
        public TMP_Text Title { get; }
        public TMP_Text Body { get; }
        public TMP_Text TruthChip { get; }

        private readonly UITheme _theme;
        private readonly MaterialPropertyBlock _mpb = new MaterialPropertyBlock();
        private Color _accent;

        /// <summary>
        /// Build a glass panel under <paramref name="parent"/> sized to
        /// <paramref name="size"/> (local units). A shared LiquidGlass material is
        /// passed in by the runtime so every panel batches; per-instance colours go
        /// through a MaterialPropertyBlock.
        /// </summary>
        public GlassPanel(Transform parent, Vector2 size, UITheme theme, Material glassMaterial,
            bool withTitle, bool withBody, bool withTruthChip)
        {
            _theme = theme;
            _accent = theme != null ? theme.RimColor : Color.white;

            // A world-space Canvas so this is genuine UGUI in the XR world. 1 canvas
            // unit == 1 world metre (root scale 1), so sizes/font are in metres.
            var go = new GameObject("GlassPanel",
                typeof(RectTransform), typeof(Canvas), typeof(GraphicRaycaster));
            var canvas = go.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.WorldSpace;
            Root = go.GetComponent<RectTransform>();
            Root.SetParent(parent, false);
            Root.sizeDelta = size;

            var bgGo = new GameObject("Bg", typeof(RectTransform), typeof(CanvasRenderer), typeof(Image));
            var bgRt = bgGo.GetComponent<RectTransform>();
            bgRt.SetParent(Root, false);
            bgRt.anchorMin = Vector2.zero; bgRt.anchorMax = Vector2.one;
            bgRt.offsetMin = Vector2.zero; bgRt.offsetMax = Vector2.zero;
            Background = bgGo.GetComponent<Image>();
            Background.material = glassMaterial;
            Background.raycastTarget = false;
            Background.color = Color.white;

            if (withTitle) Title = MakeLabel(Root, "Title", 0.055f, FontStyles.Bold, TextAlignmentOptions.TopLeft);
            if (withBody) Body = MakeLabel(Root, "Body", 0.045f, FontStyles.Normal, TextAlignmentOptions.TopLeft);
            if (withTruthChip) TruthChip = MakeLabel(Root, "TruthChip", 0.038f, FontStyles.Italic, TextAlignmentOptions.BottomRight);

            LayoutLabels(size);
            PushStatic();
        }

        private TMP_Text MakeLabel(Transform parent, string name, float fontSize,
            FontStyles style, TextAlignmentOptions align)
        {
            var go = new GameObject(name, typeof(RectTransform));
            var rt = go.GetComponent<RectTransform>();
            rt.SetParent(parent, false);
            var tmp = go.AddComponent<TextMeshProUGUI>();
            tmp.fontSize = fontSize;
            tmp.enableAutoSizing = false;
            tmp.fontStyle = style;
            tmp.alignment = align;
            tmp.color = _theme != null ? _theme.TextColor : Color.white;
            tmp.raycastTarget = false;
            tmp.textWrappingMode = TextWrappingModes.Normal;
            return tmp;
        }

        private void LayoutLabels(Vector2 size)
        {
            float pad = 0.04f;
            if (Title != null)
            {
                var rt = Title.rectTransform;
                rt.anchorMin = new Vector2(0, 1); rt.anchorMax = new Vector2(1, 1);
                rt.pivot = new Vector2(0.5f, 1);
                rt.sizeDelta = new Vector2(-pad * 2, size.y * 0.32f);
                rt.anchoredPosition = new Vector2(0, -pad);
            }
            if (Body != null)
            {
                var rt = Body.rectTransform;
                rt.anchorMin = new Vector2(0, 0); rt.anchorMax = new Vector2(1, 1);
                rt.pivot = new Vector2(0.5f, 0.5f);
                rt.offsetMin = new Vector2(pad, pad);
                rt.offsetMax = new Vector2(-pad, -(pad + size.y * (Title != null ? 0.34f : 0f)));
            }
            if (TruthChip != null)
            {
                var rt = TruthChip.rectTransform;
                rt.anchorMin = new Vector2(0, 0); rt.anchorMax = new Vector2(1, 0);
                rt.pivot = new Vector2(0.5f, 0);
                rt.sizeDelta = new Vector2(-pad * 2, size.y * 0.22f);
                rt.anchoredPosition = new Vector2(0, pad);
            }
        }

        /// <summary>Set the truth accent colour (rim tint) for this panel.</summary>
        public void SetAccent(Color accent)
        {
            _accent = accent;
            PushStatic();
        }

        /// <summary>Push the animated alpha (0..1) into the glass + labels.</summary>
        public void SetAlpha(float alpha)
        {
            if (Background != null)
            {
                Color c = Background.color;
                c.a = alpha;
                Background.color = c;
            }
            SetLabelAlpha(Title, alpha);
            SetLabelAlpha(Body, alpha);
            SetLabelAlpha(TruthChip, alpha);
        }

        private void SetLabelAlpha(TMP_Text label, float alpha)
        {
            if (label == null) return;
            Color c = label.color;
            c.a = alpha;
            label.color = c;
        }

        // Push the non-animated shader params from the theme + accent.
        private void PushStatic()
        {
            if (Background == null) return;
            Background.GetPropertyBlock(_mpb);
            if (_theme != null)
            {
                _mpb.SetColor(PanelTintId, _theme.PanelTint);
                _mpb.SetColor(RimColorId, _theme.RimColor);
                _mpb.SetColor(AccentColorId, _accent);
                _mpb.SetFloat(BlurStrengthId, _theme.BlurStrength);
                _mpb.SetFloat(GrainId, _theme.Grain);
                _mpb.SetFloat(RimWidthId, _theme.RimWidth);
                _mpb.SetFloat(AccentMixId, 0.5f);
            }
            else
            {
                _mpb.SetColor(AccentColorId, _accent);
            }
            Background.SetPropertyBlock(_mpb);
        }
    }
}
