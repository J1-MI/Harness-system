"""Persistence layer (Phase 2.1): SQLite state + journal only.

Blob/artifact storage is Phase 2.2 and is not implemented here — nothing
in this package writes bytes to disk outside of ``harness.sqlite3``.
"""
