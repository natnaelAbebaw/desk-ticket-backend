"""Fictional demo data: two companies, their staff, help articles, and support tickets.

Kettle & Co is the main workspace. It sells point-of-sale (POS) and online ordering to
cafés, so its tickets are about card readers, payouts, refunds, menus, and printers.
Harbor Books is a second, smaller workspace. It exists to test tenant isolation: it has
its own refund policy (30 days, unlike Kettle's 120), so a Kettle search that ever
returns "30 days" has leaked across tenants.

Everything here is made up. Emails use the reserved .test domain, which can never be a
real address. Error codes (K-101 and so on) are invented so that keyword search has
exact tokens to find.

Some open tickets on purpose have no answer in the knowledge base (exports, a feature
request). Milestone 6 must reply "I don't know" to those instead of making something up.
"""

from dataclasses import dataclass, field
from pathlib import Path

# Every demo user logs in with this password.
PASSWORD = "demo-password"


@dataclass(frozen=True)
class Person:
    key: str  # short name used in this file to refer to the person
    name: str
    email: str
    role: str  # role in their workspace: owner, admin, agent, member


@dataclass(frozen=True)
class Doc:
    title: str
    body: str  # Markdown
    # Who adds it. Owners and admins publish straight to approved; an agent's document
    # waits for review. review is what happens next: None (leave it), "approve", "reject".
    author: str = "owner"
    review: str | None = None
    note: str = ""


@dataclass(frozen=True)
class Answer:
    """A reusable question and answer written from a resolved ticket."""

    title: str
    question: str
    body: str
    review: str  # "approve", "reject", or "pending" (left for a reviewer)
    note: str = ""


@dataclass(frozen=True)
class Ticket:
    subject: str
    description: str
    requester: str
    category: str
    priority: str
    status: str = "open"
    assignee: str | None = None
    # (author key, message type, body), in order.
    messages: list[tuple[str, str, str]] = field(default_factory=list)
    answer: Answer | None = None


@dataclass(frozen=True)
class Workspace:
    name: str
    slug: str
    people: list[Person]
    docs: list[Doc]
    tickets: list[Ticket]


# Knowledge documents live as Markdown files, one folder per workspace slug:
#
#     app/seed/knowledge/<slug>/<NN-name>.md
#
# Add, edit, or delete files there before `make seed`; no Python change needed. Files
# load in name order, so the NN- prefix sets the order. Each file starts with a small
# header between --- lines (a simplified "front matter"):
#
#     ---
#     title: Card readers          required
#     author: owner                optional: the person key below; default owner
#     review: approve              optional: approve or reject (needs an agent author)
#     note: Why it was rejected    optional: the rejection note
#     ---
#
# Lines starting with # inside the header are comments.
KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
_DOC_KEYS = {"title", "author", "review", "note"}


def parse_doc(path: Path) -> Doc:
    text = path.read_text()
    # Split into ["", header, body]. maxsplit=2 keeps any later --- (a Markdown
    # horizontal rule) inside the body.
    parts = text.split("---\n", 2)
    if not text.startswith("---\n") or len(parts) != 3:
        raise ValueError(f"{path}: must start with a --- header block")
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or key not in _DOC_KEYS:
            raise ValueError(f"{path}: bad header line {line!r}; keys are {sorted(_DOC_KEYS)}")
        meta[key] = value.strip()
    if not meta.get("title"):
        raise ValueError(f"{path}: the header needs a title")
    review = meta.get("review") or None
    if review not in (None, "approve", "reject"):
        raise ValueError(f"{path}: review must be approve or reject, not {review!r}")
    return Doc(
        title=meta["title"],
        body=parts[2].lstrip("\n"),
        author=meta.get("author", "owner"),
        review=review,
        note=meta.get("note", ""),
    )


def load_docs(slug: str) -> list[Doc]:
    return [parse_doc(path) for path in sorted((KNOWLEDGE_DIR / slug).glob("*.md"))]


# ============================================================== Kettle & Co

KETTLE_PEOPLE = [
    Person("owner", "Maya Okafor", "maya@kettle.test", "owner"),
    Person("admin", "Omar Haddad", "omar@kettle.test", "admin"),
    Person("priya", "Priya Raman", "priya@kettle.test", "agent"),
    Person("leo", "Leo Brandt", "leo@kettle.test", "agent"),
    # Members file tickets on behalf of the cafés they look after.
    Person("sam", "Sam Whitfield", "sam@kettle.test", "member"),
    Person("june", "June Park", "june@kettle.test", "member"),
]

KETTLE_TICKETS = [
    # ----------------------------------------------------------- resolved, with answers
    Ticket(
        "Reader shows K-415 and won't take payments",
        "Our K2 at the Elm Street café beeped and now shows K-415. Nothing works on it. "
        "Lunch rush is in an hour, what do we do?",
        requester="sam",
        category="technical",
        priority="urgent",
        status="resolved",
        assignee="priya",
        messages=[
            (
                "priya",
                "reply",
                "K-415 means the reader detected tampering and locked itself. "
                "It can't be unlocked remotely. I've ordered a free replacement, arriving "
                "tomorrow. Today you can take payments on the K2 Go if you have one, or "
                "with tap to pay on the POS tablet.",
            ),
            (
                "priya",
                "internal_note",
                "Warranty replacement RMA-5521. Old unit to be returned in the prepaid box.",
            ),
            ("sam", "reply", "Using tap to pay on the tablet for now, thanks."),
        ],
        answer=Answer(
            "Reader locked with K-415",
            "A card reader shows K-415 and refuses payments. Can it be fixed?",
            "K-415 means the reader detected tampering and locked itself for security. "
            "It cannot be unlocked remotely. Order a free warranty replacement and, in "
            "the meantime, take payments with tap to pay on the POS tablet or another "
            "reader.",
            review="approve",
        ),
    ),
    Ticket(
        "Friday sales not in my bank yet",
        "We had a big Friday and the money isn't in the bank on Monday. Is something "
        "wrong with our payouts?",
        requester="june",
        category="billing",
        priority="medium",
        status="resolved",
        assignee="leo",
        messages=[
            (
                "leo",
                "reply",
                "Nothing is wrong: sales arrive 2 business days later, so "
                "Friday's sales land on Tuesday. If you need money faster, instant payouts "
                "arrive in 30 minutes for a 1% fee.",
            ),
            ("june", "reply", "Got it, it arrived this morning."),
        ],
        answer=Answer(
            "When do weekend sales arrive?",
            "A merchant's Friday sales are not in their bank account on Monday.",
            "Payouts arrive 2 business days after the sale, so Friday sales land on "
            "Tuesday and weekend sales on Tuesday or Wednesday. Instant payouts arrive "
            "within 30 minutes for 1 percent of the amount, once the location has "
            "processed payments for 30 days.",
            review="approve",
        ),
    ),
    Ticket(
        "Oat milk option missing on delivery app",
        "We added 12 milk options to our latte but the delivery app only shows some of "
        "them. Oat and soy are missing.",
        requester="sam",
        category="technical",
        priority="medium",
        status="closed",
        assignee="priya",
        messages=[
            (
                "priya",
                "reply",
                "Delivery partners show at most 10 options per modifier "
                "group, so options 11 and 12 are dropped on their side. Move oat and soy "
                "higher in the list, or split the group into two.",
            ),
            ("sam", "reply", "Moved them to the top, they show now."),
        ],
        answer=Answer(
            "Modifier options missing on delivery partners",
            "Some modifier options do not show on the delivery partner's app.",
            "Delivery partners show at most 10 options per modifier group and drop the "
            "rest. Reorder the group so the important options come first, or split it "
            "into two groups, then publish the menu again.",
            review="approve",
        ),
    ),
    Ticket(
        "Customer asking for refund to a new card",
        "A regular lost her card and wants yesterday's refund on her new one. Can we do "
        "that from the dashboard?",
        requester="june",
        category="billing",
        priority="low",
        status="resolved",
        assignee="leo",
        messages=[
            (
                "leo",
                "reply",
                "Refunds can only go back to the card that was charged. "
                "Her bank forwards it to the replacement card automatically.",
            ),
        ],
        # Waiting for review, so it must not show up in search yet.
        answer=Answer(
            "Refund to a replacement card",
            "Can a refund be sent to a customer's new card?",
            "No. Refunds always go to the original card; the bank moves the money to the "
            "replacement card.",
            review="pending",
        ),
    ),
    Ticket(
        "Owner locked out after losing phone",
        "I'm the owner of Bean There. Lost my phone and can't get past two-step "
        "verification. Please just turn it off for my account.",
        requester="sam",
        category="account",
        priority="high",
        status="resolved",
        assignee="priya",
        messages=[
            (
                "priya",
                "reply",
                "For your security we can't turn it off over chat. I've "
                "sent a link to book a short video call where we check your photo ID; we "
                "reset it on the call.",
            ),
            (
                "priya",
                "internal_note",
                "Request came from the owner's email. Still requires the ID call per policy.",
            ),
            ("sam", "reply", "Done, all sorted on the call."),
        ],
        answer=Answer(
            "Owner lost their two-step verification phone",
            "An owner lost their phone and asks support to disable two-step verification.",
            "Never reset it over email or chat. Book a video call, check the owner's "
            "photo ID against the account, and reset two-step verification on the call.",
            review="approve",
        ),
    ),
    Ticket(
        "Refund the whole order to cash please",
        "Customer wants their card payment refunded in cash. Can Kettle send cash?",
        requester="june",
        category="billing",
        priority="low",
        status="closed",
        assignee="leo",
        messages=[
            (
                "leo",
                "reply",
                "Kettle refunds only to the card. A cash refund comes out of "
                "your till and is your decision.",
            ),
        ],
        # Rejected: the wording would mislead (Kettle never "sends cash").
        answer=Answer(
            "Cash refunds",
            "Can Kettle send a cash refund?",
            "Yes, Kettle can send cash for refunds on request.",
            review="reject",
            note="Wrong: Kettle never refunds in cash. Rewrite from the refunds article.",
        ),
    ),
    # ----------------------------------------------------------- resolved, no answer
    Ticket(
        "Receipt printer stopped after power cut",
        "Storm last night, power came back but the printer won't print. Light is green.",
        requester="sam",
        category="technical",
        priority="high",
        status="resolved",
        assignee="leo",
        messages=[
            (
                "leo",
                "reply",
                "Can you hold the feed button while turning the printer on "
                "and tell me the IP address on the test slip?",
            ),
            ("sam", "reply", "It says 192.168.1.57"),
            (
                "leo",
                "reply",
                "The POS still had .42 saved: the router gave the printer a "
                "new address after the power cut. I updated it. Reserve the address on "
                "your router so it doesn't happen again.",
            ),
        ],
    ),
    # ----------------------------------------------------------- pending
    Ticket(
        "Payout on hold, email asks for documents",
        "Got an email that our payout is on hold. We just had a catering order, that's "
        "why volume went up. What do you need?",
        requester="june",
        category="billing",
        priority="high",
        status="pending",
        assignee="leo",
        messages=[
            (
                "leo",
                "reply",
                "The risk team needs a recent bank statement and the "
                "catering invoice. Please upload them from the link in the email.",
            ),
            ("leo", "internal_note", "Assigned to risk queue. Do not promise a date."),
        ],
    ),
    Ticket(
        "Chargeback for a 6 dollar coffee",
        "Got a chargeback email for 6 USD. Is it even worth answering?",
        requester="sam",
        category="billing",
        priority="low",
        status="pending",
        assignee="priya",
        messages=[
            (
                "priya",
                "reply",
                "Yes, please respond: you have 7 days, and you get the "
                "15 USD fee back if you win. Too many lost chargebacks can also lead to "
                "payout holds.",
            ),
        ],
    ),
    Ticket(
        "Menu sync failing for delivery partner",
        "Dashboard has a red banner saying the delivery partner rejected our menu.",
        requester="june",
        category="technical",
        priority="medium",
        status="pending",
        assignee="priya",
        messages=[
            (
                "priya",
                "reply",
                "The banner says 'image too large' for Blueberry Muffin. "
                "Photos must be under 5 MB. Can you upload a smaller one?",
            ),
        ],
    ),
    # ----------------------------------------------------------- open, covered by docs
    Ticket(
        "K-101 on the K2 Go every morning",
        "Every morning the handheld says K-101 and we have to re-pair it.",
        requester="sam",
        category="technical",
        priority="medium",
        assignee="priya",
    ),
    Ticket(
        "Card declined but customer says card is fine",
        "Reader shows K-207 for one customer. They say the card works everywhere else. "
        "Can you approve it from your side?",
        requester="june",
        category="technical",
        priority="low",
    ),
    Ticket(
        "Can we take payments when the wifi is down?",
        "Our internet goes down a few times a month. Is there an offline mode?",
        requester="sam",
        category="general",
        priority="medium",
    ),
    Ticket(
        "Why was I charged a fee on a refund?",
        "Refunded a 20 USD order and the fee wasn't fully returned.",
        requester="june",
        category="billing",
        priority="low",
        assignee="leo",
    ),
    Ticket(
        "Kitchen screen not showing new orders",
        "The KDS stopped showing orders since we changed the wifi. POS works fine.",
        requester="sam",
        category="technical",
        priority="urgent",
        assignee="leo",
        messages=[
            (
                "leo",
                "internal_note",
                "Probably the new network is a guest network. Waiting for the café to confirm.",
            ),
        ],
    ),
    Ticket(
        "Cashier locked out of the POS",
        "One of our cashiers can't log in, says locked.",
        requester="june",
        category="account",
        priority="medium",
    ),
    Ticket(
        "Someone changed our bank account?",
        "Got an email that the payout bank account was changed. None of us did this!",
        requester="sam",
        category="account",
        priority="urgent",
        assignee="priya",
        messages=[
            (
                "priya",
                "internal_note",
                "Possible account takeover. Payouts are already "
                "paused for 3 days after a bank change. Escalating to security.",
            ),
        ],
    ),
    Ticket(
        "How do tips get paid to staff?",
        "Are card tips in the same payout as sales?",
        requester="june",
        category="billing",
        priority="low",
    ),
    Ticket(
        "Croissants keep coming back after we 86 them",
        "We mark croissants sold out at 2pm and next morning they're available again.",
        requester="sam",
        category="technical",
        priority="low",
    ),
    # ----------------------------------------------------------- open, NOT in the docs
    # Milestone 6 must answer "I don't know" / hand off to a person for these.
    Ticket(
        "Export sales to our accounting software",
        "Can we export daily sales to Ledgerly automatically?",
        requester="june",
        category="feature_request",
        priority="low",
    ),
    Ticket(
        "Dark mode for the POS",
        "Our evening staff would love a dark mode on the tablet.",
        requester="sam",
        category="feature_request",
        priority="low",
    ),
    Ticket(
        "Reports page shows yesterday's totals twice",
        "The daily report lists Tuesday twice and Wednesday is missing.",
        requester="june",
        category="bug",
        priority="high",
        assignee="leo",
    ),
]

# ============================================================== Harbor Books

HARBOR_PEOPLE = [
    Person("owner", "Rosa Delgado", "rosa@harbor.test", "owner"),
    Person("tom", "Tom Ilsley", "tom@harbor.test", "agent"),
]

HARBOR_TICKETS = [
    Ticket(
        "Return a book I bought as a gift",
        "Got two copies of the same novel. Can I return one?",
        requester="owner",
        category="billing",
        priority="low",
    ),
    Ticket(
        "Signed copy arrived damaged",
        "The dust jacket of my signed first edition is torn.",
        requester="owner",
        category="general",
        priority="high",
        assignee="tom",
    ),
]

WORKSPACES = [
    Workspace(
        "Kettle & Co", "kettle-demo", KETTLE_PEOPLE, load_docs("kettle-demo"), KETTLE_TICKETS
    ),
    Workspace(
        "Harbor Books",
        "harbor-demo",
        HARBOR_PEOPLE,
        load_docs("harbor-demo"),
        HARBOR_TICKETS,
    ),
]
