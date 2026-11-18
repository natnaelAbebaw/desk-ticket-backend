# Desk Ticket API

HTTP API for a multi-workspace help desk. Users belong to workspaces, and each workspace owns tickets, messages, and an audit trail of changes.

## Tech stack

| Piece | What it is used for |
|---|---|
| Python 3.12 | Language runtime |
| FastAPI | HTTP routes, request validation, OpenAPI docs |
| Uvicorn | ASGI server |
| Pydantic and pydantic-settings | Request and response models, environment config |
| SQLAlchemy 2 (async) | Database models and queries |
| Alembic | Schema migrations |
| PostgreSQL 16 (`asyncpg`) | Database in Docker |
| SQLite (`aiosqlite`) | Default local database |
| Redis 7 | Read cache, ticket-list invalidation, rate limits, readiness |
| Hatchling | Package build |
| pytest, pytest-asyncio, httpx | Tests |
| Ruff | Lint and format checks |
| Docker Compose | API, Postgres, and Redis together |

User passwords are stored as PBKDF2-SHA256 hashes. API routes use an optional shared API key (`Authorization: Bearer <API_KEY>`), not per-user sessions. When `API_KEY` is empty, key checks are skipped. In production the process refuses to start with `DEBUG=true`, a weak `SECRET_KEY`, or an `API_KEY` shorter than 16 characters. OpenAPI docs are off when `ENVIRONMENT=production`.

Redis is optional at runtime. If it is down, cache reads and rate limits fall through and the request still hits the database. Readiness (`GET /ready`) returns 503 when Redis is configured and does not answer.

## Project structure

```
desk-ticket-backend/
├── app/
│   ├── main.py              # App factory, lifespan, error handlers, middleware
│   ├── api/
│   │   ├── deps.py          # API key check and Redis rate limits
│   │   ├── router.py        # Mounts /api/v1 routers
│   │   └── v1/              # health, users, workspaces, tickets
│   ├── core/
│   │   ├── config.py        # Settings from the environment
│   │   ├── logging.py       # Text or JSON logs, request id
│   │   ├── middleware.py    # Request id and access log
│   │   ├── errors.py        # AppError
│   │   ├── redis.py         # Redis client lifecycle
│   │   └── cache.py         # Cached reads and ticket-list version
│   ├── db/
│   │   ├── base.py          # SQLAlchemy Base
│   │   └── session.py       # Async engine and get_db
│   ├── models/              # User, Workspace, Ticket, messages, events
│   ├── schemas/             # Pydantic request and response models
│   └── services/            # Database operations and password hashing
├── alembic/                 # Migration scripts
├── tests/                   # API tests, including a fake Redis
├── docker/entrypoint.sh     # Migrate, then start Uvicorn
├── Dockerfile
├── docker-compose.yml       # API, Postgres 16, Redis 7
├── Makefile
├── pyproject.toml
└── alembic.ini
```

Request flow is route (`app/api`) → service (`app/services`) → model (`app/models`). Schemas stay at the HTTP boundary. Settings, logging, Redis, and errors stay in `app/core`.

### HTTP routes

Health is mounted at the root. Everything else is under `/api/v1`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness |
| `GET` | `/ready` | Database and Redis readiness |
| `POST` | `/api/v1/users` | Create a user |
| `GET` | `/api/v1/users/{user_id}` | Read a user |
| `POST` | `/api/v1/workspaces` | Create a workspace |
| `GET` | `/api/v1/workspaces/{workspace_id}` | Read a workspace |
| `POST` | `/api/v1/workspaces/{workspace_id}/members` | Add a member |
| `GET` | `/api/v1/workspaces/{workspace_id}/members` | List members |
| `POST` | `/api/v1/workspaces/{workspace_id}/tickets` | Create a ticket |
| `GET` | `/api/v1/workspaces/{workspace_id}/tickets` | List tickets (filterable) |
| `GET` | `/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}` | Read a ticket |
| `PATCH` | `/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}` | Update a ticket |
| `POST` | `.../tickets/{ticket_id}/messages` | Add a message |
| `GET` | `.../tickets/{ticket_id}/messages` | List messages |
| `GET` | `.../tickets/{ticket_id}/events` | List change events |

Workspace roles are `owner`, `admin`, `agent`, and `member`. Ticket status is `open`, `pending`, `resolved`, or `closed`. Priority is `low`, `medium`, `high`, or `urgent`. Messages are `reply`, `internal_note`, or `system`.

Writes are limited to 10 requests per minute per caller. Other API routes are limited to 60. User and workspace reads are cached for 60 seconds. Ticket lists are cached for 30 seconds and drop when a ticket in that workspace is created or updated.

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

`make lint` runs Ruff. `make test` runs pytest against an in-memory SQLite database with Redis disabled.

## Docker

Put a real `SECRET_KEY` (at least 32 characters) and `API_KEY` (at least 16 characters) in `.env`, then:

```bash
docker compose up --build
```

The container runs `alembic upgrade head` before Uvicorn. Postgres credentials in `docker-compose.yml` are for local Docker only. Change them before any shared deployment.
