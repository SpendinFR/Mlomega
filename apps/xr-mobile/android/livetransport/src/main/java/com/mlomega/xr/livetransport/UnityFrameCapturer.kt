package com.mlomega.xr.livetransport

import org.webrtc.CapturerObserver
import org.webrtc.JavaI420Buffer
import org.webrtc.SurfaceTextureHelper
import org.webrtc.TextureBufferImpl
import org.webrtc.VideoCapturer
import org.webrtc.VideoFrame
import org.webrtc.VideoSink
import android.content.Context
import org.webrtc.SurfaceTextureHelper.OnTextureFrameAvailableListener
import java.nio.ByteBuffer

/**
 * A custom libwebrtc [VideoCapturer] fed by Unity via a [VideoFrameFeeder].
 *
 * This is the GetStream/libwebrtc-documented way to inject external frames: a
 * `VideoCapturer` that owns a [SurfaceTextureHelper] and hands finished
 * [VideoFrame]s to the [CapturerObserver] libwebrtc gives it in [initialize].
 * From there the frames flow into the H.264 encoder like any camera frame. (ADR
 * docs/DECISIONS.md §E24 records why this path over a raw VideoSource.)
 *
 * Both feed paths from [VideoFrameSink] are bridged:
 *  - texture: wrapped into a [TextureBufferImpl] on the helper's GL thread,
 *  - I420 buffer: wrapped into a [JavaI420Buffer] (fallback, CPU copy).
 */
class UnityFrameCapturer(
    private val feeder: VideoFrameFeeder,
) : VideoCapturer, VideoFrameSink {

    private var observer: CapturerObserver? = null
    private var surfaceHelper: SurfaceTextureHelper? = null
    @Volatile private var running = false

    override fun initialize(
        surfaceTextureHelper: SurfaceTextureHelper,
        context: Context,
        capturerObserver: CapturerObserver,
    ) {
        this.surfaceHelper = surfaceTextureHelper
        this.observer = capturerObserver
    }

    override fun startCapture(width: Int, height: Int, framerate: Int) {
        if (running) return
        running = true
        observer?.onCapturerStarted(true)
        feeder.start(this)
    }

    override fun stopCapture() {
        if (!running) return
        running = false
        feeder.stop()
        observer?.onCapturerStopped()
    }

    override fun changeCaptureFormat(width: Int, height: Int, framerate: Int) {
        // Unity drives the actual capture size; format changes are advisory.
    }

    override fun dispose() {
        stopCapture()
        observer = null
        surfaceHelper = null
    }

    override fun isScreencast(): Boolean = false

    // --- VideoFrameSink (called by the Unity feeder) ---------------------------

    override fun onTextureFrame(
        oesTextureId: Int,
        transformMatrix: FloatArray,
        width: Int,
        height: Int,
        rotation: Int,
        timestampNs: Long,
    ) {
        if (!running) return
        val helper = surfaceHelper ?: return
        // TextureBufferImpl wraps the OES texture on the helper's GL context; the
        // toI420() path is provided by the helper's YUV converter when the encoder
        // needs software fallback.
        val buffer = TextureBufferImpl(
            width,
            height,
            VideoFrame.TextureBuffer.Type.OES,
            oesTextureId,
            org.webrtc.RendererCommon.convertMatrixToAndroidGraphicsMatrix(transformMatrix),
            helper.handler,
            helper.yuvConverter,
        ) { /* release callback: texture owned by Unity, nothing to free here */ }
        val frame = VideoFrame(buffer, rotation, timestampNs)
        observer?.onFrameCaptured(frame)
        frame.release()
    }

    override fun onI420Frame(
        data: ByteBuffer,
        width: Int,
        height: Int,
        rotation: Int,
        timestampNs: Long,
    ) {
        if (!running) return
        val chromaWidth = (width + 1) / 2
        val chromaHeight = (height + 1) / 2
        val ySize = width * height
        val uSize = chromaWidth * chromaHeight

        val buffer = JavaI420Buffer.allocate(width, height)
        // Copy Y, U, V planes out of the packed I420 input.
        data.position(0)
        copyPlane(data, buffer.dataY, ySize)
        copyPlane(data, buffer.dataU, uSize)
        copyPlane(data, buffer.dataV, uSize)

        val frame = VideoFrame(buffer, rotation, timestampNs)
        observer?.onFrameCaptured(frame)
        frame.release()
    }

    private fun copyPlane(src: ByteBuffer, dst: ByteBuffer, size: Int) {
        val tmp = ByteArray(size)
        src.get(tmp, 0, size)
        dst.put(tmp)
        dst.position(0)
    }
}
