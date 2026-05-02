"""Process-runtime utilities — long-lived loops, threadpools, etc.

The bridge in :mod:`async_bridge` lets the synchronous Tool layer (PR 7)
call the asynchronous :class:`FhirClient` (PR 6) without each tool
spinning up its own loop. Kept in its own package so future utilities
(circuit breakers in PR 25, warm-keep loop in PR 27) land alongside.
"""
