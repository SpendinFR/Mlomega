# V3.2.3 — Global Discourse Context

V3.2.2 made audio memory precise at the utterance/word level. V3.2.3 adds the global conversation layer needed for long audio:

- detect whether the whole conversation keeps the same subject;
- detect topic threads that open, pause, resume, shift, or resolve;
- detect callbacks: a phrase near the end that answers, confirms, contradicts, or reframes a phrase from the beginning;
- attach every utterance to one or more active topic threads;
- pass the global discourse context into the per-utterance microscope.

The pipeline is now:

```text
audio
→ WhisperX word timestamps
→ pyannote speaker diarization
→ atomic utterance segmentation
→ global discourse map of the full conversation
→ per-utterance microscope with local + global context
→ memory cards / frames / facets / discourse links / callbacks
→ vector DB + Graphiti + Mem0
```

New DB layers:

- `conversation_discourse_maps`
- `conversation_topic_threads`
- `utterance_discourse_links`
- `conversation_callbacks`

This is strict. If the LLM cannot produce one discourse context for every utterance, ingestion fails.
