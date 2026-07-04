// MLOmega V19 — E25
// IReceiptSink that forwards UIReceipts over the E24 reliable DataChannel via
// LiveTransportBridge. Receipts feed delivery_adapter (E6) back into the BrainLive
// voie, so losing them silently would corrupt the feedback loop. Therefore this
// sink keeps a BOUNDED local FIFO: when the transport is down (Disconnected /
// Reconnecting) or a send fails, receipts are queued and flushed once the bridge
// reports Connected. The queue is capped (oldest dropped past the cap) so a long
// outage can never grow memory without bound — matching the reference's "borné,
// drop du plus ancien" rule for the whole live stack (ADR §E25).
using System.Collections.Generic;
using MLOmega.Contracts.V19;
using MLOmega.XR.Transport;
using UnityEngine;

namespace MLOmega.XR.UI
{
    public sealed class UIReceiptTransportSink : MonoBehaviour, IReceiptSink
    {
        [SerializeField] private LiveTransportBridge _bridge;

        [Tooltip("Max receipts buffered while the transport is down. Oldest dropped past this.")]
        [Min(1)]
        [SerializeField] private int _maxPending = 128;

        // FIFO of receipts awaiting delivery. Oldest at the front.
        private readonly Queue<UIReceipt> _pending = new Queue<UIReceipt>();

        /// <summary>Number of receipts currently buffered (test/telemetry visibility).</summary>
        public int PendingCount => _pending.Count;

        /// <summary>Total receipts dropped because the buffer was full (telemetry).</summary>
        public int DroppedCount { get; private set; }

        private void Awake()
        {
            if (_bridge == null) _bridge = FindAnyObjectByType<LiveTransportBridge>();
        }

        private void OnEnable()
        {
            if (_bridge != null) _bridge.StateChanged += OnTransportState;
        }

        private void OnDisable()
        {
            if (_bridge != null) _bridge.StateChanged -= OnTransportState;
        }

        /// <summary>
        /// Send a receipt. Never throws. If the transport is up and accepts it,
        /// it goes straight out; otherwise it is buffered for the next flush.
        /// </summary>
        public void Send(UIReceipt receipt)
        {
            if (receipt == null) return;

            // Try to drain anything already queued first so ordering is preserved.
            if (_pending.Count > 0)
            {
                Enqueue(receipt);
                Flush();
                return;
            }

            if (!TrySendNow(receipt))
            {
                Enqueue(receipt);
            }
        }

        /// <summary>
        /// Attempt to deliver all buffered receipts in order. Stops at the first
        /// failure (transport still down) and leaves the remainder queued.
        /// Exposed so tests and reconnect handlers can trigger a flush explicitly.
        /// </summary>
        public void Flush()
        {
            while (_pending.Count > 0)
            {
                UIReceipt head = _pending.Peek();
                if (!TrySendNow(head))
                {
                    break; // still down; keep the rest for later
                }
                _pending.Dequeue();
            }
        }

        // --- internals ------------------------------------------------------------

        private void OnTransportState(LiveTransportState state, string detail)
        {
            if (state == LiveTransportState.Connected)
            {
                Flush();
            }
        }

        private bool TrySendNow(UIReceipt receipt)
        {
            if (_bridge == null) return false;
            if (_bridge.State != LiveTransportState.Connected) return false;
            // SendReceipt returns false when the channel is not open; treat as "keep".
            return _bridge.SendReceipt(receipt);
        }

        private void Enqueue(UIReceipt receipt)
        {
            _pending.Enqueue(receipt);
            while (_pending.Count > _maxPending)
            {
                _pending.Dequeue(); // drop the oldest — bounded buffer
                DroppedCount++;
            }
        }
    }
}
