"""Embeddings from a local Ollama server.

Ollama (https://ollama.com) downloads open-source models and serves them over a small
HTTP API, so no API key or paid account is needed. The default model,
nomic-embed-text, is Apache-2.0 licensed, returns 768-dimension vectors, and runs on a
CPU. Treating it as a remote HTTP service, not a library loaded into this process,
keeps the heavy model out of the API and worker images and means the worker already
handles timeouts and outages the way it would for a hosted provider.
"""

import httpx

from app.embeddings.base import EmbeddingError

# Ollama's own error replies are short, but cap what is copied into last_error anyway.
_ERROR_BODY_CHARS = 500


class OllamaEmbedder:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float,
        document_prefix: str = "",
        query_prefix: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._document_prefix = document_prefix
        self._query_prefix = query_prefix
        # One client for the life of the process: it keeps the TCP connection to Ollama
        # open between batches instead of reconnecting every time. `transport` lets
        # tests swap in httpx.MockTransport and fake Ollama's replies.
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, transport=transport
        )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([self._document_prefix + text for text in texts])

    async def embed_query(self, text: str) -> list[float]:
        [vector] = await self._embed([self._query_prefix + text])
        return vector

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _embed(self, inputs: list[str]) -> list[list[float]]:
        if not inputs:
            return []
        try:
            # /api/embed takes a list and returns one vector per item, so a whole batch
            # is one request. "truncate" cuts any text longer than the model's context
            # instead of failing. Chunks are far below that limit, so it is only a
            # safety net.
            response = await self._client.post(
                "/api/embed", json={"model": self.model, "input": inputs, "truncate": True}
            )
        except httpx.TimeoutException as exc:
            raise EmbeddingError(f"Ollama did not answer in time ({exc!r})") from exc
        except httpx.HTTPError as exc:
            # Connection refused, DNS failure, and so on: usually Ollama is not running.
            raise EmbeddingError(
                f"Cannot reach Ollama at {self._client.base_url} ({exc!r}). "
                "Is it running? Start it with `make ollama`."
            ) from exc

        if response.status_code == 404:
            # Ollama answers 404 when the model has not been downloaded yet.
            raise EmbeddingError(
                f"Ollama does not have model {self.model!r}. "
                f"Download it with `ollama pull {self.model}`."
            )
        if response.status_code != 200:
            raise EmbeddingError(
                f"Ollama returned {response.status_code}: {response.text[:_ERROR_BODY_CHARS]}"
            )

        try:
            vectors = response.json()["embeddings"]
        except (ValueError, KeyError) as exc:
            raise EmbeddingError("Ollama reply had no 'embeddings' field") from exc
        # A reply with the wrong number of vectors would silently pair chunks with
        # someone else's vector. Refuse it instead.
        if not isinstance(vectors, list) or len(vectors) != len(inputs):
            raise EmbeddingError(
                f"Ollama returned {len(vectors) if isinstance(vectors, list) else 0} "
                f"vectors for {len(inputs)} inputs"
            )
        return vectors
