"""Read-only views of ingestion output, for checking what the worker produced."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.ingestion import JobStatus


class ChunkRead(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    source_version: int
    position: int
    heading: str
    content: str
    token_estimate: int
    # The vector itself is left out: 768 numbers per chunk mean nothing to a reader.
    # The model name shows whether it is embedded, and by which model; null means
    # the worker has not embedded it yet.
    embedding_model: str | None
    embedded_at: datetime | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class IngestionJobRead(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    status: JobStatus
    attempts: int
    run_after: datetime
    last_error: str | None
    chunk_count: int | None
    embedded_count: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}
