.PHONY: install db ollama dev worker reembed test lint migrate

install:
	python -m pip install -e ".[dev]"

db:
	docker compose up -d db redis

# The local embedding model. The first run downloads nomic-embed-text (about 270 MB).
ollama:
	docker compose up -d ollama ollama-pull

dev:
	uvicorn app.main:app --reload

# Chunks approved knowledge in the background. Run next to `make dev`.
worker:
	python -m app.ingestion.worker

# Re-queue sources whose chunks lack a vector from the current EMBEDDING_MODEL.
reembed:
	python -m app.embeddings.backfill

test:
	pytest

lint:
	ruff check .
	ruff format --check .

migrate:
	alembic upgrade head
