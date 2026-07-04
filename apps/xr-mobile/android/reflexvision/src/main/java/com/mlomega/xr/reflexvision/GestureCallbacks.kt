package com.mlomega.xr.reflexvision

/**
 * Recognised gesture kinds surfaced to Unity. Kept as a small enum so the C#
 * [GestureBridge] can switch on the name (`.name()` over JNI).
 */
enum class GestureKind {
    /** Pinch began (thumb↔index crossed the enter threshold and held). */
    PINCH_BEGIN,

    /** Pinch is evolving — a new continuous zoom factor is available. */
    PINCH_UPDATE,

    /** Pinch released. */
    PINCH_END,

    /** Open palm held long enough to request the menu. */
    OPEN_PALM_MENU,

    /** Lateral swipe detected — request to hide the UI. */
    SWIPE_HIDE,
}

/**
 * Callbacks from [GesturePipeline] into the host (Unity via `AndroidJavaProxy`,
 * or a JVM test double). Invoked on the pipeline's result thread; the Unity
 * bridge marshals onto the main thread.
 *
 * Every gesture is a *discrete recognised event*, never one callback per camera
 * frame — the pipeline already applies hysteresis + minimum-hold debouncing, so
 * a held pinch produces one BEGIN, a stream of UPDATEs only when the zoom factor
 * changes meaningfully, and one END (matching the aggregated-ReflexEvent rule on
 * the C# side).
 */
interface GestureCallbacks {

    /**
     * A gesture was recognised.
     *
     * @param kind Which gesture (see [GestureKind]).
     * @param zoomFactor For pinch events, the continuous zoom factor (>= 1.0);
     *   0 for non-pinch gestures.
     * @param screenX Normalised (0..1) anchor x in image space (pinch/palm centre
     *   or swipe origin), for anchoring the UI. -1 if not applicable.
     * @param screenY Normalised (0..1) anchor y. -1 if not applicable.
     * @param timestampMs The frame timestamp (monotonic ms) the gesture resolved on.
     */
    fun onGesture(
        kind: GestureKind,
        zoomFactor: Float,
        screenX: Float,
        screenY: Float,
        timestampMs: Long,
    )

    /** Non-fatal error surfaced for logging/telemetry. */
    fun onError(message: String)
}
