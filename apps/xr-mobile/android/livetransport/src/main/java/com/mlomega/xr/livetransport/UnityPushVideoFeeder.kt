package com.mlomega.xr.livetransport

import java.nio.ByteBuffer

/**
 * A [VideoFrameFeeder] whose frames are *pushed* from Unity (C#) rather than
 * pulled. The C# [LiveTransportBridge] subscribes to `EyeCaptureSource.OnFrame`
 * (E23) and calls [pushTextureFrame] / [pushI420Frame] from the render thread;
 * this feeder relays them to the transport's [VideoFrameSink].
 *
 * This is the JNI-friendly shape for Unity: a plain object with push methods the
 * C# side invokes via `AndroidJavaObject.Call(...)`. The transport owns the
 * capturer thread; pushes are cheap hand-offs.
 *
 * @param width nominal capture width.
 * @param height nominal capture height.
 * @param fps nominal capture fps.
 * @param textureBacked true when Unity pushes OES texture ids (zero-copy path).
 */
class UnityPushVideoFeeder(
    override val width: Int,
    override val height: Int,
    override val fps: Int,
    override val isTextureBacked: Boolean,
) : VideoFrameFeeder {

    @Volatile private var sink: VideoFrameSink? = null

    override fun start(sink: VideoFrameSink) {
        this.sink = sink
    }

    override fun stop() {
        this.sink = null
    }

    /**
     * Push an OES texture frame (called from Unity's render thread). No-op if the
     * transport is not currently capturing.
     */
    fun pushTextureFrame(
        oesTextureId: Int,
        transformMatrix: FloatArray,
        width: Int,
        height: Int,
        rotation: Int,
        timestampNs: Long,
    ) {
        sink?.onTextureFrame(oesTextureId, transformMatrix, width, height, rotation, timestampNs)
    }

    /**
     * Push an I420 CPU buffer frame (fallback path). The [data] buffer is copied
     * by the transport before the call returns.
     */
    fun pushI420Frame(
        data: ByteBuffer,
        width: Int,
        height: Int,
        rotation: Int,
        timestampNs: Long,
    ) {
        sink?.onI420Frame(data, width, height, rotation, timestampNs)
    }
}
