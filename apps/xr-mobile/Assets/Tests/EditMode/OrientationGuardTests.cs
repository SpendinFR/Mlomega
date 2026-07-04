// MLOmega V19 — E29 EditMode tests
// Validates the OrientationGuard decision (gravity → rotation bucket) and the
// EyeCaptureSource.SetRotation setter that stamps the envelope rotation. Pure math
// + a MonoBehaviour setter → deterministic, no device needed.
using MLOmega.XR.Core;
using NUnit.Framework;
using UnityEngine;

namespace MLOmega.XR.Tests
{
    public sealed class OrientationGuardTests
    {
        // Gravity "down" in device space per orientation (accelerometer convention:
        // landscape-up reads ~(0,-1,0); rotating the device rotates that vector).
        [Test]
        public void DecideBucket_LandscapeUp_IsZero()
        {
            int b = OrientationGuard.DecideBucket(new Vector3(0f, -1f, 0f), current: 2, hysteresisDeg: 12f, minGravity: 0.35f);
            Assert.AreEqual(0, b, "gravity straight down → no rotation");
        }

        [Test]
        public void DecideBucket_RotatedNinety_IsBucketOne()
        {
            // Device rotated 90° → gravity now points along +x in device space.
            int b = OrientationGuard.DecideBucket(new Vector3(1f, 0f, 0f), current: 0, hysteresisDeg: 12f, minGravity: 0.35f);
            Assert.AreEqual(1, b, "gravity to the right → 90° bucket");
        }

        [Test]
        public void DecideBucket_UpsideDown_IsBucketTwo()
        {
            int b = OrientationGuard.DecideBucket(new Vector3(0f, 1f, 0f), current: 0, hysteresisDeg: 12f, minGravity: 0.35f);
            Assert.AreEqual(2, b, "gravity up → 180° bucket");
        }

        [Test]
        public void DecideBucket_FreeFall_KeepsCurrentBucket()
        {
            int b = OrientationGuard.DecideBucket(new Vector3(0.01f, 0.01f, 0f), current: 3, hysteresisDeg: 12f, minGravity: 0.35f);
            Assert.AreEqual(3, b, "near-zero gravity (free fall/noise) must not flip orientation");
        }

        [Test]
        public void DecideBucket_Hysteresis_HoldsThroughSmallTilt()
        {
            // Currently at bucket 0; tilt only ~40° toward bucket 1 (below 45+hys) → hold.
            float rad = 40f * Mathf.Deg2Rad;
            // angle 40°: gravity direction = (sin40, -cos40)
            Vector3 g = new Vector3(Mathf.Sin(rad), -Mathf.Cos(rad), 0f);
            int b = OrientationGuard.DecideBucket(g, current: 0, hysteresisDeg: 12f, minGravity: 0.35f);
            Assert.AreEqual(0, b, "a small tilt below the hysteresis boundary must not switch buckets");
        }

        [Test]
        public void DecideBucket_Hysteresis_SwitchesPastBoundary()
        {
            // Tilt ~75° toward bucket 1 (past 45+12) → switch to 1.
            float rad = 75f * Mathf.Deg2Rad;
            Vector3 g = new Vector3(Mathf.Sin(rad), -Mathf.Cos(rad), 0f);
            int b = OrientationGuard.DecideBucket(g, current: 0, hysteresisDeg: 12f, minGravity: 0.35f);
            Assert.AreEqual(1, b, "a tilt well past the hysteresis boundary switches buckets");
        }

        [Test]
        public void SetRotation_RoundsToNearestQuadrant_AndReportsChange()
        {
            var go = new GameObject("cap");
            var cap = go.AddComponent<EyeCaptureSource>();
            try
            {
                Assert.IsTrue(cap.SetRotation(90), "0 → 90 changed");
                Assert.AreEqual(EyeCaptureSource.FrameRotation.Deg90, cap.Rotation);
                Assert.IsFalse(cap.SetRotation(85), "85 rounds to 90 → no change");
                Assert.IsTrue(cap.SetRotation(0), "90 → 0 changed");
                Assert.AreEqual(EyeCaptureSource.FrameRotation.Deg0, cap.Rotation);
                Assert.IsTrue(cap.SetRotation(270), "0 → 270 changed");
                Assert.AreEqual(EyeCaptureSource.FrameRotation.Deg270, cap.Rotation);
            }
            finally
            {
                Object.DestroyImmediate(go);
            }
        }
    }
}
