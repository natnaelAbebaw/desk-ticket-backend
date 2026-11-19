"""A deterministic stand-in for a real embedding model, for tests.

It is not random: each word is hashed to one of the vector's positions (the "hashing
trick"), and the vector counts words. Two texts that share words therefore point in
similar directions, so search tests in milestone 4 can assert that a refund question
finds the refund chunk without loading a real model.
"""

import hashlib
import math
import re

from app.models.ingestion import EMBEDDING_DIMENSIONS

_WORD = re.compile(r"\w+")


class FakeEmbedder:
    def __init__(self, *, model: str = "fake-embedder", dimensions: int = EMBEDDING_DIMENSIONS):
        self.model = model
        self.dimensions = dimensions
        # Every batch passed to embed_documents, so tests can check what was (and was
        # not) sent to the model.
        self.calls: list[list[str]] = []

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self.vector(text)

    async def aclose(self) -> None:
        return None

    @property
    def embedded_texts(self) -> list[str]:
        return [text for batch in self.calls for text in batch]

    def vector(self, text: str) -> list[float]:
        values = [0.0] * self.dimensions
        for word in _WORD.findall(text.lower()):
            # sha256, not hash(): Python randomizes hash() per process, and the same
            # text must give the same vector in every run.
            digest = hashlib.sha256(word.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            values[index] += 1.0
        # Scale to length 1, like most real models, so cosine distance behaves the same.
        # An empty text gets a fixed unit vector instead of dividing by zero.
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            values[0] = 1.0
            return values
        return [value / norm for value in values]
