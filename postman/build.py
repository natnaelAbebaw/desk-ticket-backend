"""Generate the Postman collection that exercises every route built so far (M0 to M4).

Run from the project root:

    python postman/build.py

It writes postman/desk-ticket.postman_collection.json. The collection is generated,
not hand-edited, so that the Markdown documents come straight from examples/chunking/
and a route change is a one-line edit here.

How the collection works:
- Each request saves what later requests need (tokens, ids) into collection variables
  from its test script, so "Run collection" goes top to bottom with no copy-pasting.
- Every email and slug includes {{run_id}}, set fresh by the first request, so the
  collection can be run again and again against the same database.
- Four users play the roles the permission checks care about: owner, agent, member,
  and an outsider who belongs to a different workspace.
"""

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "chunking"
OUT = Path(__file__).resolve().parent / "desk-ticket.postman_collection.json"

# Users and their tokens. The variable names are reused by the auth header helper.
USERS = ["owner", "agent", "member", "outsider"]
PASSWORD = "password123"
API = "{{base_url}}/api/v1"
WS = API + "/workspaces/{{workspace_id}}"


# ---------------------------------------------------------------- JS snippets


def status(code: int) -> str:
    return f'pm.test("status {code}", () => pm.response.to.have.status({code}));\n'


def save(var: str, expr: str) -> str:
    """Store a value from the response for later requests."""
    return f"pm.collectionVariables.set({json.dumps(var)}, {expr});\n"


def check(name: str, condition: str) -> str:
    return f"pm.test({json.dumps(name)}, () => pm.expect({condition}).to.be.true);\n"


# Print the hits as a table in the Postman console (View > Show Postman Console), so a
# search can be judged by eye: which chunk came first, and which search found it.
SHOW_HITS = """
const body = pm.response.json();
console.log(pm.info.requestName, body.degraded ? "(DEGRADED)" : "");
console.table((body.hits || []).map(h => ({
  source: h.source_title, heading: h.heading, score: h.score.toFixed(3),
  kw: h.keyword_rank, vec: h.vector_rank,
})));
const text = (h) => (h.heading + " " + h.content).toLowerCase();
"""


def top_hits_mention(word: str, n: int = 3) -> str:
    """Relevance check: one of the top n hits mentions `word`.

    A failure here is a search-quality signal, not necessarily a bug: real embeddings
    decide the order. Read the console table before changing code.
    """
    return (
        f'pm.test("one of the top {n} hits mentions \\"{word}\\"", () => '
        f"pm.expect(body.hits.slice(0, {n}).some(h => text(h).includes({json.dumps(word)})))"
        ".to.be.true);\n"
    )


def note(message: str) -> str:
    """Print a hint to the Postman Console next to the result table."""
    return f"console.log({json.dumps(message)});\n"


def source_in_hits(var: str, expected: bool) -> str:
    """Whether any hit came from the source whose id is saved in `var`."""
    label = "in" if expected else "not in"
    # JavaScript's true/false, not Python's True/False.
    js_bool = "true" if expected else "false"
    return (
        f'pm.test("{var} {label} hits", () => pm.expect(body.hits.some(\n'
        f'  h => h.source_id === pm.collectionVariables.get("{var}"))).to.be.{js_bool});\n'
    )


# ---------------------------------------------------------------- request builder


def req(
    name: str,
    method: str,
    url: str,
    *,
    as_user: str | None = "owner",
    body: dict[str, Any] | None = None,
    tests: str = "",
    pre: str = "",
    description: str = "",
) -> dict[str, Any]:
    headers = []
    if as_user is not None:
        headers.append({"key": "Authorization", "value": f"Bearer {{{{{as_user}_token}}}}"})
    request: dict[str, Any] = {"method": method, "header": headers, "url": url}
    if description:
        request["description"] = description
    if body is not None:
        headers.append({"key": "Content-Type", "value": "application/json"})
        request["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2)}
    events = []
    if pre:
        events.append(
            {
                "listen": "prerequest",
                "script": {"type": "text/javascript", "exec": pre.splitlines()},
            }
        )
    if tests:
        events.append(
            {"listen": "test", "script": {"type": "text/javascript", "exec": tests.splitlines()}}
        )
    return {"name": name, "event": events, "request": request}


def folder(name: str, description: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": name, "description": description, "item": items}


def wait_for_ingestion(name: str, source_var: str, *, as_user: str = "owner") -> dict[str, Any]:
    """Poll the source's ingestion job until the worker finishes.

    Chunking and embedding run in the worker, not in the request, so search right after
    approval would find nothing. In the Collection Runner this request repeats itself
    (setNextRequest with its own name) once a second, up to 30 times. Sent by hand, just
    click Send again until status is succeeded.
    """
    retries = f"retries_{source_var}"
    pre = (
        # A busy-wait would freeze Postman; setTimeout delays the request instead.
        f'if (Number(pm.collectionVariables.get("{retries}") || 0) > 0) {{\n'
        "  setTimeout(() => {}, 1000);\n"
        "}\n"
    )
    tests = (
        "const job = pm.response.json();\n"
        f'const tries = Number(pm.collectionVariables.get("{retries}") || 0);\n'
        'if (job && (job.status === "queued" || job.status === "running") && tries < 30) {\n'
        f'  pm.collectionVariables.set("{retries}", tries + 1);\n'
        "  postman.setNextRequest(pm.info.requestName);\n"
        "  return;\n"
        "}\n"
        f'pm.collectionVariables.set("{retries}", 0);\n'
        + status(200)
        + 'pm.test("worker succeeded (is `make worker` running?)", () => '
        'pm.expect(job && job.status, job && job.last_error).to.eql("succeeded"));\n'
        + check("every chunk got a vector", "job.embedded_count === job.chunk_count")
    )
    return req(
        name,
        "GET",
        f"{WS}/knowledge/{{{{{source_var}}}}}/ingestion",
        as_user=as_user,
        pre=pre,
        tests=tests,
        description=wait_for_ingestion.__doc__ or "",
    )


def search(
    name: str,
    query: str,
    mode: str = "hybrid",
    *,
    as_user: str = "owner",
    limit: int = 5,
    tests: str = "",
    workspace: str = "workspace_id",
) -> dict[str, Any]:
    return req(
        name,
        "POST",
        f"{API}/workspaces/{{{{{workspace}}}}}/knowledge/search",
        as_user=as_user,
        body={"query": query, "mode": mode, "limit": limit},
        tests=status(200) + SHOW_HITS + tests,
    )


def doc(file: str) -> dict[str, str]:
    title = file.replace("-", " ").capitalize()
    return {"title": title, "body": (EXAMPLES / f"{file}.md").read_text()}


# ---------------------------------------------------------------- folders


def health() -> dict[str, Any]:
    start_run = (
        "// A new run id per run keeps emails and slugs unique across runs.\n"
        'pm.collectionVariables.set("run_id", Date.now().toString(36));\n'
    )
    return folder(
        "0. Health",
        "Liveness and readiness. Also starts a new run (fresh emails and slugs).",
        [
            req(
                "Health",
                "GET",
                "{{base_url}}/health",
                as_user=None,
                pre=start_run,
                tests=status(200),
            ),
            req("Ready (db + redis)", "GET", "{{base_url}}/ready", as_user=None, tests=status(200)),
        ],
    )


def users() -> dict[str, Any]:
    items = []
    for user in USERS:
        email = f"{user}+{{{{run_id}}}}@acme.test"
        items.append(
            req(
                f"Sign up {user}",
                "POST",
                f"{API}/users",
                as_user=None,
                body={"email": email, "name": user.capitalize(), "password": PASSWORD},
                tests=status(201) + save(f"{user}_id", "pm.response.json().id"),
            )
        )
        items.append(
            req(
                f"Log in {user}",
                "POST",
                f"{API}/auth/token",
                as_user=None,
                body={"email": email, "password": PASSWORD},
                tests=status(200) + save(f"{user}_token", "pm.response.json().access_token"),
            )
        )
    items += [
        req(
            "Sign up again with the same email -> 409",
            "POST",
            f"{API}/users",
            as_user=None,
            body={"email": "owner+{{run_id}}@acme.test", "name": "Dup", "password": PASSWORD},
            tests=status(409),
        ),
        req(
            "Log in with a wrong password -> 401",
            "POST",
            f"{API}/auth/token",
            as_user=None,
            body={"email": "owner+{{run_id}}@acme.test", "password": "wrong-password"},
            tests=status(401),
        ),
        req(
            "Sign up with a short password -> 422",
            "POST",
            f"{API}/users",
            as_user=None,
            body={"email": "short+{{run_id}}@acme.test", "name": "Short", "password": "123"},
            tests=status(422),
        ),
        req(
            "Me",
            "GET",
            f"{API}/users/me",
            tests=status(200)
            + check(
                "is the owner", 'pm.response.json().id === pm.collectionVariables.get("owner_id")'
            ),
        ),
        req("Me without a token -> 401", "GET", f"{API}/users/me", as_user=None, tests=status(401)),
        req(
            "Me with a garbage token -> 401",
            "GET",
            f"{API}/users/me",
            as_user="garbage",
            tests=status(401),
        ),
    ]
    return folder("1. Users and auth", "Sign up the four test users and log each one in.", items)


def workspaces() -> dict[str, Any]:
    def add(role: str) -> dict[str, Any]:
        return req(
            f"Add {role} as {role}",
            "POST",
            f"{WS}/members",
            body={"user_id": f"{{{{{role}_id}}}}", "role": role},
            tests=status(201),
        )

    return folder(
        "2. Workspaces and members",
        "Acme belongs to the owner. The outsider gets a workspace of their own.",
        [
            req(
                "Create workspace (owner)",
                "POST",
                f"{API}/workspaces",
                body={"name": "Acme", "slug": "acme-{{run_id}}"},
                tests=status(201) + save("workspace_id", "pm.response.json().id"),
            ),
            req(
                "Create workspace with a bad slug -> 422",
                "POST",
                f"{API}/workspaces",
                body={"name": "Bad", "slug": "Not A Slug!"},
                tests=status(422),
            ),
            req(
                "Create outsider's workspace",
                "POST",
                f"{API}/workspaces",
                as_user="outsider",
                body={"name": "Other Co", "slug": "other-{{run_id}}"},
                tests=status(201) + save("outsider_workspace_id", "pm.response.json().id"),
            ),
            add("agent"),
            add("member"),
            req(
                "Member tries to add someone -> 403",
                "POST",
                f"{WS}/members",
                as_user="member",
                body={"user_id": "{{outsider_id}}", "role": "member"},
                tests=status(403),
            ),
            req("List my workspaces", "GET", f"{API}/workspaces", tests=status(200)),
            req("Get workspace", "GET", WS, tests=status(200)),
            req(
                "List members",
                "GET",
                f"{WS}/members",
                tests=status(200) + check("owner, agent, member", "pm.response.json().total === 3"),
            ),
            req("Outsider reads Acme -> 404", "GET", WS, as_user="outsider", tests=status(404)),
            req(
                "Owner reads the agent (co-member)",
                "GET",
                f"{API}/users/{{{{agent_id}}}}",
                tests=status(200),
            ),
            req(
                "Outsider reads the owner -> 404",
                "GET",
                f"{API}/users/{{{{owner_id}}}}",
                as_user="outsider",
                tests=status(404),
            ),
        ],
    )


def tickets() -> dict[str, Any]:
    ticket = WS + "/tickets/{{ticket_id}}"
    return folder(
        "3. Tickets",
        "Create, filter, update, message, and audit a ticket. Ends resolved, so folder 4 "
        "can turn it into knowledge.",
        [
            req(
                "Create ticket (member)",
                "POST",
                f"{WS}/tickets",
                as_user="member",
                body={
                    "subject": "Charged twice for my March invoice",
                    "description": "I see two identical charges on my card for the same invoice.",
                    "priority": "high",
                    "category": "billing",
                },
                tests=status(201) + save("ticket_id", "pm.response.json().id"),
            ),
            req(
                "List tickets filtered by billing + high",
                "GET",
                f"{WS}/tickets?category=billing&priority=high",
                tests=status(200) + check("finds it", "pm.response.json().total >= 1"),
            ),
            req(
                "List tickets with a bad filter -> 422",
                "GET",
                f"{WS}/tickets?status=lost",
                tests=status(422),
            ),
            req("Get ticket", "GET", ticket, tests=status(200)),
            req(
                "Outsider gets ticket -> 404", "GET", ticket, as_user="outsider", tests=status(404)
            ),
            req(
                "Assign to agent, set pending",
                "PATCH",
                ticket,
                body={"assigned_to": "{{agent_id}}", "status": "pending"},
                tests=status(200),
            ),
            req(
                "Agent replies",
                "POST",
                f"{ticket}/messages",
                as_user="agent",
                body={
                    "body": "Thanks, we see the duplicate charge and are refunding one of them.",
                    "message_type": "reply",
                },
                tests=status(201),
            ),
            req(
                "Agent adds an internal note",
                "POST",
                f"{ticket}/messages",
                as_user="agent",
                body={
                    "body": "Webhook retried; escalated to payments with both charge ids.",
                    "message_type": "internal_note",
                },
                tests=status(201),
            ),
            req(
                "Client sends a system message -> 422",
                "POST",
                f"{ticket}/messages",
                body={"body": "spoofed", "message_type": "system"},
                tests=status(422),
            ),
            req("List messages", "GET", f"{ticket}/messages", tests=status(200)),
            req(
                "Resolve ticket",
                "PATCH",
                ticket,
                as_user="agent",
                body={"status": "resolved"},
                tests=status(200) + check("resolved", 'pm.response.json().status === "resolved"'),
            ),
            req(
                "List events (audit trail)",
                "GET",
                f"{ticket}/events",
                tests=status(200) + check("has events", "pm.response.json().total >= 2"),
            ),
        ],
    )


def knowledge() -> dict[str, Any]:
    source = WS + "/knowledge/{{%s}}"

    def create(
        var: str, file: str, *, as_user: str = "owner", expect: str = "approved"
    ) -> dict[str, Any]:
        return req(
            f"Add document: {file} ({as_user})",
            "POST",
            f"{WS}/knowledge",
            as_user=as_user,
            body=doc(file),
            tests=status(201)
            + save(var, "pm.response.json().id")
            + check(f"status {expect}", f'pm.response.json().status === "{expect}"'),
        )

    return folder(
        "4. Knowledge and ingestion",
        "Documents and ticket answers, the review flow, and the worker. Needs `make worker` "
        "and Ollama running.",
        [
            create("billing_id", "billing-guide"),
            create("security_id", "security-policy"),
            create("api_id", "api-quickstart"),
            create(
                "onboarding_id", "onboarding-handbook", as_user="agent", expect="pending_review"
            ),
            req(
                "Member adds a document -> 403",
                "POST",
                f"{WS}/knowledge",
                as_user="member",
                body={"title": "x", "body": "x"},
                tests=status(403),
            ),
            req(
                "Propose answer from the resolved ticket (agent)",
                "POST",
                WS + "/tickets/{{ticket_id}}/knowledge",
                as_user="agent",
                body={
                    "title": "Customer charged twice for the same invoice",
                    "question": "A customer sees two identical charges for one invoice. "
                    "What do we do?",
                    "body": "Duplicate charges come from a payment webhook retry. "
                    "Refund the second "
                    "charge in full from the admin panel, then send both charge ids to the "
                    "payments team so the provider side is fixed too. Tell the customer the "
                    "money returns in 5 to 10 business days.",
                },
                tests=status(201)
                + save("answer_id", "pm.response.json().id")
                + check("needs review", 'pm.response.json().status === "pending_review"'),
            ),
            req(
                "Agent approves -> 403",
                "POST",
                source % "answer_id" + "/approve",
                as_user="agent",
                tests=status(403),
            ),
            req(
                "Owner approves the ticket answer",
                "POST",
                source % "answer_id" + "/approve",
                tests=status(200),
            ),
            req(
                "Owner rejects the onboarding draft",
                "POST",
                source % "onboarding_id" + "/reject",
                body={"note": "Please add the escalation contacts before we publish."},
                tests=status(200) + check("rejected", 'pm.response.json().status === "rejected"'),
            ),
            req(
                "Agent edits the rejected draft -> back to review",
                "PATCH",
                source % "onboarding_id",
                as_user="agent",
                body={"title": "Onboarding handbook (v2)"},
                tests=status(200)
                + check("pending again", 'pm.response.json().status === "pending_review"'),
            ),
            req(
                "Owner approves onboarding",
                "POST",
                source % "onboarding_id" + "/approve",
                tests=status(200),
            ),
            req(
                "Approve twice -> 409",
                "POST",
                source % "onboarding_id" + "/approve",
                tests=status(409),
            ),
            req(
                "List knowledge (approved only)",
                "GET",
                f"{WS}/knowledge?status=approved",
                tests=status(200),
            ),
            req(
                "List knowledge as member -> 403",
                "GET",
                f"{WS}/knowledge",
                as_user="member",
                tests=status(403),
            ),
            req("Get billing guide", "GET", source % "billing_id", tests=status(200)),
            wait_for_ingestion("Wait for billing ingestion", "billing_id"),
            wait_for_ingestion("Wait for security ingestion", "security_id"),
            wait_for_ingestion("Wait for api ingestion", "api_id"),
            wait_for_ingestion("Wait for onboarding ingestion", "onboarding_id"),
            wait_for_ingestion("Wait for ticket answer ingestion", "answer_id"),
            req(
                "Billing chunks (with embedding model)",
                "GET",
                source % "billing_id" + "/chunks",
                tests=status(200)
                + check("has chunks", "pm.response.json().total > 0")
                + check(
                    "all embedded",
                    "pm.response.json().items.every(c => c.embedding_model && c.embedded_at)",
                ),
            ),
        ],
    )


def search_folder() -> dict[str, Any]:
    return folder(
        "5. Search",
        "Keyword, vector, and hybrid. Open the Postman Console to see each result table. "
        "Tests marked 'mentions' are relevance checks: real embeddings decide them.",
        [
            search(
                "keyword: exact token ws_123",
                "ws_123",
                "keyword",
                tests=check("found", "body.hits.length > 0") + top_hits_mention("ws_123", 1),
            ),
            search(
                "keyword: stemming (refunded -> refund)",
                "refunded",
                "keyword",
                tests=top_hits_mention("refund", 1),
            ),
            search(
                "keyword: no shared words -> few or none",
                "how do I get my money back",
                "keyword",
                tests=note("Keyword search needs the words themselves; compare with vector next."),
            ),
            search(
                "vector: money back -> refunds (meaning)",
                "how do I get my money back",
                "vector",
                tests=top_hits_mention("refund"),
            ),
            search(
                "vector: card declined -> failed payments",
                "my card keeps getting declined",
                "vector",
                tests=top_hits_mention("fail"),
            ),
            search(
                "vector: data leak -> breach policy",
                "someone leaked our customer data",
                "vector",
                tests=top_hits_mention("breach"),
            ),
            search(
                "hybrid: too many requests -> API errors",
                "too many requests error",
                tests=top_hits_mention("429")
                + check("not degraded", "body.degraded === false")
                + check(
                    "scores are RRF (below 2/61)", "body.hits.every(h => h.score <= 2/61 + 1e-9)"
                ),
            ),
            search(
                "hybrid: charged twice -> ticket answer",
                "customer charged twice for one invoice",
                tests=top_hits_mention("charge") + source_in_hits("answer_id", True),
            ),
            search(
                "keyword: excluded word (refund -partial)",
                "refund -partial",
                "keyword",
                tests=check(
                    "no hit mentions partial", 'body.hits.every(h => !text(h).includes("partial"))'
                ),
            ),
            search(
                "vector: nonsense still returns hits (no cutoff yet)",
                "banana bread recipe",
                "vector",
                tests=note("Note these low scores: milestone 5 measures a sensible cutoff."),
            ),
            search(
                "limit 1",
                "refund",
                limit=1,
                tests=check("exactly one", "body.hits.length === 1"),
            ),
        ],
    )


def search_rules() -> dict[str, Any]:
    url = WS + "/knowledge/search"

    def bad(name: str, body: dict[str, Any]) -> dict[str, Any]:
        return req(f"Validation: {name} -> 422", "POST", url, body=body, tests=status(422))

    return folder(
        "6. Search: tenants, roles, archive, validation",
        "The rules that keep search safe, plus the archive check.",
        [
            search(
                "Outsider searches their own empty workspace -> no Acme chunks",
                "refund",
                as_user="outsider",
                workspace="outsider_workspace_id",
                tests=check("zero hits", "body.hits.length === 0"),
            ),
            req(
                "Outsider searches Acme -> 404",
                "POST",
                url,
                as_user="outsider",
                body={"query": "refund"},
                tests=status(404),
            ),
            req(
                "Member searches -> 403",
                "POST",
                url,
                as_user="member",
                body={"query": "refund"},
                tests=status(403),
            ),
            req(
                "Agent searches -> 200",
                "POST",
                url,
                as_user="agent",
                body={"query": "refund"},
                tests=status(200),
            ),
            req(
                "No token -> 401",
                "POST",
                url,
                as_user=None,
                body={"query": "refund"},
                tests=status(401),
            ),
            bad("empty query", {"query": ""}),
            bad("query over 1000 chars", {"query": "a" * 1001}),
            bad("limit 0", {"query": "refund", "limit": 0}),
            bad("limit 21", {"query": "refund", "limit": 21}),
            bad("unknown mode", {"query": "refund", "mode": "fuzzy"}),
            search(
                "Before archive: API doc is found",
                "invoices per page",
                "keyword",
                tests=source_in_hits("api_id", True),
            ),
            req(
                "Archive the API doc",
                "POST",
                WS + "/knowledge/{{api_id}}/archive",
                tests=status(200) + check("archived", 'pm.response.json().status === "archived"'),
            ),
            search(
                "After archive: API doc is gone immediately",
                "invoices per page",
                "keyword",
                tests=source_in_hits("api_id", False),
            ),
            req(
                "Edit an archived doc -> 409",
                "PATCH",
                WS + "/knowledge/{{api_id}}",
                body={"title": "too late"},
                tests=status(409),
            ),
        ],
    )


def degraded() -> dict[str, Any]:
    return folder(
        "7. Manual: Ollama down",
        "Not part of a normal run. First run `sudo docker compose stop ollama`, then send "
        "these two by hand. Afterwards: `sudo docker compose start ollama`.",
        [
            search(
                "Hybrid with Ollama down -> degraded keyword results",
                "refund",
                tests=check("degraded", "body.degraded === true")
                + check("keyword ranks only", "body.hits.every(h => h.vector_rank === null)"),
            ),
            req(
                "Vector with Ollama down -> 503",
                "POST",
                WS + "/knowledge/search",
                body={"query": "refund", "mode": "vector"},
                tests=status(503),
            ),
        ],
    )


def build() -> dict[str, Any]:
    variables = [
        {"key": "base_url", "value": "http://localhost:8000"},
        {"key": "run_id", "value": ""},
    ]
    return {
        "info": {
            "name": "Desk Ticket (M0-M4)",
            "description": (
                "Every route built so far. Start: db, redis, ollama, `make migrate`, `make dev`, "
                "`make worker`. Then Run collection on folders 0 to 6. Folder 7 is manual.\n\n"
                "Write routes allow 10 requests per minute per user per route; a full run "
                "stays under that. Running it twice within a minute can hit 429 on sign-up "
                "(limited by IP): wait a minute."
            ),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": variables,
        "item": [
            health(),
            users(),
            workspaces(),
            tickets(),
            knowledge(),
            search_folder(),
            search_rules(),
            degraded(),
        ],
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
