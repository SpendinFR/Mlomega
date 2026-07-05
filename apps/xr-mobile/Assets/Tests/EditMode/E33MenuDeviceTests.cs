// MLOmega V19 — E33 EditMode tests.
//
// The on-glasses half of E33, all offline (no PC, no native plugin):
//   * MenuPanel builds its action grid; a selection routes the SAME device_command
//     as the voice path through the shared DeviceCommandHandler (ONE execution path)
//     and emits a UIReceipt (acted);
//   * DeviceCommandHandler set_ui_mode hide_all leaves ONLY the StatusBar (broker
//     drops every non-status intent, §13.2-1); privacy_pause toggles the StatusBar;
//   * the swipe->hide gesture is wired (the previously-missing Unity handler): a
//     SwipeHide GestureEvent drives the hide_all command via MenuGestureController;
//   * the open-palm gesture toggles the MenuPanel.
using System.Collections.Generic;
using MLOmega.Contracts.V19;
using MLOmega.XR.Reflex;
using MLOmega.XR.Scene;
using MLOmega.XR.UI;
using MLOmega.XR.UI.Components;
using NUnit.Framework;
using UnityEngine;

namespace MLOmega.XR.Tests
{
    public sealed class E33MenuDeviceTests
    {
        private readonly List<GameObject> _spawned = new List<GameObject>();

        private sealed class CapturingReceipts : IReceiptSink
        {
            public readonly List<UIReceipt> Receipts = new List<UIReceipt>();
            public void Send(UIReceipt r) => Receipts.Add(r);
        }

        [TearDown]
        public void TearDown()
        {
            foreach (GameObject go in _spawned) if (go != null) Object.DestroyImmediate(go);
            _spawned.Clear();
        }

        private T Make<T>(string name) where T : Component
        {
            var go = new GameObject(name);
            _spawned.Add(go);
            return go.AddComponent<T>();
        }

        private UIIntent CardIntent(string id, string component = "context_card", string producer = "brainlive") =>
            new UIIntent { UiIntentId = id, Component = component, Producer = producer, TtlMs = 60000 };

        // ---------------------------------------------------------------- MenuPanel
        [Test]
        public void MenuPanel_BuildsActionGrid()
        {
            var menu = Make<MenuPanel>("menu");
            menu.BuildDefaultActions();
            Assert.IsTrue(menu.Actions.Count >= 8, "menu should expose the full action grid");
            // Contains the load-bearing entries.
            var labels = new HashSet<string>();
            foreach (var a in menu.Actions) labels.Add(a.Label);
            Assert.Contains("FreeGuy", new List<string>(labels));
            Assert.Contains("Cacher", new List<string>(labels));
            Assert.Contains("Maps", new List<string>(labels));
            Assert.Contains("Fermer", new List<string>(labels));
        }

        [Test]
        public void MenuPanel_Selection_RoutesDeviceCommand_And_Receipt()
        {
            var broker = Make<UIIntentBroker>("broker");
            var handler = Make<DeviceCommandHandler>("handler");
            typeof(DeviceCommandHandler)
                .GetField("_broker", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(handler, broker);

            var menu = Make<MenuPanel>("menu");
            typeof(MenuPanel)
                .GetField("_commandHandler", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(menu, handler);
            var receipts = new CapturingReceipts();
            menu.ReceiptSink = receipts;
            menu.BuildDefaultActions();

            // Find the "Cacher" (hide_all) action and select it.
            int idx = menu.Actions.Count;
            for (int i = 0; i < menu.Actions.Count; i++)
                if (menu.Actions[i].Label == "Cacher") { idx = i; break; }
            Assert.Less(idx, menu.Actions.Count);

            menu.Open();
            bool ok = menu.Select(idx);
            Assert.IsTrue(ok);
            // The command reached the broker -> density is HideAll (same as voice).
            Assert.AreEqual(UIDensityMode.HideAll, broker.Density);
            // A UIReceipt (acted) was emitted for the selection.
            Assert.AreEqual(1, receipts.Receipts.Count);
            Assert.AreEqual("acted", receipts.Receipts[0].Event);
            // Selecting closes the menu.
            Assert.IsFalse(menu.IsOpen);
        }

        // ---------------------------------------------------- density / hide_all
        [Test]
        public void HideAll_LeavesOnlyStatusBar()
        {
            var broker = Make<UIIntentBroker>("broker");
            // Admit a normal (non-status) card and a status intent.
            broker.Submit(CardIntent("card-1"));
            broker.Submit(new UIIntent { UiIntentId = "status-1", Component = "status_bar", Producer = "ultralive", TtlMs = 60000 });
            broker.Tick(1);
            Assert.AreEqual(2, broker.ActiveIntents.Count);

            // hide_all: the non-status card is dropped, the status survives (§13.2-1).
            broker.SetDensity(UIDensityMode.HideAll);
            broker.Tick(2);
            int nonStatus = 0, status = 0;
            foreach (ActiveIntent ai in broker.ActiveIntents)
            {
                if (ai.IsStatus) status++; else if (!ai.Fading) nonStatus++;
            }
            Assert.AreEqual(0, nonStatus, "hide_all must drop every non-status intent");
            Assert.AreEqual(1, status, "the status surface survives hide_all");

            // A new normal card is refused while hidden.
            broker.Submit(CardIntent("card-2"));
            broker.Tick(3);
            bool hasCard2 = false;
            foreach (ActiveIntent ai in broker.ActiveIntents) if (ai.Intent.UiIntentId == "card-2") hasCard2 = true;
            Assert.IsFalse(hasCard2, "hide_all refuses new non-status intents");
        }

        [Test]
        public void DeviceCommand_SetUiMode_DrivesBrokerDensity()
        {
            var broker = Make<UIIntentBroker>("broker");
            var handler = Make<DeviceCommandHandler>("handler");
            typeof(DeviceCommandHandler)
                .GetField("_broker", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(handler, broker);

            Assert.IsTrue(handler.TryHandleRaw("{\"type\":\"device_command\",\"action\":\"set_ui_mode\",\"ui_mode\":\"freeguy\"}"));
            Assert.AreEqual(UIDensityMode.FreeGuy, broker.Density);

            handler.Execute(new DeviceCommand { Type = "device_command", Action = "set_ui_mode", UiMode = "normal" });
            Assert.AreEqual(UIDensityMode.Normal, broker.Density);
        }

        [Test]
        public void NonDeviceCommandJson_IsNotClaimed()
        {
            var handler = Make<DeviceCommandHandler>("handler");
            // A normal UIIntent json is NOT a device command.
            Assert.IsFalse(handler.TryHandleRaw("{\"ui_intent_id\":\"x\",\"component\":\"context_card\"}"));
        }

        // ------------------------------------------------------- swipe -> hide
        [Test]
        public void SwipeHide_Gesture_DrivesHideAll()
        {
            var broker = Make<UIIntentBroker>("broker");
            var handler = Make<DeviceCommandHandler>("handler");
            typeof(DeviceCommandHandler)
                .GetField("_broker", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(handler, broker);

            var controller = Make<MenuGestureController>("gest");
            typeof(MenuGestureController)
                .GetField("_commandHandler", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(controller, handler);

            // The previously-missing Unity handler: a swipe hides the UI.
            controller.OnGesture(new GestureEvent(GestureKind.SwipeHide, 0f, new Vector2(0.5f, 0.5f), 0));
            Assert.AreEqual(UIDensityMode.HideAll, broker.Density);
        }

        [Test]
        public void OpenPalm_Gesture_TogglesMenu()
        {
            var menu = Make<MenuPanel>("menu");
            menu.BuildDefaultActions();
            var controller = Make<MenuGestureController>("gest");
            typeof(MenuGestureController)
                .GetField("_menu", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
                .SetValue(controller, menu);

            Assert.IsFalse(menu.IsOpen);
            controller.OnGesture(new GestureEvent(GestureKind.OpenPalmMenu, 0f, Vector2.zero, 0));
            Assert.IsTrue(menu.IsOpen);
            controller.OnGesture(new GestureEvent(GestureKind.OpenPalmMenu, 0f, Vector2.zero, 0));
            Assert.IsFalse(menu.IsOpen);
        }

        [Test]
        public void MenuRegistry_ResolvesMenuPanel()
        {
            Assert.AreEqual(typeof(MenuPanel), UIComponentRegistry.ResolveType("menu_panel"));
            Assert.AreEqual(typeof(MenuPanel), UIComponentRegistry.ResolveType("menu"));
        }
    }
}
