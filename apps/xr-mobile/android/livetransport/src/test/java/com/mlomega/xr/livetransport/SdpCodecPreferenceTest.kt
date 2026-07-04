package com.mlomega.xr.livetransport

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure-JVM tests for [SdpCodecPreference] — runnable with `./gradlew test`
 * without an Android device (this is why the codec-preference logic is a plain
 * string transform, not a libwebrtc call).
 */
class SdpCodecPreferenceTest {

    private val sampleOffer = listOf(
        "v=0",
        "o=- 1 2 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "m=video 9 UDP/TLS/RTP/SAVPF 96 97 98 99 100",
        "a=rtpmap:96 VP8/90000",
        "a=rtpmap:97 rtx/90000",
        "a=rtpmap:98 VP9/90000",
        "a=rtpmap:100 H264/90000",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "a=rtpmap:111 opus/48000/2",
    ).joinToString("\r\n")

    @Test
    fun `H264 payload is hoisted to the front of m=video`() {
        val out = SdpCodecPreference.preferVideoCodec(sampleOffer, "H264", 1, "42e01f")
        val mLine = out.split("\r\n").first { it.startsWith("m=video ") }
        // Payload order after "m=video 9 UDP/TLS/RTP/SAVPF" must start with 100 (H264).
        val payloads = mLine.split(" ").drop(3)
        assertEquals("100", payloads.first())
        // All original payloads are still present.
        assertTrue(payloads.containsAll(listOf("96", "97", "98", "99", "100")))
    }

    @Test
    fun `H264 fmtp gets low-latency parameters`() {
        val out = SdpCodecPreference.preferVideoCodec(sampleOffer, "H264", 1, "42e01f")
        val fmtp = out.split("\r\n").first { it.startsWith("a=fmtp:100 ") }
        assertTrue(fmtp.contains("packetization-mode=1"))
        assertTrue(fmtp.contains("profile-level-id=42e01f"))
    }

    @Test
    fun `missing codec leaves sdp untouched`() {
        val out = SdpCodecPreference.preferVideoCodec(sampleOffer, "AV1", 1, "42e01f")
        assertEquals(sampleOffer, out)
    }

    @Test
    fun `opus gets 20ms ptime and dtx`() {
        val out = SdpCodecPreference.configureOpus(sampleOffer, 20, useDtx = true)
        val fmtp = out.split("\r\n").first { it.startsWith("a=fmtp:111 ") }
        assertTrue(fmtp.contains("minptime=20"))
        assertTrue(fmtp.contains("usedtx=1"))
        assertTrue(out.split("\r\n").any { it == "a=ptime:20" })
    }
}
