"""Generate a Postman collection for exploring the seeded demo data by hand.

    make seed-reset                # fresh demo data first (see app/seed/)
    python postman/build_demo.py   # writes postman/kettle-demo.postman_collection.json

Unlike desk-ticket.postman_collection.json, which builds its own users and data from
scratch, this one logs in as the seeded people (Maya the owner, Priya the agent, Sam
the member, Rosa from the other company) and works with what `make seed` loaded.

Folders 1 and 5 (search, isolation) only read, so they can be sent any number of times.
Folders 2 to 4 change data (approve a draft, file and resolve a ticket). A second run
of those gets 409s, because the draft is already approved: `make seed-reset` first.
"""

import json
from pathlib import Path
from typing import Any

# Run as a script from the project root, Python puts this file's folder (postman/) on
# sys.path, so the shared helpers import from build.py directly.
from build import API, check, folder, note, req, save, search, status, wait_for_ingestion

OUT = Path(__file__).resolve().parent / "kettle-demo.postman_collection.json"

# The seeded people this collection logs in as: key -> email. The key is also the token
# variable name ({{maya_token}}) used by req(as_user=...).
PEOPLE = {
    "maya": "maya@kettle.test",  # Kettle owner
    "omar": "omar@kettle.test",  # Kettle admin
    "priya": "priya@kettle.test",  # Kettle agent
    "sam": "sam@kettle.test",  # Kettle member (files tickets)
    "rosa": "rosa@harbor.test",  # Harbor Books owner: the other tenant
}
# The helpers in build.py read {{workspace_id}}; here it is Kettle's id.
KETTLE = API + "/workspaces/{{workspace_id}}"
HARBOR = API + "/workspaces/{{harbor_id}}"


def find(var: str, list_expr: str, match: str, what: str) -> str:
    """Test script: find one item in a list response and save its id."""
    # Wrapped in { } so two lookups in one script each get their own `found`.
    return (
        f"{{ const found = {list_expr}.find(x => {match});\n"
        f'pm.test("found {what}", () => pm.expect(found, "run make seed-reset?").to.exist);\n'
        f"if (found) {{ pm.collectionVariables.set({json.dumps(var)}, found.id); }} }}\n"
    )


def by_title(var: str, title: str) -> str:
    return find(var, "pm.response.json().items", f"x.title === {json.dumps(title)}", title)


def by_subject(var: str, subject: str) -> str:
    return find(var, "pm.response.json().items", f"x.subject === {json.dumps(subject)}", subject)


# A list of tickets is easier to read as a table in the Postman Console.
SHOW_TICKETS = """
console.table(pm.response.json().items.map(t => ({
  subject: t.subject, status: t.status, priority: t.priority, category: t.category,
})));
"""


def log_in() -> dict[str, Any]:
    items = [
        req(
            f"Log in {key}",
            "POST",
            f"{API}/auth/token",
            as_user=None,
            body={"email": email, "password": "{{password}}"},
            tests=status(200) + save(f"{key}_token", "pm.response.json().access_token"),
        )
        for key, email in PEOPLE.items()
    ]
    items += [
        req(
            "Priya: who am I (saves priya_id)",
            "GET",
            f"{API}/users/me",
            as_user="priya",
            tests=status(200) + save("priya_id", "pm.response.json().id"),
        ),
        req(
            "Maya: find the Kettle workspace",
            "GET",
            f"{API}/workspaces",
            as_user="maya",
            tests=status(200)
            + find(
                "workspace_id", "pm.response.json().items", 'x.slug === "kettle-demo"', "Kettle"
            ),
        ),
        req(
            "Rosa: find the Harbor workspace",
            "GET",
            f"{API}/workspaces",
            as_user="rosa",
            tests=status(200)
            + find("harbor_id", "pm.response.json().items", 'x.slug === "harbor-demo"', "Harbor"),
        ),
        req(
            "Kettle members",
            "GET",
            f"{KETTLE}/members",
            as_user="maya",
            tests=status(200) + check("6 people", "pm.response.json().total === 6"),
        ),
    ]
    return folder(
        "0. Log in (run this first)",
        "Logs in as the seeded people and finds both workspaces. Every other folder "
        "needs the tokens and ids saved here.",
        items,
    )


def search_playground() -> dict[str, Any]:
    def hint(text: str) -> str:
        return note(text)

    return folder(
        "1. Search playground (read-only)",
        "Edit the query in any request body and Send. Open View > Show Postman Console "
        "for the result table: kw and vec show which search found each chunk.",
        [
            search("keyword: exact error code K-415", "K-415", "keyword", as_user="maya"),
            search("vector: my reader is locked", "my reader is locked", "vector", as_user="maya"),
            search(
                "hybrid: weekend money hasn't arrived",
                "money from the weekend hasn't arrived",
                as_user="maya",
            ),
            search(
                "vector: someone changed our bank details",
                "someone changed our bank details",
                "vector",
                as_user="maya",
            ),
            search(
                "hybrid: wifi is down, can we take cards?",
                "our internet is down can we still take card payments",
                as_user="priya",
            ),
            search(
                "hybrid: croissants come back after sold out",
                "croissants come back the next morning after we mark them sold out",
                as_user="priya",
            ),
            search(
                "hybrid: kitchen screen not showing orders",
                "kitchen screen stopped showing orders",
                as_user="priya",
            ),
            search(
                "hybrid: refund to a new card (answer still pending)",
                "refund to a new card",
                as_user="maya",
                tests=hint(
                    "The 'Refund to a replacement card' answer is pending review, so it "
                    "must not be listed. Approve it in folder 2 to see it appear."
                ),
            ),
            search(
                "vector: not in the knowledge base",
                "can we export sales to accounting software",
                "vector",
                as_user="maya",
                tests=hint(
                    "No article covers this. Compare these scores with a good match "
                    "(about 0.7): that gap is what milestone 5 measures."
                ),
            ),
            search(
                "keyword vs hybrid: same question",
                "refund to a new card",
                "keyword",
                as_user="maya",
                tests=hint(
                    "Keyword search needs every word, so a full sentence often finds nothing."
                ),
            ),
        ],
    )


def review_queue() -> dict[str, Any]:
    source = KETTLE + "/knowledge/{{%s}}"
    return folder(
        "2. Review queue (changes data)",
        "Maya reviews what agents wrote. Approving makes the worker chunk and embed it, "
        "and then it shows up in search.",
        [
            req(
                "Pending review",
                "GET",
                f"{KETTLE}/knowledge?status=pending_review",
                as_user="maya",
                tests=status(200)
                + by_title("gift_cards_id", "Gift cards")
                + by_title("pending_answer_id", "Refund to a replacement card"),
            ),
            req(
                "Rejected (open one to read the note)",
                "GET",
                f"{KETTLE}/knowledge?status=rejected",
                as_user="maya",
                tests=status(200) + by_title("loyalty_id", "Loyalty points"),
            ),
            req(
                "Read the rejected loyalty doc",
                "GET",
                source % "loyalty_id",
                as_user="maya",
                tests=status(200) + check("has a review note", "!!pm.response.json().review_note"),
            ),
            search(
                "Before: gift cards are not searchable",
                "do gift cards expire",
                as_user="maya",
                tests='pm.test("no gift card hits", () => pm.expect(body.hits.some('
                'h => h.source_title === "Gift cards")).to.be.false);\n',
            ),
            req(
                "Priya tries to approve her own draft -> 403",
                "POST",
                source % "gift_cards_id" + "/approve",
                as_user="priya",
                tests=status(403),
            ),
            req(
                "Maya approves gift cards",
                "POST",
                source % "gift_cards_id" + "/approve",
                as_user="maya",
                tests=status(200),
            ),
            req(
                "Omar (admin) approves the refund answer",
                "POST",
                source % "pending_answer_id" + "/approve",
                as_user="omar",
                tests=status(200),
            ),
            wait_for_ingestion("Wait for gift cards ingestion", "gift_cards_id", as_user="maya"),
            wait_for_ingestion(
                "Wait for refund answer ingestion", "pending_answer_id", as_user="maya"
            ),
            search(
                "After: gift cards are searchable",
                "do gift cards expire",
                as_user="maya",
                tests='pm.test("gift card hit", () => pm.expect(body.hits.some('
                'h => h.source_title === "Gift cards")).to.be.true);\n',
            ),
            search(
                "After: the refund answer shows up",
                "refund to a new card",
                as_user="maya",
                tests='pm.test("answer is listed", () => pm.expect(body.hits.some('
                'h => h.source_title === "Refund to a replacement card")).to.be.true);\n',
            ),
            req(
                "Chunks of the card reader article",
                "GET",
                f"{KETTLE}/knowledge?kind=document&limit=100",
                as_user="maya",
                tests=status(200) + by_title("card_readers_id", "Card readers"),
            ),
            req(
                "See its chunks",
                "GET",
                source % "card_readers_id" + "/chunks",
                as_user="maya",
                tests=status(200),
            ),
        ],
    )


def tickets() -> dict[str, Any]:
    ticket = KETTLE + "/tickets/{{%s}}"
    return folder(
        "3. Tickets (as Priya and Sam)",
        "Browse the seeded tickets, then follow one new ticket from Sam through to resolved.",
        [
            req(
                "All tickets",
                "GET",
                f"{KETTLE}/tickets?limit=100",
                as_user="priya",
                tests=status(200)
                + SHOW_TICKETS
                + check("22+ tickets", "pm.response.json().total >= 22"),
            ),
            req(
                "Urgent ones",
                "GET",
                f"{KETTLE}/tickets?priority=urgent",
                as_user="priya",
                tests=status(200) + SHOW_TICKETS,
            ),
            req(
                "Open billing",
                "GET",
                f"{KETTLE}/tickets?status=open&category=billing",
                as_user="priya",
                tests=status(200) + SHOW_TICKETS,
            ),
            req(
                "Assigned to Priya",
                "GET",
                f"{KETTLE}/tickets?assigned_to={{{{priya_id}}}}",
                as_user="priya",
                tests=status(200) + SHOW_TICKETS,
            ),
            req(
                "Waiting on the customer (pending)",
                "GET",
                f"{KETTLE}/tickets?status=pending",
                as_user="priya",
                tests=status(200) + SHOW_TICKETS,
            ),
            req(
                "Find the K-415 ticket",
                "GET",
                f"{KETTLE}/tickets?limit=100",
                as_user="priya",
                tests=status(200)
                + by_subject("k415_ticket_id", "Reader shows K-415 and won't take payments"),
            ),
            req(
                "Its conversation (replies + internal note)",
                "GET",
                ticket % "k415_ticket_id" + "/messages",
                as_user="priya",
                tests=status(200),
            ),
            req(
                "Its history (events)",
                "GET",
                ticket % "k415_ticket_id" + "/events",
                as_user="priya",
                tests=status(200),
            ),
            req(
                "Sam files a new ticket",
                "POST",
                f"{KETTLE}/tickets",
                as_user="sam",
                body={
                    "subject": "Reader stuck blinking amber",
                    "description": "Our K2 has been blinking amber for 20 minutes. Can we "
                    "unplug it?",
                    "priority": "high",
                    "category": "technical",
                },
                tests=status(201) + save("new_ticket_id", "pm.response.json().id"),
            ),
            req(
                "Search for an answer before replying",
                "POST",
                f"{KETTLE}/knowledge/search",
                as_user="priya",
                body={"query": "reader blinking amber", "limit": 3},
                tests=status(200)
                + note("The status-lights section should be first: amber = updating firmware."),
            ),
            req(
                "Priya takes it",
                "PATCH",
                ticket % "new_ticket_id",
                as_user="priya",
                body={"assigned_to": "{{priya_id}}"},
                tests=status(200),
            ),
            req(
                "Priya replies",
                "POST",
                ticket % "new_ticket_id" + "/messages",
                as_user="priya",
                body={
                    "body": "Blinking amber means a firmware update. Please don't turn it off;"
                    " it finishes on its own. If it fails you'll see K-310.",
                    "message_type": "reply",
                },
                tests=status(201),
            ),
            req(
                "Sam can't post a system message -> 422",
                "POST",
                ticket % "new_ticket_id" + "/messages",
                as_user="sam",
                body={"body": "x", "message_type": "system"},
                tests=status(422),
            ),
            req(
                "Priya resolves it",
                "PATCH",
                ticket % "new_ticket_id",
                as_user="priya",
                body={"status": "resolved"},
                tests=status(200),
            ),
            req(
                "Its history now",
                "GET",
                ticket % "new_ticket_id" + "/events",
                as_user="priya",
                tests=status(200),
            ),
        ],
    )


def ticket_to_knowledge() -> dict[str, Any]:
    source = KETTLE + "/knowledge/{{%s}}"
    return folder(
        "4. Turn the new ticket into knowledge (changes data)",
        "Priya writes a reusable answer from the ticket resolved in folder 3; Maya "
        "approves it; it becomes searchable.",
        [
            req(
                "Priya proposes an answer",
                "POST",
                KETTLE + "/tickets/{{new_ticket_id}}/knowledge",
                as_user="priya",
                body={
                    "title": "Reader blinking amber",
                    "question": "A card reader has been blinking amber for a long time. Can "
                    "the merchant unplug it?",
                    "body": "Blinking amber means a firmware update is running. Do not turn "
                    "the reader off: it finishes on its own. If the update fails, the reader "
                    "shows K-310; keep it charging and retry from Settings, Hardware.",
                },
                tests=status(201)
                + save("amber_answer_id", "pm.response.json().id")
                + check("waits for review", 'pm.response.json().status === "pending_review"'),
            ),
            req(
                "Propose again -> 409",
                "POST",
                KETTLE + "/tickets/{{new_ticket_id}}/knowledge",
                as_user="priya",
                body={"title": "dup", "question": "dup", "body": "dup"},
                tests=status(409),
            ),
            req(
                "Maya approves",
                "POST",
                source % "amber_answer_id" + "/approve",
                as_user="maya",
                tests=status(200),
            ),
            wait_for_ingestion(
                "Wait for the answer's ingestion", "amber_answer_id", as_user="maya"
            ),
            search(
                "Search finds the new answer",
                "reader light blinking amber",
                as_user="maya",
                tests='pm.test("new answer listed", () => pm.expect(body.hits.some('
                'h => h.source_title === "Reader blinking amber")).to.be.true);\n',
            ),
        ],
    )


def isolation() -> dict[str, Any]:
    return folder(
        "5. Tenant isolation (read-only)",
        "Rosa runs Harbor Books, a different company. Neither side can see the other's data.",
        [
            search(
                "Rosa searches Harbor: only Harbor's policy",
                "how many days to return",
                as_user="rosa",
                workspace="harbor_id",
                tests=check(
                    "only Harbor", 'body.hits.every(h => h.source_title === "Returns and refunds")'
                ),
            ),
            search(
                "Maya searches Kettle: never Harbor's 30 days",
                "30 days return policy",
                as_user="maya",
                tests=check(
                    "no Harbor text",
                    'body.hits.every(h => !h.content.includes("30 days of delivery"))',
                ),
            ),
            req(
                "Rosa searches Kettle -> 404",
                "POST",
                f"{KETTLE}/knowledge/search",
                as_user="rosa",
                body={"query": "refund"},
                tests=status(404),
            ),
            req(
                "Maya reads Harbor's tickets -> 404",
                "GET",
                f"{HARBOR}/tickets",
                as_user="maya",
                tests=status(404),
            ),
            req(
                "Sam (member) searches -> 403",
                "POST",
                f"{KETTLE}/knowledge/search",
                as_user="sam",
                body={"query": "refund"},
                tests=status(403),
            ),
            req(
                "Sam lists knowledge -> 403",
                "GET",
                f"{KETTLE}/knowledge",
                as_user="sam",
                tests=status(403),
            ),
        ],
    )


def build() -> dict[str, Any]:
    return {
        "info": {
            "name": "Kettle demo (seeded data)",
            "description": __doc__,
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [
            {"key": "base_url", "value": "http://localhost:8000"},
            {"key": "password", "value": "demo-password"},
        ],
        "item": [
            log_in(),
            search_playground(),
            review_queue(),
            tickets(),
            ticket_to_knowledge(),
            isolation(),
        ],
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {OUT.name}")
