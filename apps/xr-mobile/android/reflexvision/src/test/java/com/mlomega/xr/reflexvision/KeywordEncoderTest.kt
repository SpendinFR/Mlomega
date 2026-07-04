package com.mlomega.xr.reflexvision

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Device-free tests of the wake-word keyword encoding (configurable wake word). */
class KeywordEncoderTest {

    @Test
    fun single_phrase_is_tokenized_with_word_start_markers() {
        val out = KeywordEncoder.encode(listOf("hey mlomega"))
        // Two ▁-prefixed word tokens, one line.
        assertEquals("▁hey ▁mlomega\n", out)
    }

    @Test
    fun boost_and_threshold_are_appended() {
        val out = KeywordEncoder.encode(listOf("bonjour"), boost = 1.5f, threshold = 0.25f)
        assertEquals("▁bonjour :1.5 #0.25\n", out)
    }

    @Test
    fun pretokenized_phrase_is_respected_verbatim() {
        // Already contains model tokens (▁): pass through unchanged, no re-lowercasing.
        val tokens = KeywordEncoder.tokenize("▁HEY ▁M L O")
        assertEquals(listOf("▁HEY", "▁M", "L", "O"), tokens)
    }

    @Test
    fun multiple_phrases_produce_multiple_lines() {
        val out = KeywordEncoder.encode(listOf("hey mlomega", "salut"))
        val lines = out.trim().split("\n")
        assertEquals(2, lines.size)
        assertTrue(lines[0].startsWith("▁hey"))
        assertTrue(lines[1].startsWith("▁salut"))
    }

    @Test
    fun empty_phrase_is_skipped() {
        val out = KeywordEncoder.encode(listOf("", "  "))
        assertEquals("", out)
    }
}
