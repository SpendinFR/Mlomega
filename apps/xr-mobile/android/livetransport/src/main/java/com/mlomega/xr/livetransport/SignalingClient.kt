package com.mlomega.xr.livetransport

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * E36 §1 — one PC endpoint the client may reach the SessionHub at. Outside the
 * home the phone is on 4G/5G and the PC sits behind NAT, so a single LAN base URL
 * no longer works. [SignalingClient] takes an ORDERED list of endpoints (LAN
 * first, then a VPN-tunnel address such as Tailscale 100.x), probes each `/health`
 * in order, and uses the first that answers — failing over automatically.
 */
data class PcEndpoint(
    val name: String,
    val baseUrl: String,
) {
    val healthUrl: String get() = "$baseUrl/health"
    val webrtcOfferUrl: String get() = "$baseUrl/webrtc/offer"
}

/**
 * Thin OkHttp client for the unified V19 signaling endpoint
 * `POST /webrtc/offer` served by `services/live-pc/sessionhub_http.py`.
 *
 * Request body: `{sdp, type, session_id, token}` (token-gated — the PC returns
 * HTTP 401 if the token does not match the session). Response: `{sdp, type}`
 * (the SDP answer). Blocking call — invoked from the transport's own coroutine,
 * never the Unity main thread.
 *
 * Two constructors: a single-URL one (backward compatible) and one taking an
 * ORDERED [endpoints] list for E36 outside-access failover.
 */
class SignalingClient private constructor(
    private val endpoints: List<PcEndpoint>,
    private val sessionId: String,
    private val token: String,
    timeoutMs: Long,
) {
    /** Single-URL constructor (backward compatible with the E24/E34 call site). */
    constructor(url: String, sessionId: String, token: String, timeoutMs: Long) :
        this(listOf(PcEndpoint("lan", url.removeSuffix("/webrtc/offer"))), sessionId, token, timeoutMs)

    /** The endpoint the last successful exchange resolved to (LAN or tunnel), or null. */
    var activeEndpoint: PcEndpoint? = null
        private set
    private val http = OkHttpClient.Builder()
        .callTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .connectTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .readTimeout(timeoutMs, TimeUnit.MILLISECONDS)
        .build()

    /** Result of a successful offer/answer exchange. */
    data class Answer(val sdp: String, val type: String)

    /**
     * E36 §1 — resolve the first reachable endpoint by probing `/health` IN ORDER
     * (LAN first, then tunnel). Always starts from the top so a return to the LAN
     * reclaims the preferred endpoint. Returns null when the PC is unreachable (the
     * caller then stays in the device-only reflex mode; no exception).
     */
    fun resolveEndpoint(): PcEndpoint? {
        for (ep in endpoints) {
            val request = Request.Builder().url(ep.healthUrl).get().build()
            try {
                http.newCall(request).execute().use { resp ->
                    if (resp.isSuccessful) {
                        activeEndpoint = ep
                        return ep
                    }
                }
            } catch (_: IOException) {
                // try the next endpoint
            }
        }
        activeEndpoint = null
        return null
    }

    /**
     * POST the local [offerSdp] and return the remote SDP answer.
     *
     * E36 §1: resolves the active endpoint first (LAN → tunnel failover) then POSTs
     * to its `/webrtc/offer`. If the chosen endpoint's offer fails, it falls over to
     * the next reachable endpoint before giving up.
     *
     * @throws IOException when NO endpoint accepts the offer (PC unreachable).
     */
    fun exchangeOffer(offerSdp: String, offerType: String): Answer {
        val body = JSONObject()
            .put("sdp", offerSdp)
            .put("type", offerType)
            .put("session_id", sessionId)
            .put("token", token)
            .toString()

        // Order the endpoints so the freshly resolved one is tried first, then the
        // rest (failover) — but the list order (LAN first) is preserved otherwise.
        resolveEndpoint()
        val ordered = buildList {
            activeEndpoint?.let { add(it) }
            endpoints.forEach { if (it != activeEndpoint) add(it) }
        }

        var lastError: IOException? = null
        for (ep in ordered) {
            val request = Request.Builder()
                .url(ep.webrtcOfferUrl)
                .post(body.toRequestBody(JSON))
                .header("Content-Type", "application/json")
                .build()
            try {
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
                    activeEndpoint = ep
                    return Answer(sdp, type)
                }
            } catch (e: IOException) {
                lastError = e  // try the next endpoint (failover)
            }
        }
        activeEndpoint = null
        throw lastError ?: IOException("signaling: no PC endpoint reachable")
    }

    private companion object {
        val JSON = "application/json; charset=utf-8".toMediaType()
    }
}
