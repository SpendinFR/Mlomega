package com.mlomega.xr.livetransport

import java.nio.ByteBuffer

/**
 * Source of the video frames the transport encodes and sends. Unity's eye/phone
 * texture is fed here from `EyeCaptureSource.OnFrame` (E23) via the C# bridge.
 *
 * Two feed paths are supported, matching the two documented GetStream/libwebrtc
 * external-capture strategies (see ADR docs/DECISIONS.md §E24):
 *
 *  - **Texture (preferred, zero-copy):** Unity renders the eye frame into a
 *    [android.graphics.SurfaceTexture]-backed OES texture. The transport's
 *    [UnityTextureCapturer] wraps it into a `VideoFrame` via
 *    `SurfaceTextureHelper`, so the frame never leaves the GPU until the H.264
 *    encoder consumes it. This is the recommended path for a live camera feed.
 *
 *  - **ByteBuffer (fallback):** Unity reads the frame back to an I420/NV21
 *    buffer (e.g. AsyncGPUReadback) and pushes it here. Simpler to wire but
 *    incurs a GPU->CPU copy per frame; used when the texture path is unavailable
 *    (e.g. capture-only phone modes without a shared OES texture).
 *
 * Implementations obtain frames however they like; the transport pulls/receives
 * them and never blocks the Unity thread.
 */
interface VideoFrameFeeder {

    /** Nominal capture size, used to configure the capturer format. */
    val width: Int
    val height: Int
    val fps: Int

    /**
     * Called by the transport when video capture starts. The feeder should begin
     * delivering frames (texture or buffer) to [sink] until [stop] is called.
     */
    fun start(sink: VideoFrameSink)

    /** Called by the transport when capture stops (disconnect/teardown). */
    fun stop()

    /** True if this feeder delivers OES-texture frames (zero-copy path). */
    val isTextureBacked: Boolean
}

/**
 * The transport-side receiver a [VideoFrameFeeder] pushes frames into. Exactly
 * one of the two methods is used per feeder, matching [VideoFrameFeeder.isTextureBacked].
 */
interface VideoFrameSink {

    /**
     * Push a raw OES texture id already rendered by Unity. [transformMatrix] is
     * the 4x4 SurfaceTexture transform; [timestampNs] is the capture monotonic
     * timestamp (same clock as FrameEnvelope.capture_monotonic_ns). [rotation]
     * is 0/90/180/270 (capture-only OrientationGuard).
     */
    fun onTextureFrame(
        oesTextureId: Int,
        transformMatrix: FloatArray,
        width: Int,
        height: Int,
        rotation: Int,
        timestampNs: Long,
    )

    /**
     * Push an I420 planar frame from a CPU buffer (fallback path). The buffer is
     * copied by the transport before this returns, so the caller may reuse it.
     */
    fun onI420Frame(
        data: ByteBuffer,
        width: Int,
        height: Int,
        rotation: Int,
        timestampNs: Long,
    )
}
