# DECISIONS

- 2026-07-03: Lot 1 implements the V19 transport seam with a simulator-first `VideoIngress`. Real XREAL/S25 hardware gates remain blocked in this container and must be validated on device before marking Lot 3 hardware steps complete.
- 2026-07-03: E10 checkpoint is not marked complete because `pytest tests/test_v18_*` cannot run in this checkout (`file or directory not found`), the full SimOnly demo is not wired to a live WebSocket server, and real ingress bench data is unavailable. Per handoff, Lot 2 is not started until E10 criteria are validated.
