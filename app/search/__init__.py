"""Retrieval over a workspace's approved knowledge.

- service.py: keyword, vector, and hybrid search, with the tenant and approval filters.
- fusion.py: Reciprocal Rank Fusion, which merges the keyword and vector rankings.

Answer generation (milestone 6) and the agent's knowledge tool (milestone 7) call
service.search; the HTTP route in app/api/v1/knowledge.py is a thin wrapper around it.
"""
