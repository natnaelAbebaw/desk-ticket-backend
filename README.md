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
| PostgreSQL 16 with pgvector (`asyncpg`) | Database and chunk embeddings, locally and in tests |
| Ollama with `nomic-embed-text` | Free, open-source embedding model served over HTTP |
| Redis 7 | Read cache, ticket-list invalidation, rate limits, readiness |
| Hatchling | Package build |
| pytest, pytest-asyncio, httpx | Tests |
| Ruff | Lint and format checks |
| Docker Compose | API, ingestion worker, Ollama, Postgres, and Redis together |

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
│   ├── embeddings/          # Embedder interface, Ollama and fake embedders, embed phase, backfill
│   ├── search/              # Keyword, vector, and hybrid (RRF) search
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
├── docker-compose.yml       # API, worker, Ollama, Postgres 16 + pgvector, Redis 7
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
| `POST` | `.../knowledge/search` | Best-matching approved chunks for a question (`mode`: hybrid, keyword, vector) |
| `GET` | `.../knowledge/{source_id}/chunks` | Chunks built from an approved source, with the model that embedded each |
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

## Embeddings

After syncing a source's chunks, the worker gives each chunk a vector, so search can match by meaning, not only by shared words.

- **Model.** [Ollama](https://ollama.com) serves `nomic-embed-text` (open source, 768 dimensions, runs on a CPU) over a local HTTP API: `make ollama` starts it and downloads the model once. No API key or paid account is needed. The app codes against a small `Embedder` interface, so a hosted provider can be added later without touching the worker.
- **No transaction during model calls.** Embedding happens in batches (`EMBEDDING_BATCH_SIZE`, default 32). Each batch reads its chunks, closes the transaction, calls the model, then writes the vectors in a new short transaction and extends the job's lease. A slow model never holds a database lock, and a failure keeps the batches already written.
- **Only what changed.** A chunk is embedded when its `embedding_model` differs from `EMBEDDING_MODEL` (including never embedded). Unchanged chunks keep their vectors after an edit. The job's `embedded_count` shows how many went to the model.
- **Model name per chunk.** Vectors from different models cannot be compared. After changing `EMBEDDING_MODEL`, run `make reembed` to queue every source with old vectors; search must only use chunks whose `embedding_model` matches the current model. A model with a size other than 768 also needs a migration of the `embedding` column.
- **Outages.** If Ollama is down, jobs retry with the usual backoff and then fail with a message saying how to start it. Once it is back, `make reembed` re-queues whatever is still missing vectors.
- **Tests** use a deterministic fake embedder, so they never need Ollama or the network.

## Search

`POST /api/v1/workspaces/{workspace_id}/knowledge/search` with `{"query": "...", "mode": "hybrid", "limit": 5}`. Owners, admins, and agents can search; results come only from the caller's workspace.

- **Keyword.** Postgres full-text search over a generated `tsvector` column (GIN index). Words are stemmed (`refunded` matches `refund`), heading matches rank above content matches, and the query accepts search-box syntax: `"exact phrase"`, `or`, `-exclude`. Every word must match, so it is best for exact terms such as error codes.
- **Vector.** The question is embedded with the same model as the chunks and compared by cosine distance through an HNSW index. Best for meaning: "get my money back" finds the refund policy. Vector search always returns the nearest chunks, even weak ones; deciding what is too weak to answer from belongs to answer generation.
- **Hybrid** (default). Both searches return 50 candidates, merged with Reciprocal Rank Fusion. Its keyword side needs every word, like keyword mode, so it speaks up for codes and exact terms and stays silent on most full sentences. (Matching any word ranked worse in the eval: common words pushed right vector results down.) Fusion works like this: each chunk scores `1/(60 + rank)` per list it appears in, so chunks found by both rise to the top. Each hit includes `keyword_rank` and `vector_rank` to show why it ranked where it did.
- **Filters in SQL.** Every query requires the caller's workspace, a source that is approved right now (an archived source vanishes from results immediately, before the worker removes its chunks), and, for vectors, the current `EMBEDDING_MODEL`. HNSW iterative scans (pgvector 0.8) keep a small workspace from getting too few results in a large shared table.
- **Model down.** Hybrid search falls back to keyword results with `"degraded": true`, matching chunks with *any* of the question's words, because without vectors requiring every word finds almost nothing for full sentences; vector-only search returns `503 embedding_unavailable`.

## Retrieval evaluation

`make eval` measures how well search finds the right knowledge. It runs on the demo data, so run `make seed-reset` first, with the worker and Ollama running.

The questions live in `evals/retrieval/kettle-demo.yaml`: 44 cases, each a question plus the chunk headings that answer it. There are four kinds:
- **exact**: error codes and fees
- **paraphrase**: customer wording that shares few words with the answer
- **ticket**: answered by a Q&A written from a resolved ticket
- **unanswerable**: off-topic questions, questions answered only by another tenant, and questions answered only by pending or rejected knowledge

For each mode (keyword, vector, hybrid) the eval reports:

| Metric | Meaning |
|---|---|
| recall@1, @3, @5 | Share of answerable questions with a right chunk in the top 1, 3 or 5. Answer generation uses the top 5 |
| MRR | Mean of 1/rank of the first right chunk: 1st scores 1, 2nd 0.5, not found 0 |
| Cutoff | The cosine similarity that best separates answerable from unanswerable questions. Answer generation uses it to say "I don't know". It is based on vector similarity, because hybrid RRF scores depend only on rank and cannot be compared across questions |

A second file, `kettle-demo-holdout.yaml` (34 cases, `make eval-holdout`), is for checking only. Settings and the cutoff are chosen on the first file, so its scores flatter them; the holdout shows whether a choice works on questions it was not chosen for.

`make eval-baseline` saves a run as `evals/retrieval/kettle-demo.baseline.json`. Later runs show changes from that baseline, plus any question whose hybrid rank moved. To try other search settings without changing code:

```bash
python -m app.evals.retrieval --rrf-k 20 --candidates 30 --any-word-hybrid
```

## Local

Requires Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
make install
cp .env.example .env
make db        # Postgres and Redis in Docker
make ollama    # embedding model in Docker (first run downloads ~270 MB)
make migrate
make dev        # API
make worker     # ingestion and embeddings, in a second terminal
```

- Liveness: `GET /health`
- Readiness: `GET /ready`
- Docs: `http://127.0.0.1:8000/docs`

### Demo data

`make seed` loads two fictional companies through the real service functions, then waits for the worker to embed their knowledge. `make seed-reset` deletes them and loads them again. Every demo user's password is `demo-password`.

- **Kettle & Co** (`maya@kettle.test`, owner): sells point-of-sale systems to cafés.
  - 6 users, one for each role.
  - 22 tickets covering every status, with replies and internal notes.
  - 6 published help articles.
  - Ticket answers in each review state: approved, pending and rejected.
  - A few tickets on purpose have no answer in the knowledge base.
- **Harbor Books** (`rosa@harbor.test`, owner): a second tenant with its own 30-day returns policy. If "30 days" ever appears in a Kettle search, results have leaked across workspaces.

The data lives in `app/seed/data.py`.

`make lint` runs Ruff. `make test` runs pytest against Postgres with Redis disabled, so run `make db` first. Each run drops and recreates the database named in `TEST_DATABASE_URL` (default `desk_test` on the Docker Postgres), builds it with the Alembic migrations, and empties all tables before every test. The name must end in `_test`. A test fails if the models and migrations drift apart.

## Docker

Put a real `SECRET_KEY` (at least 32 characters) in `.env`, then:

```bash
docker compose up --build
```

A one-off `migrate` service runs `alembic upgrade head` first; the API and worker start only after it succeeds. Postgres credentials in `docker-compose.yml` are for local Docker only. Change them before any shared deployment.
