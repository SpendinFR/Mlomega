package com.mlomega.xr.reflexvision

/**
 * All tunables for [GesturePipeline]. Nothing in the gesture state machine is a
 * magic number — thresholds, hysteresis margins and minimum hold durations live
 * here so they can be driven from `configs/gestures.yaml` (the same policy source
 * as the rest of the pipeline, handoff §7 / GUIDE_V19_REFERENCE §9.2). Populated
 * from Unity through [GestureConfigFactory.forUnity] (JNI-friendly), or built
 * with defaults in JVM unit tests.
 *
 * The three recognised gestures (GUIDE_V19_REFERENCE §9.2 / handoff §7):
 *   * pinch  — thumb-tip ↔ index-tip distance, normalised by hand span, mapped to
 *              a continuous zoom factor (begin / evolve / end). Drives LensWindow.
 *   * open palm held — a menu-open gesture; requires a sustained hold to fire.
 *   * lateral swipe — a fast horizontal wrist travel to hide the UI.
 *
 * @property modelAssetPath Absolute path to the MediaPipe HandLandmarker/Gesture
 *   `.task` bundle on app storage (downloaded at first run — see README, never
 *   committed).
 * @property numHands Max hands tracked (1 keeps latency lowest on device).
 * @property minHandDetectionConfidence MediaPipe detection confidence floor.
 * @property minHandPresenceConfidence MediaPipe presence confidence floor.
 * @property minTrackingConfidence MediaPipe landmark-tracking confidence floor.
 */
data class GestureConfig(
    val modelAssetPath: String,
    val numHands: Int = 1,
    val minHandDetectionConfidence: Float = 0.5f,
    val minHandPresenceConfidence: Float = 0.5f,
    val minTrackingConfidence: Float = 0.5f,
    val pinch: PinchConfig = PinchConfig(),
    val palm: PalmConfig = PalmConfig(),
    val swipe: SwipeConfig = SwipeConfig(),
)

/**
 * Pinch → continuous zoom mapping. Distance is thumb-tip↔index-tip in normalised
 * image space, divided by a hand-span reference (wrist↔middle-MCP) so it is scale
 * invariant to how close the hand is to the camera.
 *
 * Hysteresis (anti-false-positive, handoff §7): the pinch must cross
 * [enterNormalizedDistance] to *begin* and only ends once it relaxes past the
 * larger [exitNormalizedDistance]; it must also hold [minHoldMs] before a begin
 * fires. The normalised distance is then mapped linearly from
 * [zoomAtMinDistance] (fully closed) to [zoomAtMaxDistance] (open) to produce a
 * continuous zoom factor.
 */
data class PinchConfig(
    val enterNormalizedDistance: Float = 0.28f,
    val exitNormalizedDistance: Float = 0.40f,
    val minHoldMs: Long = 60L,
    /** Normalised distance treated as "fully pinched" (clamps the zoom factor high). */
    val closedNormalizedDistance: Float = 0.06f,
    /** Normalised distance treated as "wide open" (clamps the zoom factor low). */
    val openNormalizedDistance: Float = 0.55f,
    /** Zoom factor emitted at [closedNormalizedDistance]. */
    val zoomAtMinDistance: Float = 4.0f,
    /** Zoom factor emitted at [openNormalizedDistance]. */
    val zoomAtMaxDistance: Float = 1.0f,
)

/**
 * Open-palm-held → menu. Fires once the hand is classified "Open_Palm" by the
 * MediaPipe GestureRecognizer continuously for [minHoldMs] with at least
 * [minScore] category score; re-arms only after the palm drops.
 */
data class PalmConfig(
    val minHoldMs: Long = 550L,
    val minScore: Float = 0.55f,
)

/**
 * Lateral swipe → hide UI. Detected when the wrist landmark travels more than
 * [minTravelNormalized] horizontally within [maxDurationMs], at a speed above
 * [minSpeedNormalizedPerSec]. A cooldown ([cooldownMs]) prevents a single wave
 * from firing repeatedly.
 */
data class SwipeConfig(
    val minTravelNormalized: Float = 0.30f,
    val maxDurationMs: Long = 350L,
    val minSpeedNormalizedPerSec: Float = 1.2f,
    val cooldownMs: Long = 700L,
    /** Vertical travel above this (relative to horizontal) rejects the swipe as non-lateral. */
    val maxVerticalRatio: Float = 0.6f,
)

/**
 * Small JNI-friendly factory so Unity can build a [GestureConfig] with defaults
 * without constructing every nested data class over `AndroidJavaObject`. The
 * threshold-bearing nested configs keep their Kotlin defaults; Unity passes only
 * what it routinely overrides (model path + hand count).
 */
object GestureConfigFactory {
    @JvmStatic
    fun forUnity(modelAssetPath: String, numHands: Int): GestureConfig =
        GestureConfig(modelAssetPath = modelAssetPath, numHands = if (numHands < 1) 1 else numHands)
}
