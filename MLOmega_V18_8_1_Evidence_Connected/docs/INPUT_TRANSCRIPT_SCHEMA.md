# Format d'entrée conversation

```json
{
  "metadata": {
    "conversation_id": "conv_unique",
    "started_at": "2026-06-02T18:00:00+02:00",
    "ended_at": "2026-06-02T19:00:00+02:00",
    "topic": "MemoryLight Omega",
    "channel": "audio/transcript",
    "participants": ["me", "Sarah"],
    "speaker_map": {"SPEAKER_00": "me", "SPEAKER_01": "Sarah"},
    "relationship_context": {"Sarah": "amie / discussion projet"}
  },
  "turns": [
    {"speaker": "SPEAKER_00", "start": 0.0, "end": 4.2, "text": "..."}
  ]
}
```

## Important

- Si `speaker_map` est absent, le système garde `SPEAKER_00` comme personne provisoire.
- Pour reconnaître les voix, utiliser `enroll-voice` puis adapter l'étape WhisperX/pyannote/SpeechBrain.
- La relation personne+sujet+canal est centrale : tu ne parles pas pareil à tout le monde.
