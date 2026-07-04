package com.mlomega.xr.reflexvision

import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.KeywordSpotter
import com.k2fsa.sherpa.onnx.KeywordSpotterConfig
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineRecognizer
import com.k2fsa.sherpa.onnx.OnlineRecognizerConfig
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig
import com.k2fsa.sherpa.onnx.Vad
import com.k2fsa.sherpa.onnx.VadModelConfig
import com.k2fsa.sherpa.onnx.getFeatureConfig
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * MLOmega V19 on-device speech pipeline (E26).
 *
 * A single 16 kHz mono [AudioRecord] feeds three sherpa-onnx calculators in
 * parallel (handoff §3.2 — **NO LLM / NO VLM**, all small on-device models,
 * < 100 ms):
 *   * a Silero [Vad] gates the ASR so decoding runs only inside speech;
 *   * a streaming zipformer [OnlineRecognizer] (FR *or* EN, chosen by config)
 *     emits partial + final transcripts with timestamps → SubtitleSkill;
 *   * a [KeywordSpotter] watches for the configurable wake word → WakeWordGate.
 *
 * **On-demand (GUIDE_V19_REFERENCE §9.4):** the models + mic are created on
 * [start] and released on [stop]; the Unity ReflexScheduler / WakeWordGate call
 * these so the mic path is not resident when unused. A [MicForegroundService]
 * holds the background-mic slot with a privacy-visible notification.
 *
 * Model files are loaded from app storage (paths in [AsrKwsConfig]); weights are
 * never committed (download URLs + install layout in the README).
 *
 * This module cannot be compiled in the authoring environment (no Android SDK);
 * it is written against the pinned sherpa-onnx Android API (see build.gradle.kts)
 * and the real compile/run belongs to the S25 validation gate
 * (ADR docs/DECISIONS §E26).
 */
class AsrKwsService(
    private val appContext: Context,
    private val config: AsrKwsConfig,
    private val callbacks: AsrKwsCallbacks,
) {
    private val running = AtomicBoolean(false)

    @Volatile private var recognizer: OnlineRecognizer? = null
    @Volatile private var vad: Vad? = null
    @Volatile private var spotter: KeywordSpotter? = null
    @Volatile private var audioRecord: AudioRecord? = null
    @Volatile private var worker: Thread? = null

    /** Whether the wake word is currently armed (KWS enabled). */
    private val kwsArmed = AtomicBoolean(true)

    /**
     * Start capture + all calculators. No-op if already running. Requires that
     * RECORD_AUDIO has been granted (the Unity PermissionGate handles this).
     */
    fun start() {
        if (!running.compareAndSet(false, true)) return
        try {
            MicForegroundService.start(appContext)
            buildModels()
            startAudioLoop()
        } catch (t: Throwable) {
            running.set(false)
            release()
            callbacks.onError("asr start failed: ${t.message}")
        }
    }

    /** Stop capture, release models + mic, stop the foreground service. */
    fun stop() {
        if (!running.compareAndSet(true, false)) return
        try {
            worker?.join(500)
        } catch (_: InterruptedException) {
        }
        worker = null
        release()
        MicForegroundService.stop(appContext)
    }

    fun isRunning(): Boolean = running.get()

    /** Arm/disarm the wake-word spotter without tearing down ASR (e.g. while a command is being taken). */
    fun setWakeWordArmed(armed: Boolean) = kwsArmed.set(armed)

    // ----------------------------------------------------------------------
    //  Model construction
    // ----------------------------------------------------------------------

    private fun buildModels() {
        val feat: FeatureConfig = getFeatureConfig(sampleRate = config.sampleRate, featureDim = 80)

        val transducer = OnlineTransducerModelConfig(
            encoder = File(config.asrModelDir, "encoder.onnx").absolutePath,
            decoder = File(config.asrModelDir, "decoder.onnx").absolutePath,
            joiner = File(config.asrModelDir, "joiner.onnx").absolutePath,
        )
        val modelConfig = OnlineModelConfig(
            transducer = transducer,
            tokens = File(config.asrModelDir, "tokens.txt").absolutePath,
            numThreads = config.numThreads,
            provider = config.provider,
        )
        val recognizerConfig = OnlineRecognizerConfig(
            featConfig = feat,
            modelConfig = modelConfig,
            enableEndpoint = true,
        )
        recognizer = OnlineRecognizer(config = recognizerConfig)

        val vadConfig = VadModelConfig(
            sileroVadModelConfig = com.k2fsa.sherpa.onnx.SileroVadModelConfig(
                model = config.vadModelPath,
                threshold = config.vad.threshold,
                minSilenceDuration = config.vad.minSilenceDurationSec,
                minSpeechDuration = config.vad.minSpeechDurationSec,
            ),
            sampleRate = config.sampleRate,
            numThreads = config.numThreads,
            provider = config.provider,
        )
        vad = Vad(config = vadConfig)

        // KeywordSpotter: streaming zipformer transducer over the encoded wake word.
        val keywordsFile = writeKeywordsFile()
        val kwsModel = OnlineModelConfig(
            transducer = OnlineTransducerModelConfig(
                encoder = File(config.kwsModelDir, "encoder.onnx").absolutePath,
                decoder = File(config.kwsModelDir, "decoder.onnx").absolutePath,
                joiner = File(config.kwsModelDir, "joiner.onnx").absolutePath,
            ),
            tokens = File(config.kwsModelDir, "tokens.txt").absolutePath,
            numThreads = config.numThreads,
            provider = config.provider,
        )
        val kwsConfig = KeywordSpotterConfig(
            featConfig = feat,
            modelConfig = kwsModel,
            keywordsFile = keywordsFile.absolutePath,
            keywordsScore = config.kws.keywordsScore,
            keywordsThreshold = config.kws.keywordsThreshold,
            maxActivePaths = config.kws.maxActivePaths,
            numTrailingBlanks = config.kws.numTrailingBlanks,
        )
        spotter = KeywordSpotter(config = kwsConfig)
    }

    /**
     * Encode the configured wake phrase(s) into a sherpa keywords file on app
     * storage. Regenerated each start so a config change takes effect without any
     * committed asset.
     */
    private fun writeKeywordsFile(): File {
        val contents = KeywordEncoder.encode(
            phrases = config.wakeWords,
            boost = config.kws.keywordsScore,
            threshold = config.kws.keywordsThreshold,
        )
        val file = File(appContext.filesDir, "reflex_wake_keywords.txt")
        file.writeText(contents)
        return file
    }

    // ----------------------------------------------------------------------
    //  Audio loop: AudioRecord 16 kHz mono -> VAD-gated ASR + KWS
    // ----------------------------------------------------------------------

    private fun startAudioLoop() {
        val minBuf = AudioRecord.getMinBufferSize(
            config.sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        val bufferSize = maxOf(minBuf, config.sampleRate / 5 * 2) // ~200 ms floor
        val record = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            config.sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            bufferSize,
        )
        audioRecord = record

        val chunk = ShortArray(config.sampleRate / 10) // 100 ms chunks
        val kwsStream = spotter!!.createStream()

        worker = thread(name = "mlomega-asr-kws", isDaemon = true) {
            record.startRecording()
            var segmentStartMs = -1L
            while (running.get()) {
                val n = record.read(chunk, 0, chunk.size)
                if (n <= 0) continue
                val samples = FloatArray(n) { chunk[it] / 32768.0f }
                val nowMs = System.currentTimeMillis()

                // --- wake word ---
                if (kwsArmed.get()) {
                    val s = spotter ?: break
                    kwsStream.acceptWaveform(samples, sampleRate = config.sampleRate)
                    while (s.isReady(kwsStream)) {
                        s.decode(kwsStream)
                        val kw = s.getResult(kwsStream).keyword
                        if (kw.isNotEmpty()) {
                            s.reset(kwsStream)
                            callbacks.onWakeWord(kw, nowMs)
                        }
                    }
                }

                // --- VAD-gated ASR ---
                val v = vad ?: break
                v.acceptWaveform(samples)
                if (v.isSpeechDetected() && segmentStartMs < 0L) segmentStartMs = nowMs
                while (!v.empty()) {
                    val segment = v.front()
                    decodeSegment(segment.samples, segmentStartMs, nowMs)
                    v.pop()
                    segmentStartMs = -1L
                }
            }
            try { record.stop() } catch (_: Throwable) {}
        }
    }

    private fun decodeSegment(samples: FloatArray, startMs: Long, endMs: Long) {
        val r = recognizer ?: return
        val stream = r.createStream()
        try {
            stream.acceptWaveform(samples, sampleRate = config.sampleRate)
            while (r.isReady(stream)) {
                r.decode(stream)
                val partial = r.getResult(stream).text
                if (partial.isNotBlank()) {
                    callbacks.onTranscript(partial, false, langCode(), startMs, endMs)
                }
            }
            stream.inputFinished()
            while (r.isReady(stream)) {
                r.decode(stream)
            }
            val finalText = r.getResult(stream).text
            if (finalText.isNotBlank()) {
                callbacks.onTranscript(finalText, true, langCode(), startMs, endMs)
            }
        } finally {
            stream.release()
        }
    }

    private fun langCode(): String = if (config.language == AsrLanguage.FR) "fr" else "en"

    private fun release() {
        try { audioRecord?.release() } catch (_: Throwable) {}
        audioRecord = null
        try { recognizer?.release() } catch (_: Throwable) {}
        recognizer = null
        try { spotter?.release() } catch (_: Throwable) {}
        spotter = null
        try { vad?.release() } catch (_: Throwable) {}
        vad = null
    }
}
