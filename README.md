# Desk Ticket API

Production FastAPI template with config checks, structured request logs, async SQLAlchemy, Alembic, and a sample tickets API.

## Layout

- `app/main.py` — app factory, error handlers, middleware
- `app/core/` — settings, logging, request IDs
- `app/api/v1/` — HTTP routes
- `app/services/` — database operations
- `app/models/` — SQLAlchemy models
- `alembic/` — schema migrations
- `docker-compose.yml` — API plus Postgres

## Local

Requires Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
make install
cp .env.example .env
make migrate
make dev
```

- Liveness: `GET /health`
- Readiness: `GET /ready`
- Docs: `http://127.0.0.1:8000/docs`
- Users, workspaces, and tickets: `http://127.0.0.1:8000/api/v1/workspaces/{workspace_id}/tickets`

`API_KEY` is optional locally. When it is set, API routes expect `Authorization: Bearer <API_KEY>`.

## Tests

```bash
make test
```

## Docker

Put a real `SECRET_KEY` (at least 32 characters) and `API_KEY` (at least 16 characters) in `.env`, then:

```bash
docker compose up --build
```

The container runs `alembic upgrade head` before Uvicorn. Docs are disabled when `ENVIRONMENT=production`. The app refuses to start in production with `DEBUG=true` or a weak `SECRET_KEY`.

Postgres credentials in `docker-compose.yml` are for local Docker only. Change them before any shared deployment.
