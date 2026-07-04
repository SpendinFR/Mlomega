package com.mlomega.xr.livetransport

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Thin OkHttp client for the unified V19 signaling endpoint
 * `POST /webrtc/offer` served by `services/live-pc/sessionhub_http.py`.
 *
 * Request body: `{sdp, type, session_id, token}` (token-gated — the PC returns
 * HTTP 401 if the token does not match the session). Response: `{sdp, type}`
 * (the SDP answer). Blocking call — invoked from the transport's own coroutine,
 * never the Unity main thread.
 */
class SignalingClient(
    private val url: String,
    private val sessionId: String,
    private val token: String,
    timeoutMs: Long,
) {
    private val http = OkHttpClient.Builder()
        .callTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .connectTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .readTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .build()

    /** Result of a successful offer/answer exchange. */
    data class Answer(val sdp: String, val type: String)

    /**
     * POST the local [offerSdp] and return the remote SDP answer.
     *
     * @throws IOException on transport error or a non-2xx / 401 response.
     */
    fun exchangeOffer(offerSdp: String, offerType: String): Answer {
        val body = JSONObject()
            .put("sdp", offerSdp)
            .put("type", offerType)
            .put("session_id", sessionId)
            .put("token", token)
            .toString()
            .toRequestBody(JSON)

        val request = Request.Builder()
            .url(url)
            .post(body)
            .header("Content-Type", "application/json")
            .build()

        http.newCall(request).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) {
                throw IOException("signaling ${resp.code}: ${text.take(200)}")
            }
            val json = JSONObject(text)
            val sdp = json.optString("sdp")
            val type = json.optString("type")
            if (sdp.isEmpty() || type.isEmpty()) {
                throw IOException("signaling answer missing sdp/type: ${text.take(200)}")
            }
            return Answer(sdp, type)
        }
    }

    private companion object {
        val JSON = "application/json; charset=utf-8".toMediaType()
    }
}
