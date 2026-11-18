# Desk Ticket API

HTTP API for a multi-workspace help desk. Users belong to workspaces, and each workspace owns tickets, messages, and an audit trail of changes.

## Tech stack

| Piece | What it is used for |
|---|---|
| Python 3.12 | Language runtime |
| FastAPI | HTTP routes, request validation, OpenAPI docs |
| Uvicorn | ASGI server |
| Pydantic and pydantic-settings | Request and response models, environment config |
| PyJWT | Signed access tokens (HS256) |
| SQLAlchemy 2 (async) | Database models and queries |
| Alembic | Schema migrations |
| PostgreSQL 16 with pgvector (`asyncpg`) | Database, locally and in tests |
| Redis 7 | Read cache, ticket-list invalidation, rate limits, readiness |
| Hatchling | Package build |
| pytest, pytest-asyncio, httpx | Tests |
| Ruff | Lint and format checks |
| Docker Compose | API, ingestion worker, Postgres, and Redis together |

## Authentication and tenant isolation

Users sign up with `POST /api/v1/users`, then exchange email and password for an access token at `POST /api/v1/auth/token`. Every other `/api/v1` route needs `Authorization: Bearer <token>`. Tokens are JWTs signed with `SECRET_KEY` and expire after `ACCESS_TOKEN_TTL_MINUTES` (default 60). Passwords are stored as PBKDF2-SHA256 hashes. A failed login returns the same error whether or not the email exists.

Each workspace is a tenant. Rules:

- Workspace routes check that the caller is a member of the workspace in the path. A non-member gets `404`, the same answer as a workspace that does not exist, so ids cannot be probed.
- Who did something always comes from the token. Request bodies do not carry `created_by`, `actor_id`, `author_id`, or `owner_id`; extra fields like these are ignored.
- Owners and admins add members. Only owners can add owners or admins.
- `system` messages are written by the server only.
- A user's profile is visible to themselves and to people who share a workspace with them.

In production the process refuses to start with `DEBUG=true` or a weak `SECRET_KEY`. OpenAPI docs are off when `ENVIRONMENT=production`.

Redis is optional at runtime. If it is down, cache reads and rate limits fall through and the request still hits the database. Readiness (`GET /ready`) returns 503 when Redis is configured and does not answer.

## Project structure

```
desk-ticket-backend/
├── app/
│   ├── main.py              # App factory, lifespan, error handlers, middleware
│   ├── api/
│   │   ├── deps.py          # Current user, workspace membership, roles, rate limits
│   │   ├── router.py        # Mounts /api/v1 routers
│   │   └── v1/              # health, auth, users, workspaces, tickets, knowledge
│   ├── core/
│   │   ├── config.py        # Settings from the environment
│   │   ├── logging.py       # Text or JSON logs, request id
│   │   ├── middleware.py    # Request id and access log
│   │   ├── errors.py        # AppError
│   │   ├── security.py      # Access token create and verify
│   │   ├── redis.py         # Redis client lifecycle
│   │   └── cache.py         # Cached reads and ticket-list version
│   ├── ingestion/           # Chunker, Postgres job queue, worker process
│   ├── db/
│   │   ├── base.py          # SQLAlchemy Base
│   │   └── session.py       # Async engine and get_db
│   ├── models/              # User, Workspace, Ticket, messages, events, knowledge
│   ├── schemas/             # Pydantic request and response models
│   └── services/            # Database operations and password hashing
├── alembic/                 # Migration scripts
├── tests/                   # API tests, including a fake Redis
├── docker/entrypoint.sh     # Migrate, then start Uvicorn
├── Dockerfile
├── docker-compose.yml       # API, worker, Postgres 16 + pgvector, Redis 7
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
| `POST` | `/api/v1/users` | Sign up (no token needed) |
| `POST` | `/api/v1/auth/token` | Log in, get an access token |
| `GET` | `/api/v1/users/me` | Read the current user |
| `GET` | `/api/v1/users/{user_id}` | Read yourself or a co-member |
| `POST` | `/api/v1/workspaces` | Create a workspace (you become owner) |
| `GET` | `/api/v1/workspaces` | List your workspaces |
| `GET` | `/api/v1/workspaces/{workspace_id}` | Read a workspace |
| `POST` | `/api/v1/workspaces/{workspace_id}/members` | Add a member (owner or admin) |
| `GET` | `/api/v1/workspaces/{workspace_id}/members` | List members |
| `POST` | `/api/v1/workspaces/{workspace_id}/tickets` | Create a ticket |
| `GET` | `/api/v1/workspaces/{workspace_id}/tickets` | List tickets (filterable) |
| `GET` | `/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}` | Read a ticket |
| `PATCH` | `/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}` | Update a ticket |
| `POST` | `.../tickets/{ticket_id}/messages` | Add a message |
| `GET` | `.../tickets/{ticket_id}/messages` | List messages |
| `GET` | `.../tickets/{ticket_id}/events` | List change events |
| `POST` | `.../tickets/{ticket_id}/knowledge` | Propose a Q&A from a resolved ticket |
| `POST` | `/api/v1/workspaces/{workspace_id}/knowledge` | Add a Markdown document |
| `GET` | `/api/v1/workspaces/{workspace_id}/knowledge` | List knowledge (filter by `status`, `kind`) |
| `GET` | `.../knowledge/{source_id}` | Read a knowledge source |
| `PATCH` | `.../knowledge/{source_id}` | Edit title, question, or body |
| `POST` | `.../knowledge/{source_id}/approve` | Approve (owner or admin) |
| `POST` | `.../knowledge/{source_id}/reject` | Reject with a note (owner or admin) |
| `POST` | `.../knowledge/{source_id}/archive` | Archive (owner or admin) |
| `GET` | `.../knowledge/{source_id}/chunks` | Chunks built from an approved source |
| `GET` | `.../knowledge/{source_id}/ingestion` | Latest ingestion job and its status |

Workspace roles are `owner`, `admin`, `agent`, and `member`. Ticket status is `open`, `pending`, `resolved`, or `closed`. Priority is `low`, `medium`, `high`, or `urgent`. Messages are `reply`, `internal_note`, or `system`.

Writes, including signup and login, are limited to 10 requests per minute per caller. Other API routes are limited to 60. User and workspace reads are cached for 60 seconds. Ticket lists are cached for 30 seconds and drop when a ticket in that workspace is created or updated.

## Knowledge base

Each workspace keeps its own knowledge: Markdown documents and question-and-answer pairs written from resolved tickets. Later milestones chunk, embed, and retrieve it. Only `approved` sources will ever be retrieved.

| Who | Can do |
|---|---|
| Owner, admin | Add documents (published approved), approve, reject, archive, edit anything not archived |
| Agent | Submit documents and ticket answers for review, edit their own pending or rejected drafts |
| Member | Nothing. Knowledge, drafts, and review notes are internal |

Rules:

- Ticket answers always wait for review, even from owners, since they are written from a real customer conversation. Remove names, emails, and account details before submitting.
- Only `resolved` or `closed` tickets can be proposed, and a ticket has at most one pending or approved entry.
- Content that is already pending or approved in the workspace is rejected as a duplicate (whitespace and title are ignored). Partial unique indexes back this up in the database.
- Editing a rejected draft sends it back to review. An owner or admin editing approved text keeps it approved and becomes its reviewer. Every content change bumps `version`.
- Archive instead of delete, so past answers that cited a source stay traceable.

## Ingestion

A separate worker process turns approved knowledge into chunks: `make worker` locally, the `worker` service in Docker Compose.

- **Queue.** Jobs are rows in `ingestion_jobs`, inserted in the same transaction that approves, edits, or archives a source, so a job is never lost. Workers claim them with `SELECT ... FOR UPDATE SKIP LOCKED`, so several can run at once without taking the same job.
- **Idempotent.** A job only names a source. The worker reads the source when it runs: an approved source gets the chunks its current text produces, anything else gets none. Edits made before a job runs share one queued job.
- **Chunking.** Markdown is split on headings, then whole paragraphs are packed to about 1,600 characters (roughly 400 tokens), with a short overlap when a section spills over. Code blocks stay whole unless one alone is over 2,400 characters. Each chunk keeps its heading path, like `Guide > Billing > Refunds`. Ticket answers repeat the question in every chunk.
- **Unchanged text is kept.** A re-sync keeps chunks whose text did not change and only adds or removes the rest, so later embeddings are only recomputed where needed.
- **Failures.** A failed job retries after 10s, 20s, 40s, and so on (capped at an hour), up to `INGESTION_MAX_ATTEMPTS` (default 5), then stays `failed` with `last_error`. A worker that dies mid-job leaves a lease (`INGESTION_LEASE_SECONDS`, default 300) that runs out, and another worker picks the job up.

## Local

Requires Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
make install
cp .env.example .env
make db        # Postgres and Redis in Docker
make migrate
make dev        # API
make worker     # ingestion, in a second terminal
```

- Liveness: `GET /health`
- Readiness: `GET /ready`
- Docs: `http://127.0.0.1:8000/docs`

`make lint` runs Ruff. `make test` runs pytest against Postgres with Redis disabled, so run `make db` first. Each run drops and recreates the database named in `TEST_DATABASE_URL` (default `desk_test` on the Docker Postgres), builds it with the Alembic migrations, and empties all tables before every test. The name must end in `_test`. A test fails if the models and migrations drift apart.

## Docker

Put a real `SECRET_KEY` (at least 32 characters) in `.env`, then:

```bash
docker compose up --build
```

The container runs `alembic upgrade head` before Uvicorn. Postgres credentials in `docker-compose.yml` are for local Docker only. Change them before any shared deployment.
