"""Verification middleware — citations, field checks, abstention.

The keystone of the trust story (ARCHITECTURE §3 layers 3 and 4). The
middleware sits between the agent's draft response and the UI; nothing
the model writes is shown until every cited claim resolves to a record
the agent actually fetched and the structural fields match.
"""
