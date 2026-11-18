"""Turns approved knowledge sources into retrievable chunks.

chunking.py  pure text splitting, no database
jobs.py      the Postgres job queue: enqueue, claim, finish, retry
sync.py      brings one source's chunks in line with its current state
worker.py    the long-running process: `python -m app.ingestion.worker`
"""
