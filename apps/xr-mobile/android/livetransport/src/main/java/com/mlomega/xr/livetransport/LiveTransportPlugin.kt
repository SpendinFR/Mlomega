package com.mlomega.xr.livetransport

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import org.webrtc.AudioSource
import org.webrtc.AudioTrack
import org.webrtc.DataChannel
import org.webrtc.DefaultVideoDecoderFactory
import org.webrtc.DefaultVideoEncoderFactory
import org.webrtc.EglBase
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.MediaStreamTrack
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.RtpParameters
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import org.webrtc.SurfaceTextureHelper
import org.webrtc.VideoSource
import org.webrtc.VideoTrack
import java.nio.charset.StandardCharsets
import java.util.concurrent.atomic.AtomicBoolean

/**
 * MLOmega V19 mobile live transport (E24).
 *
 * Owns one WebRTC [PeerConnection] to the PC gateway: uplink H.264 video (fed
 * from Unity via [VideoFrameFeeder]), uplink Opus 20 ms microphone audio, and a
 * reliable/ordered `contracts` [DataChannel] carrying FrameEnvelope/LocalTrack
 * up and UIIntent down (UIReceipt back up). Signaling is the token-gated
 * `POST /webrtc/offer` (see [SignalingClient]); reconnection uses bounded
 * backoff; bitrate/resolution adapt from `getStats()` per
 * GUIDE_V19_REFERENCE §8.4.
 *
 * Threading: public methods are safe to call from the Unity main thread. All
 * WebRTC work runs on an internal coroutine scope; [LiveTransportCallbacks] fire
 * on background threads (the Unity bridge marshals to the main thread).
 *
 * Lifecycle: [start] -> (connected) -> [sendContractMessage]/[getState] -> [stop].
 * Call [dispose] once when the app shuts down to free the factory/EGL context.
 *
 * This module cannot be compiled in the authoring environment (no Android SDK);
 * it is written against the pinned GetStream API (see build.gradle.kts) and the
 * real compile/run belongs to the S25 validation gate (ADR docs/DECISIONS §E24).
 */
class LiveTransportPlugin(
    private val appContext: Context,
    private val config: LiveTransportConfig,
    private val videoFeeder: VideoFrameFeeder,
    private val callbacks: LiveTransportCallbacks,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val started = AtomicBoolean(false)

    private val eglBase: EglBase = EglBase.create()
    private lateinit var factory: PeerConnectionFactory

    private var peer: PeerConnection? = null
    private var dataChannel: DataChannel? = null
    private var videoSource: VideoSource? = null
    private var videoCapturer: UnityFrameCapturer? = null
    private var videoTrack: VideoTrack? = null
    private var surfaceHelper: SurfaceTextureHelper? = null
    private var audioSource: AudioSource? = null
    private var audioTrack: AudioTrack? = null

    @Volatile private var state: TransportState = TransportState.DISCONNECTED
    @Volatile private var adaptiveJob: Job? = null

    // Adaptive controller state.
    private var currentBitrateBps: Int = config.video.startBitrateBps
    private var resolutionRung: Int = 0
    private var healthyPolls: Int = 0

    /** Current transport state (thread-safe snapshot). */
    fun getState(): TransportState = state

    /**
     * Begin the connect/reconnect loop. Idempotent; a second call is a no-op
     * while already started.
     */
    fun start() {
        if (!started.compareAndSet(false, true)) return
        initFactory()
        scope.launch { connectLoop() }
    }

    /** Tear down the peer and stop all capture. Safe to call repeatedly. */
    fun stop() {
        if (!started.compareAndSet(true, false)) return
        scope.launch { teardownPeer("stopped") }
        setState(TransportState.DISCONNECTED, "stopped")
    }

    /**
     * Send a contracts JSON message (typically a UIReceipt) up the reliable
     * DataChannel. Returns false if the channel is not open.
     */
    fun sendContractMessage(json: String): Boolean {
        val dc = dataChannel ?: return false
        if (dc.state() != DataChannel.State.OPEN) return false
        val bytes = json.toByteArray(StandardCharsets.UTF_8)
        val buffer = DataChannel.Buffer(java.nio.ByteBuffer.wrap(bytes), false /* binary */)
        return dc.send(buffer)
    }

    /** Release the factory / EGL context. Call once at app shutdown. */
    fun dispose() {
        stop()
        scope.cancel()
        try {
            if (this::factory.isInitialized) factory.dispose()
        } catch (_: Throwable) {
        }
        eglBase.release()
    }

    // --- setup ----------------------------------------------------------------

    private fun initFactory() {
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(appContext)
                .setEnableInternalTracer(false)
                .createInitializationOptions(),
        )
        // Hardware H.264 encoder preferred; software fallback enabled so the
        // encoder never hard-fails on a device lacking HW H.264.
        val encoderFactory = DefaultVideoEncoderFactory(
            eglBase.eglBaseContext,
            /* enableIntelVp8Encoder = */ true,
            /* enableH264HighProfile = */ false,
        )
        val decoderFactory = DefaultVideoDecoderFactory(eglBase.eglBaseContext)
        factory = PeerConnectionFactory.builder()
            .setVideoEncoderFactory(encoderFactory)
            .setVideoDecoderFactory(decoderFactory)
            .createPeerConnectionFactory()
    }

    private fun rtcConfig(): PeerConnection.RTCConfiguration {
        val ice = config.iceServers.map { s ->
            val b = PeerConnection.IceServer.builder(s.urls)
            if (s.username != null) b.setUsername(s.username)
            if (s.credential != null) b.setPassword(s.credential)
            b.createIceServer()
        }
        return PeerConnection.RTCConfiguration(ice).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            bundlePolicy = PeerConnection.BundlePolicy.MAXBUNDLE
            rtcpMuxPolicy = PeerConnection.RtcpMuxPolicy.REQUIRE
            continualGatheringPolicy =
                PeerConnection.ContinualGatheringPolicy.GATHER_CONTINUALLY
            // Low-latency: prefer the newer congestion control; keep candidate
            // pool small on LAN.
            iceCandidatePoolSize = 1
            enableCpuOveruseDetection = true
        }
    }

    // --- connect / reconnect --------------------------------------------------

    private suspend fun connectLoop() {
        var attempt = 0
        var delayMs = config.backoff.initialDelayMs
        while (scope.isActive && started.get()) {
            try {
                setState(TransportState.CONNECTING, "attempt ${attempt + 1}")
                establish()
                // establish() returns once the peer reaches a terminal failed/
                // closed state; loop to reconnect.
                if (!started.get()) break
                setState(TransportState.RECONNECTING, "peer lost")
            } catch (t: Throwable) {
                callbacks.onError("connect failed: ${t.message}")
                setState(TransportState.RECONNECTING, t.message ?: "error")
            }

            attempt++
            if (config.backoff.maxAttempts > 0 && attempt >= config.backoff.maxAttempts) {
                setState(TransportState.DISCONNECTED, "backoff exhausted")
                break
            }
            val jitter = (Math.random() * config.backoff.jitterMs).toLong()
            delay(delayMs + jitter)
            delayMs = (delayMs * config.backoff.multiplier)
                .toLong()
                .coerceAtMost(config.backoff.maxDelayMs)
        }
    }

    /** Build the peer, negotiate, and suspend until it terminates. */
    private suspend fun establish() {
        val terminated = kotlinx.coroutines.CompletableDeferred<Unit>()

        val pc = factory.createPeerConnection(rtcConfig(), object : PcObserver() {
            override fun onIceConnectionChange(newState: PeerConnection.IceConnectionState) {
                when (newState) {
                    PeerConnection.IceConnectionState.CONNECTED,
                    PeerConnection.IceConnectionState.COMPLETED -> {
                        healthyPolls = 0
                        setState(TransportState.CONNECTED, "ice $newState")
                        startAdaptiveController()
                    }
                    PeerConnection.IceConnectionState.FAILED,
                    PeerConnection.IceConnectionState.CLOSED,
                    PeerConnection.IceConnectionState.DISCONNECTED -> {
                        if (!terminated.isCompleted) terminated.complete(Unit)
                    }
                    else -> {}
                }
            }

            override fun onDataChannel(dc: DataChannel) {
                // The offerer creates the channel; this fires only if the PC
                // created one. Kept for symmetry/robustness.
                bindDataChannel(dc)
            }
        }) ?: throw IllegalStateException("createPeerConnection returned null")
        peer = pc

        // Reliable/ordered DataChannel named to match the PC gateway ("contracts").
        val dcInit = DataChannel.Init().apply {
            ordered = true // reliable + ordered (no maxRetransmits/maxRetransmitTime)
        }
        bindDataChannel(pc.createDataChannel(config.dataChannelLabel, dcInit))

        addAudioTrack(pc)
        addVideoTrack(pc)

        // Create offer, apply codec preferences, set local, exchange via HTTP.
        val offer = createOfferSuspending(pc)
        var munged = SdpCodecPreference.preferVideoCodec(
            offer.description,
            config.video.preferredCodec,
            config.video.packetizationMode,
            config.video.profileLevelId,
        )
        munged = SdpCodecPreference.configureOpus(munged, config.audio.ptimeMs, config.audio.useDtx)
        val localDesc = SessionDescription(offer.type, munged)
        setLocalSuspending(pc, localDesc)

        val answer = withContext(Dispatchers.IO) {
            SignalingClient(
                config.signalingUrl, config.sessionId, config.token, config.signalingTimeoutMs,
            ).exchangeOffer(localDesc.description, localDesc.type.canonicalForm())
        }
        setRemoteSuspending(
            pc,
            SessionDescription(SessionDescription.Type.fromCanonicalForm(answer.type), answer.sdp),
        )

        applyVideoBitrate(currentBitrateBps)
        applyResolutionRung(resolutionRung)

        terminated.await()
        adaptiveJob?.cancel()
        adaptiveJob = null
    }

    private fun bindDataChannel(dc: DataChannel) {
        dataChannel = dc
        dc.registerObserver(object : DataChannel.Observer {
            override fun onBufferedAmountChange(previousAmount: Long) {}
            override fun onStateChange() {}
            override fun onMessage(buffer: DataChannel.Buffer) {
                val data = ByteArray(buffer.data.remaining())
                buffer.data.get(data)
                val text = String(data, StandardCharsets.UTF_8)
                callbacks.onDataChannelMessage(text)
            }
        })
    }

    private fun addAudioTrack(pc: PeerConnection) {
        if (!config.audio.enabled) return
        val constraints = MediaConstraints().apply {
            fun flag(k: String, v: Boolean) =
                mandatory.add(MediaConstraints.KeyValuePair(k, v.toString()))
            flag("googEchoCancellation", config.audio.enableEchoCancellation)
            flag("googNoiseSuppression", config.audio.enableNoiseSuppression)
            flag("googAutoGainControl", true)
            flag("googHighpassFilter", true)
        }
        val src = factory.createAudioSource(constraints)
        val track = factory.createAudioTrack("mic0", src)
        audioSource = src
        audioTrack = track
        pc.addTrack(track, listOf("mlomega-stream"))
    }

    private fun addVideoTrack(pc: PeerConnection) {
        val helper = SurfaceTextureHelper.create("CaptureThread", eglBase.eglBaseContext)
        val capturer = UnityFrameCapturer(videoFeeder)
        val src = factory.createVideoSource(capturer.isScreencast)
        capturer.initialize(helper, appContext, src.capturerObserver)
        capturer.startCapture(config.video.width, config.video.height, config.video.fps)
        val track = factory.createVideoTrack("cam0", src)
        surfaceHelper = helper
        videoCapturer = capturer
        videoSource = src
        videoTrack = track
        pc.addTrack(track, listOf("mlomega-stream"))
    }

    // --- adaptive bitrate / resolution (getStats) -----------------------------

    private fun startAdaptiveController() {
        if (adaptiveJob?.isActive == true) return
        adaptiveJob = scope.launch {
            while (isActive && started.get()) {
                delay(config.adaptive.statsIntervalMs)
                pollStatsAndAdapt()
            }
        }
    }

    private fun pollStatsAndAdapt() {
        val pc = peer ?: return
        pc.getStats { report ->
            var lostFraction = 0.0
            var rttMs = 0.0
            for (stat in report.statsMap.values) {
                when (stat.type) {
                    "outbound-rtp" -> {
                        // fractionLost isn't always present on outbound; use remote-
                        // inbound below. Kept for encoders that expose it.
                    }
                    "remote-inbound-rtp" -> {
                        (stat.members["fractionLost"] as? Number)?.let {
                            lostFraction = it.toDouble()
                        }
                        (stat.members["roundTripTime"] as? Number)?.let {
                            rttMs = it.toDouble() * 1000.0
                        }
                    }
                }
            }
            adaptTo(lostFraction, rttMs)
        }
    }

    /** Pure decision function so the ladder logic is testable and config-driven. */
    private fun adaptTo(lostFraction: Double, rttMs: Double) {
        val a = config.adaptive
        val unhealthy = lostFraction > a.lossDownThreshold || rttMs > a.rttDownThresholdMs
        if (unhealthy) {
            healthyPolls = 0
            val next = (currentBitrateBps * a.downFactor).toInt()
                .coerceAtLeast(config.video.minBitrateBps)
            if (next < currentBitrateBps) {
                currentBitrateBps = next
                applyVideoBitrate(currentBitrateBps)
            }
            if (resolutionRung < a.resolutionLadder.lastIndex) {
                resolutionRung++
                applyResolutionRung(resolutionRung)
            }
            setState(TransportState.DEGRADED, "loss=$lostFraction rtt=${rttMs}ms")
        } else if (lostFraction < a.lossUpThreshold && rttMs < a.rttDownThresholdMs) {
            healthyPolls++
            if (healthyPolls >= a.healthyPollsToRecover) {
                healthyPolls = 0
                val next = (currentBitrateBps * a.upFactor).toInt()
                    .coerceAtMost(config.video.maxBitrateBps)
                if (next > currentBitrateBps) {
                    currentBitrateBps = next
                    applyVideoBitrate(currentBitrateBps)
                }
                if (resolutionRung > 0) {
                    resolutionRung--
                    applyResolutionRung(resolutionRung)
                }
                if (state == TransportState.DEGRADED) {
                    setState(TransportState.CONNECTED, "recovered")
                }
            }
        }
        emitStats(lostFraction, rttMs)
    }

    private fun applyVideoBitrate(bps: Int) {
        val sender = peer?.senders?.firstOrNull { it.track()?.kind() == MediaStreamTrack.VIDEO_TRACK_KIND }
            ?: return
        val params: RtpParameters = sender.parameters
        for (enc in params.encodings) {
            enc.maxBitrateBps = bps
            enc.minBitrateBps = config.video.minBitrateBps
        }
        sender.parameters = params
    }

    private fun applyResolutionRung(rung: Int) {
        val ladder = config.adaptive.resolutionLadder
        val scale = ladder.getOrElse(rung) { ladder.last() }
        val sender = peer?.senders?.firstOrNull { it.track()?.kind() == MediaStreamTrack.VIDEO_TRACK_KIND }
            ?: return
        val params = sender.parameters
        for (enc in params.encodings) {
            enc.scaleResolutionDownBy = scale
        }
        sender.parameters = params
    }

    private fun emitStats(lostFraction: Double, rttMs: Double) {
        val json = JSONObject()
            .put("state", state.name)
            .put("bitrate_bps", currentBitrateBps)
            .put("resolution_rung", resolutionRung)
            .put("loss_fraction", lostFraction)
            .put("rtt_ms", rttMs)
            .toString()
        callbacks.onStats(json)
    }

    // --- teardown / state -----------------------------------------------------

    private fun teardownPeer(reason: String) {
        adaptiveJob?.cancel(); adaptiveJob = null
        try { videoCapturer?.stopCapture() } catch (_: Throwable) {}
        try { videoCapturer?.dispose() } catch (_: Throwable) {}
        try { videoTrack?.dispose() } catch (_: Throwable) {}
        try { videoSource?.dispose() } catch (_: Throwable) {}
        try { surfaceHelper?.dispose() } catch (_: Throwable) {}
        try { audioTrack?.dispose() } catch (_: Throwable) {}
        try { audioSource?.dispose() } catch (_: Throwable) {}
        try { dataChannel?.dispose() } catch (_: Throwable) {}
        try { peer?.dispose() } catch (_: Throwable) {}
        videoCapturer = null; videoTrack = null; videoSource = null; surfaceHelper = null
        audioTrack = null; audioSource = null; dataChannel = null; peer = null
    }

    private fun setState(next: TransportState, detail: String) {
        if (next == state) return
        state = next
        callbacks.onStateChanged(next, detail)
    }

    // --- SDP coroutine adapters ----------------------------------------------

    private suspend fun createOfferSuspending(pc: PeerConnection): SessionDescription {
        val constraints = MediaConstraints().apply {
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveAudio", "false"))
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveVideo", "false"))
        }
        val deferred = kotlinx.coroutines.CompletableDeferred<SessionDescription>()
        pc.createOffer(object : SimpleSdpObserver() {
            override fun onCreateSuccess(sdp: SessionDescription) = run { deferred.complete(sdp) }
            override fun onCreateFailure(error: String?) =
                run { deferred.completeExceptionally(IllegalStateException("createOffer: $error")) }
        }, constraints)
        return deferred.await()
    }

    private suspend fun setLocalSuspending(pc: PeerConnection, sdp: SessionDescription) {
        val d = kotlinx.coroutines.CompletableDeferred<Unit>()
        pc.setLocalDescription(object : SimpleSdpObserver() {
            override fun onSetSuccess() = run { d.complete(Unit) }
            override fun onSetFailure(error: String?) =
                run { d.completeExceptionally(IllegalStateException("setLocal: $error")) }
        }, sdp)
        d.await()
    }

    private suspend fun setRemoteSuspending(pc: PeerConnection, sdp: SessionDescription) {
        val d = kotlinx.coroutines.CompletableDeferred<Unit>()
        pc.setRemoteDescription(object : SimpleSdpObserver() {
            override fun onSetSuccess() = run { d.complete(Unit) }
            override fun onSetFailure(error: String?) =
                run { d.completeExceptionally(IllegalStateException("setRemote: $error")) }
        }, sdp)
        d.await()
    }
}

/** No-op [SdpObserver] base so overrides stay terse. */
private open class SimpleSdpObserver : SdpObserver {
    override fun onCreateSuccess(sdp: SessionDescription) {}
    override fun onSetSuccess() {}
    override fun onCreateFailure(error: String?) {}
    override fun onSetFailure(error: String?) {}
}

/** No-op [PeerConnection.Observer] base so overrides stay terse. */
private open class PcObserver : PeerConnection.Observer {
    override fun onSignalingChange(newState: PeerConnection.SignalingState) {}
    override fun onIceConnectionChange(newState: PeerConnection.IceConnectionState) {}
    override fun onIceConnectionReceivingChange(receiving: Boolean) {}
    override fun onIceGatheringChange(newState: PeerConnection.IceGatheringState) {}
    override fun onIceCandidate(candidate: IceCandidate) {}
    override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>) {}
    override fun onAddStream(stream: org.webrtc.MediaStream) {}
    override fun onRemoveStream(stream: org.webrtc.MediaStream) {}
    override fun onDataChannel(dc: DataChannel) {}
    override fun onRenegotiationNeeded() {}
    override fun onConnectionChange(newState: PeerConnection.PeerConnectionState) {}
}
