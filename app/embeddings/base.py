from typing import Protocol


class EmbeddingError(Exception):
    """The embedding model could not produce vectors (down, timed out, bad reply).

    The worker treats it like any other job failure: the attempt is recorded and the
    job retries with backoff. Messages should say what to do, because they end up in
    ingestion_jobs.last_error where an admin reads them.
    """


class Embedder(Protocol):
    """What the app needs from an embedding model.

    A Protocol is structural typing: any class with these attributes and methods is an
    Embedder, without inheriting from this one. Tests can pass a FakeEmbedder and the
    worker cannot tell the difference.
    """

    # Stored on each chunk next to its vector (knowledge_chunks.embedding_model).
    model: str

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, in the same order. Used for chunks."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """A vector for a search query. Kept separate from embed_documents because many
        retrieval models encode the two sides differently (milestone 4 uses this)."""
        ...

    async def aclose(self) -> None:
        """Release connections. Called once when the process shuts down."""
        ...
