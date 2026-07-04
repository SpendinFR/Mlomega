package com.mlomega.xr.livetransport

/**
 * Rewrites a local SDP offer so a preferred video codec (H.264) is listed first
 * on the `m=video` line, and stamps low-latency H.264 fmtp parameters.
 *
 * libwebrtc offers VP8/VP9/H264 with VP8 first by default. Peers negotiate the
 * first mutually-supported codec, so to force H.264 (NVDEC-friendly on the PC,
 * hardware-encoded on the S25) we reorder the payload-type list. We also inject
 * `packetization-mode` / `profile-level-id` into the H.264 fmtp line so the
 * encoder runs constrained-baseline with single-NAL packetization — the widest
 * low-latency configuration.
 *
 * Pure string transform (no libwebrtc types) so it is unit-testable off-device.
 */
object SdpCodecPreference {

    /**
     * Reorder the m=video payloads to put [preferredCodec] (case-insensitive,
     * e.g. "H264") first, and ensure its fmtp carries the given
     * [packetizationMode] / [profileLevelId]. Returns the rewritten SDP.
     */
    fun preferVideoCodec(
        sdp: String,
        preferredCodec: String,
        packetizationMode: Int,
        profileLevelId: String,
    ): String {
        val eol = if (sdp.contains("\r\n")) "\r\n" else "\n"
        val lines = sdp.split(Regex("\r\n|\n")).toMutableList()

        val mLineIndex = lines.indexOfFirst { it.startsWith("m=video ") }
        if (mLineIndex < 0) return sdp

        // Collect payload types whose rtpmap names match the preferred codec.
        val codecUpper = preferredCodec.uppercase()
        val preferredPts = ArrayList<String>()
        val rtpmap = Regex("^a=rtpmap:(\\d+) ([A-Za-z0-9._-]+)/\\d+.*$")
        for (line in lines) {
            val m = rtpmap.find(line) ?: continue
            val pt = m.groupValues[1]
            val name = m.groupValues[2].uppercase()
            if (name == codecUpper) preferredPts.add(pt)
        }
        if (preferredPts.isEmpty()) return sdp // codec not offered; leave as-is

        // Rebuild the m=video line: "m=video <port> <proto> <pt...>" with the
        // preferred payload types hoisted to the front, order otherwise preserved.
        val mParts = lines[mLineIndex].split(" ").toMutableList()
        val header = mParts.subList(0, 3) // m=video, port, proto
        val payloads = mParts.subList(3, mParts.size)
        val reordered = ArrayList<String>(preferredPts)
        for (pt in payloads) if (pt !in preferredPts) reordered.add(pt)
        lines[mLineIndex] = (header + reordered).joinToString(" ")

        // Ensure each preferred payload type has an fmtp line with the low-latency
        // parameters. Add or amend as needed.
        for (pt in preferredPts) {
            val fmtpIndex = lines.indexOfFirst { it.startsWith("a=fmtp:$pt ") }
            val params =
                "level-asymmetry-allowed=1;packetization-mode=$packetizationMode;" +
                    "profile-level-id=$profileLevelId"
            if (fmtpIndex >= 0) {
                if (!lines[fmtpIndex].contains("profile-level-id")) {
                    lines[fmtpIndex] = lines[fmtpIndex].trimEnd(';') + ";" + params
                }
            } else {
                // Insert the fmtp right after this payload's rtpmap line.
                val rtpmapIndex = lines.indexOfFirst { it.startsWith("a=rtpmap:$pt ") }
                if (rtpmapIndex >= 0) {
                    lines.add(rtpmapIndex + 1, "a=fmtp:$pt $params")
                }
            }
        }

        return lines.joinToString(eol)
    }

    /**
     * Set the Opus ptime and DTX on the m=audio fmtp so the mic runs 20 ms frames
     * with discontinuous transmission (voice-optimised, low bitrate on silence).
     */
    fun configureOpus(sdp: String, ptimeMs: Int, useDtx: Boolean): String {
        val eol = if (sdp.contains("\r\n")) "\r\n" else "\n"
        val lines = sdp.split(Regex("\r\n|\n")).toMutableList()

        val opusPt = Regex("^a=rtpmap:(\\d+) opus/\\d+.*$", RegexOption.IGNORE_CASE)
            .let { rx -> lines.firstNotNullOfOrNull { rx.find(it)?.groupValues?.get(1) } }
            ?: return sdp

        val fmtpIndex = lines.indexOfFirst { it.startsWith("a=fmtp:$opusPt ") }
        val extra = "minptime=$ptimeMs;useinbandfec=1" + if (useDtx) ";usedtx=1" else ""
        if (fmtpIndex >= 0) {
            if (!lines[fmtpIndex].contains("minptime")) {
                lines[fmtpIndex] = lines[fmtpIndex].trimEnd(';') + ";" + extra
            }
        } else {
            val rtpmapIndex = lines.indexOfFirst { it.startsWith("a=rtpmap:$opusPt ") }
            if (rtpmapIndex >= 0) lines.add(rtpmapIndex + 1, "a=fmtp:$opusPt $extra")
        }
        // Explicit a=ptime as a hint for peers that honour it.
        val mAudio = lines.indexOfFirst { it.startsWith("m=audio ") }
        if (mAudio >= 0 && lines.none { it == "a=ptime:$ptimeMs" }) {
            lines.add(mAudio + 1, "a=ptime:$ptimeMs")
        }
        return lines.joinToString(eol)
    }
}
