// MLOmega V19 — E26
// SubtitleSkill (§9.2/§14.4): turns AsrKws transcripts into Subtitle UIIntents.
// A partial refreshes the SAME ui_intent_id (rendered muted); a final replaces it
// (solid) and closes the line so the next turn expires it. It also mirrors the
// line into SceneCache.translation_hot, whose TTL enforces "expire au changement
// de tour ou délai" (§9.1). Runs with NO BrainLive and NO PC (handoff §3.2): the
// transcripts come straight from the on-device sherpa-onnx pipeline. Aggregated
// ReflexEvents count finalised segments, never one per partial.
using System.Collections.Generic;
using MLOmega.XR.Scene;
using UnityEngine;

namespace MLOmega.XR.Reflex.Skills
{
    public sealed class SubtitleSkill : ReflexSkillBase
    {
        [SerializeField] private LocalTrackStore _trackStore;

        public override ReflexSkillId SkillId => ReflexSkillId.Subtitle;

        // One live line per speaker; a new speaker or a final closes the previous.
        private string _currentIntentId;
        private int _lineSeq;

        protected override void Awake()
        {
            base.Awake();
            if (_trackStore == null) _trackStore = FindAnyObjectByType<LocalTrackStore>();
        }

        /// <summary>
        /// Handle a transcript from the AsrBridge. Partial → refresh muted line;
        /// final → solidify + close. `speakerTrackId` (optional) offsets the subtitle
        /// under a stable speaker.
        /// </summary>
        public void OnTranscript(string text, bool isFinal, string language, string speakerTrackId = null)
        {
            if (!IsActive || string.IsNullOrEmpty(text)) return;
            long now = NowMs();

            if (_currentIntentId == null)
            {
                _currentIntentId = "ul_sub_" + (_lineSeq++);
            }

            var intent = NewIntent("subtitle", _currentIntentId);
            intent.TruthLevel = "observed";
            if (!string.IsNullOrEmpty(speakerTrackId)) intent.TargetTrackId = speakerTrackId;
            intent.Content["text"] = text;
            intent.Content["language"] = language ?? "";
            intent.Content["final"] = isFinal;
            EmitIntent(intent);

            // Mirror into translation_hot (turn/delay TTL lives there).
            _trackStore?.SceneCache?.SubmitTranslation(speakerTrackId, text, isFinal, language);

            if (isFinal)
            {
                RecordReflex("subtitle",
                    new Dictionary<string, object> { { "language", language ?? "" }, { "chars", text.Length } },
                    0, 1.0, "info", now);
                // Close the line: the next partial opens a fresh id, so the current
                // one ages out (expiration au tour suivant).
                _currentIntentId = null;
            }
        }

        protected override void OnDeactivated() => _currentIntentId = null;
    }
}
