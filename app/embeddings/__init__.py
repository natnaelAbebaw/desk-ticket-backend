"""Turning text into vectors.

- base.py: the Embedder interface the rest of the app codes against, and EmbeddingError.
- ollama.py: the real embedder, an open-source model served by Ollama over HTTP.
- fake.py: a deterministic embedder for tests (no network, no cost, never flaky).
- embed.py: the worker's embed phase, which fills in missing or stale chunk vectors.
- backfill.py: re-queues sources whose chunks were embedded by another model.

Only build_embedder below knows which provider is configured, so swapping Ollama for a
hosted API later means adding one class and one branch here.
"""

from app.core.config import Settings
from app.embeddings.base import Embedder, EmbeddingError
from app.embeddings.fake import FakeEmbedder
from app.embeddings.ollama import OllamaEmbedder

__all__ = ["Embedder", "EmbeddingError", "FakeEmbedder", "OllamaEmbedder", "build_embedder"]


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "fake":
        return FakeEmbedder()
    return OllamaEmbedder(
        base_url=settings.ollama_url,
        model=settings.embedding_model,
        timeout_seconds=settings.embedding_timeout_seconds,
        document_prefix=settings.embedding_document_prefix,
        query_prefix=settings.embedding_query_prefix,
    )
