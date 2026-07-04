// MLOmega V19 — E25
// LensWindow (§13.1): a temporary focus panel for zoom / OCR / inspection
// (§14.5). Shows a cropped texture (when the intent carries one via a RenderTexture
// id resolved by the runtime) and/or extracted/translated text. Classified as a
// focus intent (priority rung 2). Never fabricates detail absent from the image —
// it only displays what the content dictionary provides. Pin/hide interactions map
// to acted/dismissed receipts.
using UnityEngine;
using UnityEngine.UI;

namespace MLOmega.XR.UI.Components
{
    public sealed class LensWindow : UIComponentBase
    {
        [SerializeField] private Vector2 _size = new Vector2(0.5f, 0.5f);
        [SerializeField] private Vector3 _focusOffset = new Vector3(0f, 0.05f, 0.9f);

        private GlassPanel _panel;
        private RawImage _content;

        public override string ComponentKey => "lens_window";

        protected override void OnConfigured()
        {
            _panel = new GlassPanel(transform, _size, Theme,
                Context != null ? Context.GlassMaterial : null,
                withTitle: true, withBody: true, withTruthChip: true);

            // Optional cropped-texture surface inset into the panel.
            var go = new GameObject("LensContent", typeof(RectTransform), typeof(CanvasRenderer), typeof(RawImage));
            var rt = go.GetComponent<RectTransform>();
            rt.SetParent(_panel.Root, false);
            rt.anchorMin = new Vector2(0.05f, 0.30f);
            rt.anchorMax = new Vector2(0.95f, 0.78f);
            rt.offsetMin = Vector2.zero; rt.offsetMax = Vector2.zero;
            _content = go.GetComponent<RawImage>();
            _content.raycastTarget = false;
            _content.color = new Color(1, 1, 1, 0);
        }

        /// <summary>Assign the cropped texture the runtime resolved for this lens.</summary>
        public void SetContentTexture(Texture texture)
        {
            if (_content == null) return;
            _content.texture = texture;
            _content.color = texture != null ? Color.white : new Color(1, 1, 1, 0);
        }

        protected override void Bind(Contracts.V19.UIIntent intent)
        {
            if (_panel.Title != null) _panel.Title.text = IntentRead.Content(intent, "title", "Lens");
            if (_panel.Body != null) _panel.Body.text = IntentRead.Content(intent, "text",
                IntentRead.Content(intent, "ocr", ""));
            PlaceFocus();
        }

        protected override void OnTruth(TruthDescriptor truth)
        {
            if (_panel == null) return;
            _panel.SetAccent(truth.Accent);
            if (_panel.TruthChip != null) _panel.TruthChip.text = ContextCard.TruthChipText(truth);
        }

        /// <summary>Pin the lens (explicit keep) — an acted receipt.</summary>
        public void Pin() => RaiseActed(new System.Collections.Generic.Dictionary<string, object>
        {
            { "kind", "lens_pinned" }
        });

        private void PlaceFocus()
        {
            Camera cam = Context != null ? Context.Camera : Camera.main;
            if (cam == null) return;
            transform.SetPositionAndRotation(
                cam.transform.TransformPoint(_focusOffset),
                Quaternion.LookRotation(transform.position - cam.transform.position, Vector3.up));
        }

        protected override void Update()
        {
            base.Update();
            if (Phase != UIComponentPhase.Idle)
            {
                PlaceFocus();
                _panel?.SetAlpha(CurrentAlpha);
                if (_content != null && _content.texture != null)
                {
                    Color c = _content.color; c.a = CurrentAlpha; _content.color = c;
                }
            }
        }
    }
}
