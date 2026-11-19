"""Split Markdown knowledge into chunks small enough to embed and retrieve.

Why chunk at all: retrieval returns whole chunks to the LLM. A whole 50-page document
would blow the context window and drown the relevant paragraph; a single sentence
loses the context needed to answer. A few hundred tokens per chunk is the usual
middle ground.

Strategy, in order of preference:
1. Split on Markdown headings, so a chunk never mixes two topics. Each chunk keeps
   its heading path ("Billing > Refunds > Partial refunds") as context.
2. Inside a section, pack whole paragraphs up to TARGET_CHARS.
3. A paragraph longer than MAX_CHARS is split on sentences, and a sentence longer
   than that is cut by characters as a last resort.

Sizes are in characters, not tokens. Tokens depend on the embedding model's
tokenizer, which is chosen in milestone 3. English averages about 4 characters per
token, so TARGET_CHARS of 1,600 is roughly 400 tokens.
"""

import hashlib
import re
from dataclasses import dataclass

TARGET_CHARS = 1_600
MAX_CHARS = 2_400
# When a section spills into a second chunk, the previous chunk's last paragraph is
# repeated at the start of the next one if it is at most this long. That way a
# sentence that depends on the one before it is not cut off from its context.
OVERLAP_CHARS = 400
HEADING_SEPARATOR = " > "

# A Markdown ATX heading: 1-6 '#' then a space, e.g. "## Refunds".
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# Opening or closing line of a fenced code block (``` or ~~~).
_FENCE = re.compile(r"^\s*(```|~~~)")
# A sentence ends at . ! or ? followed by whitespace. Good enough for English prose;
# it will split "e.g. this" too early, which only makes a chunk slightly smaller.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    position: int
    # Heading path inside the source, starting with the source title.
    heading: str
    content: str
    content_hash: str
    # Rough token count (characters / 4) for budgeting until a real tokenizer exists.
    token_estimate: int

    @property
    def embedding_text(self) -> str:
        """The exact text milestone 3 will embed: heading context plus content.

        Including the heading helps a chunk like "Click Save." match a question
        about "changing billing email", which only the heading mentions.
        """
        return f"{self.heading}\n\n{self.content}"


def chunk_hash(heading: str, content: str) -> str:
    """Hash of everything that goes into the embedding. If it is unchanged after an
    edit, the old chunk (and later its embedding) can be kept instead of recomputed."""
    return hashlib.sha256(f"{heading}\n\n{content}".encode()).hexdigest()


def chunk_document(title: str, body: str) -> list[Chunk]:
    pieces: list[tuple[str, str]] = []
    for path, text in _sections(body):
        heading = HEADING_SEPARATOR.join([title.strip(), *path])
        pieces.extend((heading, part) for part in _pack(_paragraphs(text)))
    return _finish(pieces)


def chunk_ticket_answer(title: str, question: str, body: str) -> list[Chunk]:
    """Each chunk repeats the question, because an answer fragment without its question
    ("Yes, after 30 days.") means nothing on its own. Ticket answers are short, so the
    repetition costs little."""
    question = question.strip()
    parts = _pack(_paragraphs(body))
    return _finish([(title.strip(), f"Question: {question}\n\nAnswer: {part}") for part in parts])


def _finish(pieces: list[tuple[str, str]]) -> list[Chunk]:
    return [
        Chunk(
            position=index,
            heading=heading,
            content=content,
            content_hash=chunk_hash(heading, content),
            token_estimate=max(1, (len(heading) + len(content)) // 4),
        )
        for index, (heading, content) in enumerate(pieces)
    ]


def _sections(body: str) -> list[tuple[list[str], str]]:
    """Split Markdown into (heading path, text) pairs.

    The path is a stack: a "## B" after "# A" gives ["A", "B"]; a later "# C" pops
    back to ["C"]. Lines inside code fences are never treated as headings, because
    "# comment" in a shell snippet is not a heading.
    """
    sections: list[tuple[list[str], str]] = []
    stack: list[tuple[int, str]] = []
    lines: list[str] = []
    in_fence = False

    def flush() -> None:
        text = "\n".join(lines).strip()
        if text:
            sections.append(([name for _, name in stack], text))
        lines.clear()

    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            lines.append(line)
            continue
        match = None if in_fence else _HEADING.match(line)
        if match is None:
            lines.append(line)
            continue
        flush()
        level, name = len(match.group(1)), match.group(2).strip()
        # Drop headings at the same or a deeper level; they are no longer in scope.
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, name))
    flush()
    return sections


def _paragraphs(text: str) -> list[str]:
    """Split on blank lines, but keep a fenced code block together as one paragraph
    even if it contains blank lines, so code is not split at its blank lines. A block
    longer than MAX_CHARS is still cut later by _split_long; there is no better place
    to break it."""
    paragraphs: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        if not line.strip() and not in_fence:
            if current:
                paragraphs.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        paragraphs.append("\n".join(current).strip())
    return [p for p in paragraphs if p]


def _pack(paragraphs: list[str]) -> list[str]:
    """Greedily fill chunks with whole paragraphs up to TARGET_CHARS."""
    units: list[str] = []
    for paragraph in paragraphs:
        units.extend(_split_long(paragraph))

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        # +2 for the blank line that joins paragraphs.
        if current and size + 2 + len(unit) > TARGET_CHARS:
            chunks.append("\n\n".join(current))
            tail = current[-1]
            # Carry the last paragraph over as overlap, unless it is long or it would
            # push the next chunk past the hard limit.
            if len(tail) <= OVERLAP_CHARS and len(tail) + 2 + len(unit) <= MAX_CHARS:
                current, size = [tail], len(tail)
            else:
                current, size = [], 0
        current.append(unit)
        size += len(unit) + (2 if len(current) > 1 else 0)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _split_long(paragraph: str) -> list[str]:
    """Break a paragraph over MAX_CHARS into sentence groups, then hard-cut anything
    still too long (a giant table or a line with no punctuation)."""
    if len(paragraph) <= MAX_CHARS:
        return [paragraph]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > MAX_CHARS:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:MAX_CHARS])
            sentence = sentence[MAX_CHARS:]
        if current and len(current) + 1 + len(sentence) > MAX_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        pieces.append(current)
    return pieces
