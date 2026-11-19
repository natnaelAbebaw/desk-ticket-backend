.PHONY: install db dev worker test lint migrate

install:
	python -m pip install -e ".[dev]"

db:
	docker compose up -d db redis

dev:
	uvicorn app.main:app --reload

# Chunks approved knowledge in the background. Run next to `make dev`.
worker:
	python -m app.ingestion.worker

test:
	pytest

lint:
	ruff check .
	ruff format --check .

migrate:
	alembic upgrade head
