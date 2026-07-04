package com.mlomega.xr.livetransport

/**
 * All tunables for [LiveTransportPlugin]. Nothing here is hard-coded in the
 * transport logic — the degraded-mode thresholds, backoff bounds and codec
 * targets live in this object so they can be driven from `configs/*` (the same
 * policy source as the PC side, handoff §3.6 / GUIDE_V19_REFERENCE §8.4).
 *
 * @property signalingUrl Absolute URL of the unified signaling endpoint,
 *   e.g. `http://192.168.1.10:8710/webrtc/offer` (SessionHub HTTP port 8710).
 * @property sessionId Session id issued by `POST /session/create`.
 * @property token Ephemeral session token required by `/webrtc/offer`.
 * @property iceServers STUN/TURN servers; empty for a pure-LAN deployment.
 * @property video Video encode targets (H.264 low-latency).
 * @property audio Opus microphone parameters.
 * @property backoff Reconnect backoff policy (bounded).
 * @property adaptive Bitrate/resolution adaptation policy driven by getStats().
 * @property dataChannelLabel Reliable/ordered DataChannel label; must match the
 *   PC gateway ("contracts").
 */
data class LiveTransportConfig(
    val signalingUrl: String,
    val sessionId: String,
    val token: String,
    val iceServers: List<IceServerConfig> = emptyList(),
    val video: VideoConfig = VideoConfig(),
    val audio: AudioConfig = AudioConfig(),
    val backoff: BackoffConfig = BackoffConfig(),
    val adaptive: AdaptiveConfig = AdaptiveConfig(),
    val dataChannelLabel: String = "contracts",
    /** Signaling POST timeout, milliseconds. */
    val signalingTimeoutMs: Long = 8_000L,
)

/** ICE server entry (STUN/TURN). */
data class IceServerConfig(
    val urls: List<String>,
    val username: String? = null,
    val credential: String? = null,
)

/**
 * H.264 low-latency video targets. [preferredCodec] is enforced by rewriting the
 * SDP m=video payload order (see [SdpCodecPreference]); libwebrtc otherwise picks
 * VP8/VP9 first.
 */
data class VideoConfig(
    val preferredCodec: String = "H264",
    val width: Int = 1280,
    val height: Int = 720,
    val fps: Int = 30,
    /** Start bitrate, bits per second. */
    val startBitrateBps: Int = 2_500_000,
    val minBitrateBps: Int = 400_000,
    val maxBitrateBps: Int = 4_000_000,
    /**
     * H.264 packetization-mode & profile-level-id hints forced into the SDP fmtp
     * line so the encoder runs constrained-baseline (widest low-latency support).
     */
    val profileLevelId: String = "42e01f",
    val packetizationMode: Int = 1,
)

/** Opus microphone parameters (20 ms frames per handoff/guide). */
data class AudioConfig(
    val enabled: Boolean = true,
    /** Opus frame duration in milliseconds. */
    val ptimeMs: Int = 20,
    val startBitrateBps: Int = 24_000,
    val useDtx: Boolean = true,
    val enableEchoCancellation: Boolean = true,
    val enableNoiseSuppression: Boolean = true,
)

/**
 * Bounded exponential backoff for reconnection. The loop never sleeps longer
 * than [maxDelayMs] and gives up after [maxAttempts] consecutive failures
 * (0 = unbounded attempts, still delay-capped).
 */
data class BackoffConfig(
    val initialDelayMs: Long = 500L,
    val maxDelayMs: Long = 10_000L,
    val multiplier: Double = 2.0,
    val jitterMs: Long = 250L,
    val maxAttempts: Int = 0,
)

/**
 * Adaptive bitrate/resolution policy. Reactions are computed from `getStats()`
 * (fraction lost + round-trip time). All thresholds are config, not magic
 * numbers — the "Transport vidéo dégradé" row of GUIDE_V19_REFERENCE §8.4.
 */
data class AdaptiveConfig(
    /** How often to poll getStats(), milliseconds. */
    val statsIntervalMs: Long = 2_000L,
    /** Packet-loss fraction (0..1) above which we step bitrate/resolution DOWN. */
    val lossDownThreshold: Double = 0.05,
    /** Packet-loss fraction below which, if stable, we step back UP. */
    val lossUpThreshold: Double = 0.02,
    /** RTT (ms) above which we also step down even if loss is low. */
    val rttDownThresholdMs: Double = 300.0,
    /** Multiplicative bitrate step applied on each down/up decision. */
    val downFactor: Double = 0.6,
    val upFactor: Double = 1.2,
    /** Consecutive healthy polls required before stepping back up. */
    val healthyPollsToRecover: Int = 3,
    /**
     * Resolution scale-down ladder (denominators of the capture resolution).
     * 1 = full, 2 = half each axis, etc. The transport applies the current rung
     * as a scaleResolutionDownBy on the video sender.
     */
    val resolutionLadder: List<Double> = listOf(1.0, 1.5, 2.0, 3.0),
)
