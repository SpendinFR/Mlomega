package com.mlomega.xr.livetransport

/**
 * Connection lifecycle of the transport, surfaced to Unity as callbacks.
 *
 * Maps 1:1 to the states the C# `LiveTransportBridge` re-emits as events and the
 * StatusBar renders (E25). Ordering is not strictly monotone — a live session
 * may cycle CONNECTED -> DEGRADED -> RECONNECTING -> CONNECTED.
 */
enum class TransportState {
    /** No peer connection yet, or intentionally stopped. */
    DISCONNECTED,

    /** Signaling in flight / ICE gathering; first connect or after a drop. */
    CONNECTING,

    /** Media + DataChannel flowing normally. */
    CONNECTED,

    /**
     * Still connected but the adaptive controller has reduced bitrate/resolution
     * due to loss/RTT ("Transport vidéo dégradé", GUIDE_V19_REFERENCE §8.4).
     */
    DEGRADED,

    /** Connection lost; the bounded backoff loop is retrying. */
    RECONNECTING,
}

/**
 * Callbacks from the native transport into the host (Unity via
 * `AndroidJavaProxy`, or a JVM test double). All methods are invoked on a
 * background thread; the Unity bridge marshals them onto the main thread.
 */
interface LiveTransportCallbacks {

    /** Fired whenever [TransportState] changes. [detail] is a short reason. */
    fun onStateChanged(state: TransportState, detail: String)

    /**
     * A UIIntent (or any contracts message) arrived on the reliable DataChannel.
     * [json] is the raw JSON string; the C# side deserializes with the shared
     * Newtonsoft contracts (UIIntent). The device is expected to reply with a
     * UIReceipt via [LiveTransportPlugin.sendContractMessage].
     */
    fun onDataChannelMessage(json: String)

    /**
     * Periodic transport stats snapshot (bitrate, loss, rtt, resolution rung),
     * for the StatusBar/telemetry. [json] is a small flat JSON object.
     */
    fun onStats(json: String)

    /** Non-fatal error surfaced for logging/telemetry. */
    fun onError(message: String)
}
