package com.mlomega.xr.reflexvision

/**
 * Encodes a human-readable wake phrase into the sherpa-onnx KeywordSpotter
 * "keywords file" format, so the wake word is fully configurable
 * (MLOmegaConfig → here) with no recompile.
 *
 * sherpa-onnx expects one keyword per line, expressed as space-separated BPE
 * tokens, optionally followed by a per-keyword boost (`:score`) and threshold
 * (`#threshold`). The transducer KWS models ship with a `bpe.model`; the phrase
 * is tokenised with the sherpa-onnx text2token tooling at model-prep time. To
 * keep the *device* path dependency-free and deterministic, this encoder accepts
 * the phrase already split into model tokens (the README documents generating
 * them once with `sherpa-onnx-cli text2token`), or falls back to a
 * whitespace/▁-prefixed word split that works for the whole-word keyword models.
 *
 * Pure and JVM-testable ([KeywordEncoderTest]); no Android / sherpa types here.
 */
object KeywordEncoder {

    /**
     * Build the full keywords-file contents for one or more wake phrases.
     *
     * @param phrases wake phrases, e.g. ["hey mlomega"].
     * @param boost per-keyword score boost applied to every line (`:score`); null
     *   to omit and use the spotter's global keywordsScore.
     * @param threshold per-keyword detection threshold (`#threshold`); null to
     *   omit and use the global keywordsThreshold.
     */
    fun encode(phrases: List<String>, boost: Float? = null, threshold: Float? = null): String {
        val sb = StringBuilder()
        for (phrase in phrases) {
            val tokens = tokenize(phrase)
            if (tokens.isEmpty()) continue
            sb.append(tokens.joinToString(" "))
            if (boost != null) sb.append(" :").append(trimNumber(boost))
            if (threshold != null) sb.append(" #").append(trimNumber(threshold))
            sb.append('\n')
        }
        return sb.toString()
    }

    /**
     * Tokenise a phrase to the whole-word/BPE-ish form the wordpiece KWS models
     * accept: lowercased words, each prefixed with the sentencepiece word-start
     * marker "▁". Multi-word phrases become multiple ▁-prefixed tokens. This is
     * the deterministic device-side fallback; a pre-tokenised phrase (already
     * containing spaces between model tokens) is passed through unchanged.
     */
    fun tokenize(phrase: String): List<String> {
        val trimmed = phrase.trim()
        if (trimmed.isEmpty()) return emptyList()
        // If the caller already provided model tokens (contains the ▁ marker),
        // respect their tokenisation verbatim.
        if (trimmed.contains(WORD_START)) {
            return trimmed.split(Regex("\\s+")).filter { it.isNotEmpty() }
        }
        return trimmed
            .lowercase()
            .split(Regex("\\s+"))
            .filter { it.isNotEmpty() }
            .map { WORD_START + it }
    }

    private fun trimNumber(v: Float): String {
        // Compact, locale-independent number rendering (no trailing zeros).
        val s = v.toString()
        return if (s.endsWith(".0")) s.dropLast(2) else s
    }

    const val WORD_START = "▁" // "▁" sentencepiece word-start marker
}
