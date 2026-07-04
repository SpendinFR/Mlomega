// MLOmega V19 — E25
// UIRuntime: the renderer that turns the broker's arbitrated ActiveIntents into
// live liquid-glass components and back. It:
//   * subscribes to the UIIntentBroker (IntentAdmitted / IntentFading / IntentDropped);
//   * maps each intent's `component` field to a concrete UIComponentBase type
//     using the §13.1 design-system table (UIComponentRegistry);
//   * pools components per type (simple free-list) so admit/drop churn does not
//     allocate GameObjects every frame;
//   * wires each component with the SceneCache (anchoring) and the IReceiptSink
//     (receipts) via a shared UIComponentContext;
//   * shares one LiquidGlass Material across every panel so they batch and the
//     Kawase blur is sampled once.
// The broker owns arbitration/priority/TTL/density; the runtime owns instantiation
// and lifecycle only — it never second-guesses the broker's decisions.
using System.Collections.Generic;
using MLOmega.Contracts.V19;
using MLOmega.XR.Scene;
using MLOmega.XR.UI.Components;
using UnityEngine;

namespace MLOmega.XR.UI
{
    public sealed class UIRuntime : MonoBehaviour
    {
        [SerializeField] private UIIntentBroker _broker;
        [SerializeField] private SceneCache _sceneCache;
        [SerializeField] private UITheme _theme;
        [SerializeField] private Camera _camera;

        [Tooltip("LiquidGlass material shared by all panels. If null, built from the shader at runtime.")]
        [SerializeField] private Material _glassMaterial;

        [Tooltip("Component that receives outbound receipts (transport sink).")]
        [SerializeField] private MonoBehaviour _receiptSinkBehaviour; // must implement IReceiptSink

        private IReceiptSink _sink;
        private UIComponentContext _context;

        // ui_intent_id -> live component instance rendering it.
        private readonly Dictionary<string, UIComponentBase> _live =
            new Dictionary<string, UIComponentBase>();
        // component-key -> free-list of recycled instances.
        private readonly Dictionary<string, Stack<UIComponentBase>> _pool =
            new Dictionary<string, Stack<UIComponentBase>>();

        private Transform _root;

        private void Awake()
        {
            if (_broker == null) _broker = FindAnyObjectByType<UIIntentBroker>();
            if (_sceneCache == null) _sceneCache = FindAnyObjectByType<SceneCache>();
            if (_camera == null) _camera = Camera.main;
            _sink = _receiptSinkBehaviour as IReceiptSink;

            if (_glassMaterial == null)
            {
                Shader s = Shader.Find("MLOmega/LiquidGlass");
                if (s != null) _glassMaterial = new Material(s);
            }

            _context = new UIComponentContext(_sceneCache, _glassMaterial, _camera);

            var rootGo = new GameObject("UIRuntimeRoot");
            rootGo.transform.SetParent(transform, false);
            _root = rootGo.transform;
        }

        private void OnEnable()
        {
            if (_broker == null) return;
            _broker.IntentAdmitted += OnAdmitted;
            _broker.IntentFading += OnFading;
            _broker.IntentDropped += OnDropped;
        }

        private void OnDisable()
        {
            if (_broker == null) return;
            _broker.IntentAdmitted -= OnAdmitted;
            _broker.IntentFading -= OnFading;
            _broker.IntentDropped -= OnDropped;
        }

        // ------------------------------------------------------------------
        //  Broker event handlers
        // ------------------------------------------------------------------

        private void OnAdmitted(ActiveIntent active)
        {
            UIIntent intent = active.Intent;
            string id = intent.UiIntentId;

            // Dedup refresh: same id already live -> just update its payload.
            if (_live.TryGetValue(id, out UIComponentBase existing))
            {
                existing.Refresh(intent);
                return;
            }

            string key = UIComponentRegistry.KeyFor(intent.Component);
            if (key == null)
            {
                Debug.LogWarning($"[UIRuntime] no component mapping for '{intent.Component}' (intent {id}).");
                return;
            }

            UIComponentBase comp = Rent(key);
            if (comp == null) return;
            _live[id] = comp;
            comp.Admit(intent, _sink, OnComponentRecycled);
        }

        private void OnFading(ActiveIntent active, UIIntentDropReason reason)
        {
            if (_live.TryGetValue(active.Intent.UiIntentId, out UIComponentBase comp))
            {
                // A user suppression is the only "dismissed" receipt (§13.3);
                // everything else fades silently (drop-reason already journaled).
                bool userDismissed = reason == UIIntentDropReason.UserSuppressed;
                comp.BeginFadeOut(userDismissed);
            }
        }

        private void OnDropped(UIIntent intent, UIIntentDropReason reason)
        {
            // The component fades itself out on IntentFading and returns to the pool
            // via OnComponentRecycled; if it was never shown (e.g. no mapping) there
            // is nothing to do. We keep the _live map cleaned on recycle.
        }

        // ------------------------------------------------------------------
        //  Pooling
        // ------------------------------------------------------------------

        private UIComponentBase Rent(string key)
        {
            if (_pool.TryGetValue(key, out Stack<UIComponentBase> stack) && stack.Count > 0)
            {
                return stack.Pop();
            }
            return Create(key);
        }

        private UIComponentBase Create(string key)
        {
            System.Type type = UIComponentRegistry.TypeFor(key);
            if (type == null) return null;

            var go = new GameObject(key);
            go.transform.SetParent(_root, false);
            var comp = (UIComponentBase)go.AddComponent(type);
            comp.Configure(_context, _theme);
            go.SetActive(false);
            return comp;
        }

        private void OnComponentRecycled(UIComponentBase comp)
        {
            // Remove from the live map (find by value — the set is tiny, bounded by
            // the density cap, so a linear scan is fine).
            string foundId = null;
            foreach (KeyValuePair<string, UIComponentBase> kv in _live)
            {
                if (ReferenceEquals(kv.Value, comp)) { foundId = kv.Key; break; }
            }
            if (foundId != null) _live.Remove(foundId);

            string key = comp.ComponentKey;
            if (!_pool.TryGetValue(key, out Stack<UIComponentBase> stack))
            {
                stack = new Stack<UIComponentBase>();
                _pool[key] = stack;
            }
            stack.Push(comp);
        }
    }
}
