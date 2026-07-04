package com.mlomega.xr.livetransport

/**
 * JNI-friendly factory for [LiveTransportConfig].
 *
 * Kotlin data-class default arguments are not reachable through Unity's
 * `AndroidJavaObject`/`AndroidJavaClass` JNI bridge (JNI sees only the full-arity
 * constructor). This factory exposes a small `@JvmStatic` entry point Unity can
 * call with the handful of values it actually varies, filling the rest from the
 * config defaults. (ADR docs/DECISIONS.md §E24.)
 */
object LiveTransportConfigFactory {

    /**
     * Build a [LiveTransportConfig] with the video geometry Unity supplies and
     * all policy thresholds left at their config defaults (which the app can
     * later override from `configs/*`).
     */
    @JvmStatic
    fun forUnity(
        signalingUrl: String,
        sessionId: String,
        token: String,
        width: Int,
        height: Int,
        fps: Int,
    ): LiveTransportConfig = LiveTransportConfig(
        signalingUrl = signalingUrl,
        sessionId = sessionId,
        token = token,
        video = VideoConfig(width = width, height = height, fps = fps),
    )
}
