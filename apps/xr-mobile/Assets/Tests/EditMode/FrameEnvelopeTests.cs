// MLOmega V19 — E23 EditMode tests
// Validates the FrameEnvelope produced by EyeCaptureSource: field correctness,
// "f_<n>" frame_id formatting, and monotonic frame_id + capture timestamps.
using System;
using System.Collections.Generic;
using MLOmega.Contracts.V19;
using MLOmega.XR.Core;
using NUnit.Framework;
using UnityEngine;

namespace MLOmega.XR.Tests
{
    public sealed class FrameEnvelopeTests
    {
        [Test]
        public void FormatFrameId_ProducesUnderscorePrefixedDecimal()
        {
            Assert.AreEqual("f_0", EyeCaptureSource.FormatFrameId(0));
            Assert.AreEqual("f_1", EyeCaptureSource.FormatFrameId(1));
            Assert.AreEqual("f_42", EyeCaptureSource.FormatFrameId(42));
            Assert.AreEqual("f_1000000", EyeCaptureSource.FormatFrameId(1_000_000));
        }

        [Test]
        public void BuildEnvelopeInto_PopulatesAllContractFields()
        {
            var env = new FrameEnvelope();
            var pose = new StampedPose(
                new Vector3(1f, 2f, 3f),
                new Quaternion(0.1f, 0.2f, 0.3f, 0.4f),
                isTracking: true,
                monotonicNs: 123);
            var when = new DateTime(2026, 7, 4, 12, 30, 0, DateTimeKind.Utc);

            EyeCaptureSource.BuildEnvelopeInto(
                env, sessionId: "xrs-abc", frameNumber: 7,
                captureMonotonicNs: 999, capturedAtUtc: when, pose: pose,
                rotation: 90, source: ContractDefaults.FrameSource.XrealEye);

            Assert.AreEqual("v19.0", env.ContractsVersion);
            Assert.AreEqual("xrs-abc", env.SessionId);
            Assert.AreEqual("f_7", env.FrameId);
            Assert.AreEqual(999, env.CaptureMonotonicNs);
            Assert.AreEqual(90, env.Rotation);
            Assert.AreEqual("xreal_eye", env.Source);
            StringAssert.StartsWith("2026-07-04T12:30:00", env.CapturedAtUtc);
            StringAssert.EndsWith("Z", env.CapturedAtUtc);

            Assert.IsNotNull(env.Pose);
            CollectionAssert.AreEqual(new List<double> { 1f, 2f, 3f }, env.Pose.Position);
            CollectionAssert.AreEqual(
                new List<double> { 0.1f, 0.2f, 0.3f, 0.4f }, env.Pose.Rotation);
        }

        [Test]
        public void BuildEnvelopeInto_ReusesEnvelopeWithoutReallocatingLists()
        {
            var env = new FrameEnvelope();
            var pose = new StampedPose(Vector3.one, Quaternion.identity, true, 0);
            var when = DateTime.UtcNow;

            EyeCaptureSource.BuildEnvelopeInto(env, "s", 0, 0, when, pose, 0, "simulated");
            List<double> posRef = env.Pose.Position;
            List<double> rotRef = env.Pose.Rotation;

            EyeCaptureSource.BuildEnvelopeInto(env, "s", 1, 0, when, pose, 0, "simulated");

            // Same list instances reused across frames (allocation-free hot path).
            Assert.AreSame(posRef, env.Pose.Position);
            Assert.AreSame(rotRef, env.Pose.Rotation);
            Assert.AreEqual(3, env.Pose.Position.Count);
            Assert.AreEqual(4, env.Pose.Rotation.Count);
        }

        [Test]
        public void FrameIds_AreMonotonicAndTimestampsNonDecreasing()
        {
            var env = new FrameEnvelope();
            var pose = new StampedPose(Vector3.zero, Quaternion.identity, false, 0);

            long prevMono = long.MinValue;
            long prevNum = -1;
            for (long i = 0; i < 100; i++)
            {
                long monotonic = i * 33_000_000L; // ~30fps
                EyeCaptureSource.BuildEnvelopeInto(
                    env, "s", i, monotonic, DateTime.UtcNow, pose, 0, "simulated");

                Assert.AreEqual($"f_{i}", env.FrameId);

                long parsed = long.Parse(env.FrameId.Substring(2));
                Assert.Greater(parsed, prevNum, "frame_id numeric part must strictly increase");
                Assert.GreaterOrEqual(env.CaptureMonotonicNs, prevMono,
                    "capture_monotonic_ns must be non-decreasing");
                prevNum = parsed;
                prevMono = env.CaptureMonotonicNs;
            }
        }
    }
}
