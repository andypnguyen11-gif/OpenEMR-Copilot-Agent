"""LangGraph node bodies for the W2-07 supervisor graph.

Each module in this package wraps a v1 worker (or the v1 single-loop
orchestrator) as a LangGraph node body. Node bodies read the typed
:class:`TurnState`, do their work, and return a partial dict that the
graph merges into the next state.

The naming matches the planner's ``Worker`` enum members so the graph
topology in :mod:`orchestrator.supervisor_langgraph` can register
``add_node(worker.value, make_node(...))`` without any string lookups.
"""

from __future__ import annotations
