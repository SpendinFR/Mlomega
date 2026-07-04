// MLOmega V19 — E25 EditMode tests
// UIReceiptTransportSink's bounded buffer (ReceiptOutbox): receipts sent while the
// transport is down are queued in order; the queue is capped (oldest dropped past
// the cap); a reconnect flush delivers the survivors in FIFO order. Driven through
// the pure ReceiptOutbox with an injectable "transport up/down" delegate — no live
// WebRTC needed.
using System.Collections.Generic;
using MLOmega.Contracts.V19;
using MLOmega.XR.UI;
using NUnit.Framework;

namespace MLOmega.XR.Tests
{
    public sealed class UIReceiptOutboxTests
    {
        private static UIReceipt Receipt(string id) => new UIReceipt
        {
            UiIntentId = id,
            Event = "displayed",
            ContractsVersion = "v19.0"
        };

        [Test]
        public void WhenConnected_SendsImmediately()
        {
            var sent = new List<string>();
            var outbox = new ReceiptOutbox(8, r => { sent.Add(r.UiIntentId); return true; });

            outbox.Send(Receipt("a"));
            outbox.Send(Receipt("b"));

            Assert.AreEqual(0, outbox.PendingCount);
            CollectionAssert.AreEqual(new[] { "a", "b" }, sent);
        }

        [Test]
        public void WhenDown_BuffersThenFlushesInOrderOnReconnect()
        {
            var sent = new List<string>();
            bool up = false;
            var outbox = new ReceiptOutbox(16, r =>
            {
                if (!up) return false;
                sent.Add(r.UiIntentId);
                return true;
            });

            outbox.Send(Receipt("a"));
            outbox.Send(Receipt("b"));
            outbox.Send(Receipt("c"));
            Assert.AreEqual(3, outbox.PendingCount, "all buffered while down");
            Assert.AreEqual(0, sent.Count);

            up = true;
            outbox.Flush();

            Assert.AreEqual(0, outbox.PendingCount);
            CollectionAssert.AreEqual(new[] { "a", "b", "c" }, sent, "FIFO order preserved");
        }

        [Test]
        public void BoundedBuffer_DropsOldestPastCap()
        {
            var outbox = new ReceiptOutbox(maxPending: 3, trySend: _ => false); // always down

            for (int i = 0; i < 5; i++) outbox.Send(Receipt("r" + i));

            Assert.AreEqual(3, outbox.PendingCount, "buffer capped at maxPending");
            Assert.AreEqual(2, outbox.DroppedCount, "two oldest dropped");

            // On reconnect, only the surviving (newest) three flush, oldest-first.
            var sent = new List<string>();
            outbox.TrySend = r => { sent.Add(r.UiIntentId); return true; };
            outbox.Flush();
            CollectionAssert.AreEqual(new[] { "r2", "r3", "r4" }, sent);
        }

        [Test]
        public void PartialFlush_StopsAtFirstFailure_KeepsRemainder()
        {
            var sent = new List<string>();
            int allowed = 2; // transport accepts 2 then "drops"
            var outbox = new ReceiptOutbox(16, r =>
            {
                if (allowed <= 0) return false;
                allowed--;
                sent.Add(r.UiIntentId);
                return true;
            });

            // Prime the queue while fully down.
            outbox.TrySend = _ => false;
            outbox.Send(Receipt("a"));
            outbox.Send(Receipt("b"));
            outbox.Send(Receipt("c"));

            // Now allow exactly two through.
            outbox.TrySend = r =>
            {
                if (allowed <= 0) return false;
                allowed--;
                sent.Add(r.UiIntentId);
                return true;
            };
            outbox.Flush();

            CollectionAssert.AreEqual(new[] { "a", "b" }, sent);
            Assert.AreEqual(1, outbox.PendingCount, "the undelivered receipt stays queued");
        }

        [Test]
        public void Send_NeverThrows_WhenTransportThrows()
        {
            var outbox = new ReceiptOutbox(4, _ => throw new System.Exception("channel closed"));
            Assert.DoesNotThrow(() => outbox.Send(Receipt("a")));
            Assert.AreEqual(1, outbox.PendingCount, "a throwing transport is treated as 'down'");
        }
    }
}
