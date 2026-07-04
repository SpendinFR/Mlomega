package com.mlomega.xr.reflexvision

/**
 * Language selector for the streaming ASR. sherpa-onnx ships separate
 * mono-lingual streaming zipformer models for FR and EN (there is no confirmed
 * bilingual FR/EN streaming model — handoff §4), so the language is a config
 * choice that picks the model directory.
 */
enum class AsrLanguage { FR, EN }

/**
 * All tunables + model locations for [AsrKwsService]. Model files are NOT
 * committed: they are downloaded to app storage at first run (URLs + install
 * paths in the README) and their absolute directories are passed here.
 *
 * @property language Which streaming model to load (FR or EN).
 * @property asrModelDir Absolute dir of the streaming zipformer model
 *   (`encoder.onnx` / `decoder.onnx` / `joiner.onnx` / `tokens.txt`).
 * @property vadModelPath Absolute path to the Silero VAD onnx used to gate ASR.
 * @property kwsModelDir Absolute dir of the KeywordSpotter model
 *   (`encoder/decoder/joiner` + `tokens.txt`, streaming zipformer KWS).
 * @property wakeWords Human-readable wake phrase(s) (e.g. "hey mlomega"). Encoded
 *   to the sherpa keywords format (token ids per line) by [KeywordEncoder] and
 *   written to a keywords file the KeywordSpotter loads.
 * @property sampleRate AudioRecord sample rate (sherpa streaming models are 16k).
 * @property numThreads onnxruntime intra-op threads (2 keeps latency low on S25).
 * @property provider onnxruntime execution provider ("cpu" is the safe default;
 *   "nnapi" can be enabled once validated on device).
 */
data class AsrKwsConfig(
    val language: AsrLanguage,
    val asrModelDir: String,
    val vadModelPath: String,
    val kwsModelDir: String,
    val wakeWords: List<String>,
    val sampleRate: Int = 16_000,
    val numThreads: Int = 2,
    val provider: String = "cpu",
    val vad: VadConfig = VadConfig(),
    val kws: KwsConfig = KwsConfig(),
)

/**
 * Silero VAD gating parameters. ASR decoding only runs inside detected speech,
 * which is what keeps this path cheap (handoff §3.2, < 100 ms) — silence costs
 * nothing.
 */
data class VadConfig(
    val threshold: Float = 0.5f,
    val minSilenceDurationSec: Float = 0.25f,
    val minSpeechDurationSec: Float = 0.10f,
    /** Ring-buffer window the VAD scans, seconds. */
    val bufferSizeSec: Float = 30f,
)

/**
 * KeywordSpotter tuning. [keywordsScore] boosts the wake-word tokens; [threshold]
 * is the detection floor. Both are the sherpa-onnx knobs that trade off wake-word
 * recall vs false triggers; they live here so the wake word can be tuned without
 * recompiling.
 */
data class KwsConfig(
    val keywordsScore: Float = 1.5f,
    val keywordsThreshold: Float = 0.25f,
    val maxActivePaths: Int = 4,
    val numTrailingBlanks: Int = 1,
)
