// MLOmega V19 — E23 EditMode tests
// Round-trips UIIntent / UIReceipt / FrameEnvelope through Newtonsoft.Json and
// asserts the wire keys stay snake_case (the JSON the PC services consume). This
// is the Unity-side proof that the [JsonProperty] rewrite in the synced contracts
// serializes to the same shape as the Python pydantic models.
using System.Collections.Generic;
using MLOmega.Contracts.V19;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using NUnit.Framework;

namespace MLOmega.XR.Tests
{
    public sealed class ContractSerializationTests
    {
        [Test]
        public void UIIntent_RoundTrips_WithSnakeCaseKeys()
        {
            var intent = new UIIntent
            {
                ContractsVersion = "v19.0",
                UiIntentId = "ui-1",
                Producer = "brainlive",
                Component = "context_card",
                TruthLevel = "observed",
                Confidence = 0.83,
                Priority = 0.92,
                TtlMs = 4000,
                EvidenceRefs = new List<string> { "ev-1", "ev-2" },
                DeliveryId = "del-9",
                Content = new Dictionary<string, object> { { "text", "hello" } }
            };

            string json = JsonConvert.SerializeObject(intent);
            JObject o = JObject.Parse(json);

            // Wire keys must be snake_case (what delivery_adapter / companion-web read).
            Assert.AreEqual("v19.0", (string)o["contracts_version"]);
            Assert.AreEqual("ui-1", (string)o["ui_intent_id"]);
            Assert.AreEqual("brainlive", (string)o["producer"]);
            Assert.AreEqual("observed", (string)o["truth_level"]);
            Assert.AreEqual(0.92, (double)o["priority"], 1e-9);
            Assert.AreEqual(4000, (long)o["ttl_ms"]);
            Assert.AreEqual("del-9", (string)o["delivery_id"]);

            var back = JsonConvert.DeserializeObject<UIIntent>(json);
            Assert.AreEqual(intent.UiIntentId, back.UiIntentId);
            Assert.AreEqual(intent.Producer, back.Producer);
            Assert.AreEqual(intent.Priority, back.Priority, 1e-9);
            Assert.AreEqual(intent.TtlMs, back.TtlMs);
            CollectionAssert.AreEqual(intent.EvidenceRefs, back.EvidenceRefs);
        }

        [Test]
        public void UIReceipt_RoundTrips_WithSnakeCaseKeys()
        {
            var receipt = new UIReceipt
            {
                ContractsVersion = "v19.0",
                UiIntentId = "ui-1",
                DeliveryId = "del-9",
                Event = "displayed",
                ObservedAt = "2026-07-04T12:00:00Z",
                Source = "unity-xr"
            };

            string json = JsonConvert.SerializeObject(receipt);
            JObject o = JObject.Parse(json);

            Assert.AreEqual("ui-1", (string)o["ui_intent_id"]);
            Assert.AreEqual("del-9", (string)o["delivery_id"]);
            Assert.AreEqual("displayed", (string)o["event"]);
            Assert.AreEqual("2026-07-04T12:00:00Z", (string)o["observed_at"]);

            var back = JsonConvert.DeserializeObject<UIReceipt>(json);
            Assert.AreEqual(receipt.UiIntentId, back.UiIntentId);
            Assert.AreEqual(receipt.Event, back.Event);
            Assert.AreEqual(receipt.DeliveryId, back.DeliveryId);
        }

        [Test]
        public void FrameEnvelope_RoundTrips_WithNestedPose()
        {
            var env = new FrameEnvelope
            {
                ContractsVersion = "v19.0",
                SessionId = "xrs-1",
                FrameId = "f_5",
                CaptureMonotonicNs = 123456789,
                CapturedAtUtc = "2026-07-04T12:00:00.0000000Z",
                Rotation = 90,
                Source = "phone_camera",
                Pose = new Pose
                {
                    ContractsVersion = "v19.0",
                    Position = new List<double> { 1, 2, 3 },
                    Rotation = new List<double> { 0, 0, 0, 1 }
                }
            };

            string json = JsonConvert.SerializeObject(env);
            JObject o = JObject.Parse(json);

            Assert.AreEqual("xrs-1", (string)o["session_id"]);
            Assert.AreEqual("f_5", (string)o["frame_id"]);
            Assert.AreEqual(123456789, (long)o["capture_monotonic_ns"]);
            Assert.AreEqual(90, (long)o["rotation"]);
            Assert.AreEqual("phone_camera", (string)o["source"]);
            Assert.AreEqual(3, ((JArray)o["pose"]["position"]).Count);

            var back = JsonConvert.DeserializeObject<FrameEnvelope>(json);
            Assert.AreEqual("f_5", back.FrameId);
            Assert.AreEqual(90, back.Rotation);
            CollectionAssert.AreEqual(env.Pose.Position, back.Pose.Position);
            CollectionAssert.AreEqual(env.Pose.Rotation, back.Pose.Rotation);
        }
    }
}
