// MLOmega V19 — E25
// Subtitle (§13.1): live ASR / translation at the bottom of vision (or offset
// under a speaker). Renders partial text muted and final text solid, updating in
// place as the same ui_intent_id is refreshed (§14.4 partial->final). Head-locked
// low in the FOV so it never occludes the scene. No truth chip: a subtitle is a
// transcript, its "truth" is carried by the text itself. Emits displayed once the
// line is visible; `seen` after the dwell.
using UnityEngine;

namespace MLOmega.XR.UI.Components
{
    public sealed class Subtitle : UIComponentBase
    {
        [SerializeField] private Vector2 _size = new Vector2(0.72f, 0.12f);
        [SerializeField] private Vector3 _bottomOffset = new Vector3(0f, -0.28f, 1.0f);

        private GlassPanel _panel;

        public override string ComponentKey => "subtitle";

        protected override void OnConfigured()
        {
            _panel = new GlassPanel(transform, _size, Theme,
                Context != null ? Context.GlassMaterial : null,
                withTitle: false, withBody: true, withTruthChip: false);
            if (_panel.Body != null)
            {
                _panel.Body.alignment = TMPro.TextAlignmentOptions.Center;
                _panel.Body.fontSize = 0.05f;
            }
        }

        protected override void Bind(Contracts.V19.UIIntent intent)
        {
            string text = IntentRead.Content(intent, "text", "");
            bool isFinal = IntentRead.Flag(intent.Content, "final",
                IntentRead.Flag(intent.UiHint, "final", true));
            string lang = IntentRead.Content(intent, "language", null);

            if (_panel.Body != null)
            {
                string prefix = string.IsNullOrEmpty(lang) ? "" : $"<size=70%><color=#9FB3C8>[{lang}] </color></size>";
                // Partial lines render muted / italic to signal they may still change.
                _panel.Body.text = isFinal
                    ? $"{prefix}{text}"
                    : $"{prefix}<i><color=#B0C4DE>{text}…</color></i>";
            }
            PlaceBottom();
        }

        private void PlaceBottom()
        {
            Camera cam = Context != null ? Context.Camera : Camera.main;
            if (cam == null) return;
            transform.SetPositionAndRotation(
                cam.transform.TransformPoint(_bottomOffset),
                Quaternion.LookRotation(transform.position - cam.transform.position, Vector3.up));
        }

        protected override void Update()
        {
            base.Update();
            if (Phase != UIComponentPhase.Idle)
            {
                PlaceBottom();
                _panel?.SetAlpha(CurrentAlpha);
            }
        }
    }
}
