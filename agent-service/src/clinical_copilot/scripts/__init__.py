"""Operator scripts invoked via ``python -m clinical_copilot.scripts.<name>``.

These are maintenance utilities — not part of the request-serving runtime.
They reuse the runtime's settings/engine wiring so dev and prod hit the
same database the agent itself reads.
"""
