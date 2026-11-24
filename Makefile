.PHONY: install db ollama dev worker reembed seed seed-reset eval eval-baseline eval-holdout test lint migrate

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

# Fictional demo workspaces (Kettle & Co, Harbor Books) with tickets and knowledge.
seed:
	python -m app.seed --wait

seed-reset:
	python -m app.seed --reset --wait

# Retrieval quality on the demo data (needs make seed-reset, the worker, and Ollama).
eval:
	python -m app.evals.retrieval

eval-baseline:
	python -m app.evals.retrieval --save-baseline

# Questions that search settings are never tuned on (see the file's header).
eval-holdout:
	python -m app.evals.retrieval --dataset evals/retrieval/kettle-demo-holdout.yaml

test:
	pytest

lint:
	ruff check .
	ruff format --check .

migrate:
	alembic upgrade head
