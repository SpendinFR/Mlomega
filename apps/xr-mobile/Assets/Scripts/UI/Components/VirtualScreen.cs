// MLOmega V19 — E25
// VirtualScreen (§13.1, §14.9): an explicit, user-requested resizable surface for
// a TV / replay / notes / work screen. Unlike the automatic components it only
// appears on a deliberate request, so it carries no "urgency" signalling. Content
// is a texture (player/PC stream, resolved by the runtime) or a notes body. The
// surface can be resized via SetSize; StatusBar keeps mic/camera controls (handled
// by StatusBar, not here). Emits displayed/seen/dismissed like the rest.
using UnityEngine;
using UnityEngine.UI;

namespace MLOmega.XR.UI.Components
{
    public sealed class VirtualScreen : UIComponentBase
    {
        [SerializeField] private Vector2 _size = new Vector2(1.2f, 0.68f);
        [SerializeField] private Vector3 _placeOffset = new Vector3(0f, 0.1f, 1.8f);

        private GlassPanel _panel;
        private RawImage _surface;
        private bool _placed;

        public override string ComponentKey => "virtual_screen";

        protected override void OnConfigured()
        {
            _panel = new GlassPanel(transform, _size, Theme,
                Context != null ? Context.GlassMaterial : null,
                withTitle: true, withBody: false, withTruthChip: false);

            var go = new GameObject("Surface", typeof(RectTransform), typeof(CanvasRenderer), typeof(RawImage));
            var rt = go.GetComponent<RectTransform>();
            rt.SetParent(_panel.Root, false);
            rt.anchorMin = new Vector2(0.02f, 0.02f);
            rt.anchorMax = new Vector2(0.98f, 0.86f);
            rt.offsetMin = Vector2.zero; rt.offsetMax = Vector2.zero;
            _surface = go.GetComponent<RawImage>();
            _surface.raycastTarget = false;
            _surface.color = new Color(0.03f, 0.04f, 0.06f, 1f);
        }

        /// <summary>Assign the stream/replay/notes render texture.</summary>
        public void SetSurfaceTexture(Texture texture)
        {
            if (_surface == null) return;
            _surface.texture = texture;
            _surface.color = texture != null ? Color.white : new Color(0.03f, 0.04f, 0.06f, 1f);
        }

        /// <summary>Resize the surface (explicit user resize).</summary>
        public void SetSize(Vector2 size)
        {
            _size = size;
            if (_panel != null) _panel.Root.sizeDelta = size;
        }

        protected override void Bind(Contracts.V19.UIIntent intent)
        {
            if (_panel.Title != null)
            {
                _panel.Title.text = IntentRead.Content(intent, "title",
                    IntentRead.Content(intent, "label", "Virtual Screen"));
            }
            // A one-shot placement in front of the user; the surface then stays put.
            Place();
            _placed = true;
        }

        private void Place()
        {
            Camera cam = Context != null ? Context.Camera : Camera.main;
            if (cam == null) return;
            transform.SetPositionAndRotation(
                cam.transform.TransformPoint(_placeOffset),
                Quaternion.LookRotation(transform.position - cam.transform.position, Vector3.up));
        }

        protected override void Update()
        {
            base.Update();
            if (Phase != UIComponentPhase.Idle)
            {
                if (!_placed) { Place(); _placed = true; }
                _panel?.SetAlpha(CurrentAlpha);
                if (_surface != null && _surface.texture != null)
                {
                    Color c = _surface.color; c.a = CurrentAlpha; _surface.color = c;
                }
            }
            else _placed = false;
        }
    }
}
