.PHONY: install db dev test lint migrate

install:
	python -m pip install -e ".[dev]"

db:
	docker compose up -d db redis

dev:
	uvicorn app.main:app --reload

test:
	pytest

lint:
	ruff check .
	ruff format --check .

migrate:
	alembic upgrade head
