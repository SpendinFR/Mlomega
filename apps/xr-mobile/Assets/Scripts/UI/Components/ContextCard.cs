// MLOmega V19 — E25
// ContextCard (§13.1): a short 2-3 line side panel carrying a memory / rule /
// relation from BrainLive, with its source. Anchored as a lateral head-locked
// panel. Being a BrainLive contextual hint it is, by the truth ladder, usually
// "probable"/"inferred": it therefore shows the discreet truth chip and, for
// relational readings, the hypothesis label — never presented as observation
// (§17.2, §17.3 social rule). Emits displayed/seen/dismissed via the base.
using UnityEngine;

namespace MLOmega.XR.UI.Components
{
    public sealed class ContextCard : UIComponentBase
    {
        [SerializeField] private Vector2 _size = new Vector2(0.42f, 0.20f);
        [SerializeField] private Vector3 _lateralOffset = new Vector3(0.34f, 0.02f, 1.1f);

        private GlassPanel _panel;

        public override string ComponentKey => "context_card";

        protected override void OnConfigured()
        {
            _panel = new GlassPanel(transform, _size, Theme,
                Context != null ? Context.GlassMaterial : null,
                withTitle: true, withBody: true, withTruthChip: true);
        }

        protected override void Bind(Contracts.V19.UIIntent intent)
        {
            string title = IntentRead.Content(intent, "title", "Context");
            string body = IntentRead.Content(intent, "text", IntentRead.Content(intent, "body", ""));
            string source = IntentRead.Content(intent, "source", null);

            if (_panel.Title != null) _panel.Title.text = title;
            if (_panel.Body != null)
            {
                _panel.Body.text = string.IsNullOrEmpty(source)
                    ? body
                    : $"{body}\n<size=80%><color=#9FB3C8>src: {source}</color></size>";
            }
            PlaceLateral();
        }

        protected override void OnTruth(TruthDescriptor truth)
        {
            if (_panel == null) return;
            _panel.SetAccent(truth.Accent);
            if (_panel.TruthChip != null)
            {
                _panel.TruthChip.text = TruthChipText(truth);
            }
        }

        public static string TruthChipText(TruthDescriptor truth)
        {
            if (truth.ShowHypothesisLabel) return "hypothesis";
            if (truth.ShowProbableBadge) return "probable";
            if (!string.IsNullOrEmpty(truth.AgeText)) return truth.AgeText;
            return string.Empty;
        }

        private void PlaceLateral()
        {
            Camera cam = Context != null ? Context.Camera : Camera.main;
            if (cam == null) return;
            transform.SetPositionAndRotation(
                cam.transform.TransformPoint(_lateralOffset),
                Quaternion.LookRotation(transform.position - cam.transform.position, Vector3.up));
        }

        protected override void Update()
        {
            base.Update();
            if (Phase != UIComponentPhase.Idle)
            {
                PlaceLateral();
                _panel?.SetAlpha(CurrentAlpha);
            }
        }
    }
}
