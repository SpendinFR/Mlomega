// MLOmega V19 — E34 §5
// EntityHotUpdate: a device-bound DataChannel message the PC scene adapter pushes
// the moment identity_fusion names a person. It carries a compact relation pack
// (last topics / open promises from the core relationship tables) so the device
// SceneCache.entities_hot can render the ContextCard from the local cache with no
// round-trip. Not a formal schema contract (live-only), so it lives in the Scene
// assembly next to SceneCache rather than the generated Contracts copy.
using System.Collections.Generic;
using Newtonsoft.Json;

namespace MLOmega.XR.Scene
{
    /// <summary>PC-&gt;device prefetch of a person's relation pack (E34 §5).</summary>
    public sealed class EntityHotUpdate
    {
        [JsonProperty("type")] public string Type { get; set; }
        [JsonProperty("entity_id")] public string EntityId { get; set; }
        [JsonProperty("person_id")] public string PersonId { get; set; }
        [JsonProperty("name")] public string Name { get; set; }
        [JsonProperty("relation_pack")] public List<Dictionary<string, object>> RelationPack { get; set; }
        [JsonProperty("as_of")] public string AsOf { get; set; }

        /// <summary>True when a raw DataChannel message is an entity_hot_update.</summary>
        public static bool IsEntityHotUpdate(string json)
        {
            if (string.IsNullOrEmpty(json)) return false;
            return json.IndexOf("\"entity_hot_update\"", System.StringComparison.Ordinal) >= 0;
        }
    }
}
