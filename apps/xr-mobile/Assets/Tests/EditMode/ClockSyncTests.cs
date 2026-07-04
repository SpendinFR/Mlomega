// MLOmega V19 — E23 EditMode tests
// Proves the Unity ClockSync client computes offset/RTT identically to the PC
// SessionHub (services/live-pc/sessionhub.py) — the SAME numeric inputs and
// tolerances as tests/v19/test_sessionhub.py::test_clock_offsets_are_coherent.
using System;
using System.Collections;
using MLOmega.XR.Core;
using NUnit.Framework;
using UnityEngine.TestTools;

namespace MLOmega.XR.Tests
{
    public sealed class ClockSyncTests
    {
        // Mirror of test_sessionhub.py case A: client clock 5ms ahead of server,
        // symmetric 1ms network legs.
        [Test]
        public void ComputeSample_ClientAhead_OffsetIsMinus5ms()
        {
            ClockSample s = ClockSync.ComputeSample(
                clientSendNs: 6_000_000,
                serverRecvNs: 1_000_000,
                serverSendNs: 1_100_000,
                clientRecvNs: 6_100_000);

            // Server test asserts abs(offset + 5ms) < 100us.
            Assert.Less(Math.Abs(s.OffsetNs + 5_000_000), 100_000);
            Assert.AreEqual(-5_000_000, s.OffsetNs);
            Assert.AreEqual(0, s.RttNs);
        }

        // Mirror of case B: client clock 8ms behind server.
        [Test]
        public void ComputeSample_ClientBehind_OffsetIsPlus8ms()
        {
            ClockSample s = ClockSync.ComputeSample(
                clientSendNs: -7_000_000,
                serverRecvNs: 1_000_000,
                serverSendNs: 1_100_000,
                clientRecvNs: -6_900_000);

            Assert.Less(Math.Abs(s.OffsetNs - 8_000_000), 100_000);
            Assert.AreEqual(8_000_000, s.OffsetNs);
            Assert.AreEqual(0, s.RttNs);
        }

        // Floor-division-by-2 must match Python's `// 2` for an odd negative sum.
        [Test]
        public void ComputeSample_OddNegativeSum_FloorsTowardNegativeInfinity()
        {
            // sum of the two deltas = -3  -> Python -3 // 2 == -2 ; C# -3/2 == -1.
            // client_send=0, server_recv=0, server_send=0, client_recv=3
            //   (server_recv-client_send) + (server_send-client_recv) = 0 + (-3) = -3
            ClockSample s = ClockSync.ComputeSample(0, 0, 0, 3);
            Assert.AreEqual(-2, s.OffsetNs, "offset must floor toward -inf like Python // 2");
        }

        [UnityTest]
        public IEnumerator RunBurst_PicksLowestRttSample()
        {
            // Two exchanges: the second has a much larger RTT; the burst must keep
            // the first (lower RTT) sample's offset.
            var clock = new ManualMonotonicClock();
            var transport = new ScriptedTransport(clock, new[]
            {
                // (serverRecv, serverSend, roundTripNs): the transport advances the
                // client clock by roundTripNs between send and recv, so
                // rtt = roundTripNs - (serverSend - serverRecv).
                new ScriptedTransport.Leg(serverRecv: 1_000_000, serverSend: 1_000_100, roundTripNs: 400),   // rtt 300
                new ScriptedTransport.Leg(serverRecv: 2_000_000, serverSend: 2_000_100, roundTripNs: 50_000), // rtt 49900
            });

            var sync = new ClockSync(transport, clock, samplesPerBurst: 2, maxRetries: 0);
            yield return sync.RunBurst("sess-1", "tok");

            Assert.AreEqual(ClockSync.SyncState.Synced, sync.State);
            Assert.IsTrue(sync.HasOffset);
            // The kept offset must come from the low-RTT leg (rtt 300), not 49900.
            Assert.AreEqual(300, sync.CurrentRttNs);
        }

        [UnityTest]
        public IEnumerator RunBurst_AllFail_BecomesUnsynced()
        {
            var clock = new ManualMonotonicClock();
            var transport = new AlwaysFailTransport();
            var sync = new ClockSync(transport, clock, samplesPerBurst: 2, maxRetries: 1);

            yield return sync.RunBurst("sess-1", "tok");

            Assert.AreEqual(ClockSync.SyncState.Unsynced, sync.State);
            Assert.IsFalse(sync.HasOffset);
            Assert.IsNotNull(sync.LastError);
        }

        // --- Test transports -------------------------------------------------

        /// <summary>
        /// Replays a fixed list of legs. Before each reply it forces the clock so
        /// the ClientSend/ClientRecv stamps ClockSync reads are exactly the ones the
        /// leg prescribes, making offset/rtt fully deterministic.
        /// </summary>
        private sealed class ScriptedTransport : IClockSyncTransport
        {
            public readonly struct Leg
            {
                public readonly long ServerRecv;
                public readonly long ServerSend;
                public readonly long RoundTripNs;

                public Leg(long serverRecv, long serverSend, long roundTripNs)
                {
                    ServerRecv = serverRecv;
                    ServerSend = serverSend;
                    RoundTripNs = roundTripNs;
                }
            }

            private readonly ManualMonotonicClock _clock;
            private readonly Leg[] _legs;
            private int _i;

            public ScriptedTransport(ManualMonotonicClock clock, Leg[] legs)
            {
                _clock = clock;
                _legs = legs;
            }

            public IEnumerator Exchange(string sessionId, string token, long clientSendNs,
                Action<ClockExchangeReply> onReply, Action<string> onError)
            {
                Leg leg = _legs[_i++];
                // Advance the client clock so ClockSync's post-reply NowNs() reads
                // clientSendNs + RoundTripNs, giving a deterministic per-leg RTT.
                _clock.Set(clientSendNs + leg.RoundTripNs);
                onReply(new ClockExchangeReply(leg.ServerRecv, leg.ServerSend));
                yield break;
            }
        }

        private sealed class AlwaysFailTransport : IClockSyncTransport
        {
            public IEnumerator Exchange(string sessionId, string token, long clientSendNs,
                Action<ClockExchangeReply> onReply, Action<string> onError)
            {
                onError("simulated network failure");
                yield break;
            }
        }
    }
}
