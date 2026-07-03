from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone

@dataclass
class DegradedState:
    active: bool = False
    reasons: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    def event(self) -> dict[str, object]:
        return {'type':'degraded_state','active':self.active,'reasons':self.reasons,'updated_at':self.updated_at}
