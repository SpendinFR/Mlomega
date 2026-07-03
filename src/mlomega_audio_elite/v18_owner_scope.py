"""Release-default owner-scope guard for legacy compatibility code.

V18 keeps legacy modules importable, but a release build must never choose a
person implicitly.  The single escape hatch is intentionally opt-in and makes
the release audit fail, so it cannot silently become a production default.
"""
from __future__ import annotations

import os

from .governance_v18 import ScopeError


_LEGACY_IMPLICIT_OWNER_ENV = "MLOMEGA_ALLOW_LEGACY_IMPLICIT_OWNER"
_TRUTHY = {"1", "true", "yes", "on"}


def legacy_implicit_owner_enabled() -> bool:
    """Return whether an operator explicitly enabled unsafe legacy fallback."""
    return os.environ.get(_LEGACY_IMPLICIT_OWNER_ENV, "false").strip().lower() in _TRUTHY


def reject_implicit_owner_fallback(operation: str) -> None:
    """Fail closed before legacy code selects an arbitrary/default owner.

    The compatibility switch is for controlled data-repair diagnostics only.
    It is not accepted by the V18 release audit and must not be set for a live
    multi-person deployment.
    """
    if legacy_implicit_owner_enabled():
        return
    raise ScopeError(
        f"Explicit person_id is required for {operation}; "
        f"legacy default-owner selection is disabled in V18. "
        f"Set {_LEGACY_IMPLICIT_OWNER_ENV}=true only for isolated repair work."
    )
