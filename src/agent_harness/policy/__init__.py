"""Policy and Approval Engine (Phase 4).

The only place effective permission is computed. ``TaskContract.
requested_capabilities`` (domain, Phase 1.1) is a request; this package
turns a request plus an admin-owned ``PolicyCeiling`` into a
``PolicyDecision.grants`` — the sole legitimate source of permission
elsewhere in the system.
"""
