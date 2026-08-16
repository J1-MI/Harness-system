"""Application layer: use cases built on top of domain contracts.

Depends on ``agent_harness.domain`` only. Must not depend on
``agent_harness.providers`` or any concrete infrastructure (persistence,
process execution, etc.) — those are injected as ports in later phases.
"""
