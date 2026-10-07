.PHONY: install dev test lint migrate

install:
	python -m pip install -e ".[dev]"

dev:
	uvicorn app.main:app --reload

test:
	pytest

lint:
	ruff check .
	ruff format --check .

migrate:
	alembic upgrade head
