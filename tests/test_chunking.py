"""Unit tests for the Markdown chunker. Pure functions, so no database is needed."""

from app.ingestion.chunking import (
    MAX_CHARS,
    OVERLAP_CHARS,
    TARGET_CHARS,
    chunk_document,
    chunk_ticket_answer,
)


def _paragraph(words: int, word: str = "lorem") -> str:
    return " ".join([word] * words) + "."


def test_headings_become_a_path_and_reset_at_higher_levels() -> None:
    body = "\n".join(
        [
            "Intro text.",
            "# Billing",
            "Billing overview.",
            "## Refunds",
            "Refund rules.",
            "### Partial",
            "Partial refund rules.",
            "# Accounts",
            "Account text.",
        ]
    )
    chunks = chunk_document("Help", body)
    assert [(c.heading, c.content) for c in chunks] == [
        ("Help", "Intro text."),
        ("Help > Billing", "Billing overview."),
        ("Help > Billing > Refunds", "Refund rules."),
        ("Help > Billing > Refunds > Partial", "Partial refund rules."),
        # A new top-level heading drops Billing and everything under it.
        ("Help > Accounts", "Account text."),
    ]
    assert [c.position for c in chunks] == [0, 1, 2, 3, 4]


def test_heading_without_text_adds_no_chunk() -> None:
    chunks = chunk_document("Help", "# Empty\n## Child\nChild text.")
    assert [(c.heading, c.content) for c in chunks] == [("Help > Empty > Child", "Child text.")]


def test_hash_lines_inside_code_fences_are_not_headings() -> None:
    body = "# Install\n```bash\n# update the package index\napt update\n\napt install x\n```"
    chunks = chunk_document("Guide", body)
    assert len(chunks) == 1
    assert chunks[0].heading == "Guide > Install"
    # The fence stays whole, including its blank line.
    assert "# update the package index" in chunks[0].content
    assert "apt update\n\napt install x" in chunks[0].content


def test_short_paragraphs_are_packed_into_one_chunk() -> None:
    chunks = chunk_document("Doc", "First.\n\nSecond.\n\nThird.")
    assert len(chunks) == 1
    assert chunks[0].content == "First.\n\nSecond.\n\nThird."


def test_long_sections_split_on_paragraphs_with_overlap() -> None:
    # Each paragraph is about 300 chars, so about five fit in one chunk.
    paragraphs = [_paragraph(50, f"p{index}") for index in range(12)]
    chunks = chunk_document("Doc", "\n\n".join(paragraphs))
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.content) <= MAX_CHARS
    # No paragraph is cut in half: every chunk is made of whole paragraphs.
    for chunk in chunks:
        assert all(part in paragraphs for part in chunk.content.split("\n\n"))
    # Overlap: each chunk after the first starts with the previous chunk's last paragraph.
    for previous, current in zip(chunks, chunks[1:], strict=False):
        last = previous.content.split("\n\n")[-1]
        assert len(last) <= OVERLAP_CHARS
        assert current.content.startswith(last)


def test_paragraph_over_the_limit_splits_on_sentences() -> None:
    sentence = _paragraph(40)  # about 240 chars
    giant = " ".join([sentence] * 20)  # about 4,800 chars, one paragraph
    chunks = chunk_document("Doc", giant)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk.content) <= MAX_CHARS
        # Splits fall between sentences.
        assert chunk.content.endswith(".")


def test_text_with_no_breaks_is_hard_cut() -> None:
    chunks = chunk_document("Doc", "x" * (MAX_CHARS * 2 + 10))
    assert [len(c.content) for c in chunks] == [MAX_CHARS, MAX_CHARS, 10]


def test_target_size_is_respected_for_normal_prose() -> None:
    body = "\n\n".join(_paragraph(30) for _ in range(40))
    for chunk in chunk_document("Doc", body):
        assert len(chunk.content) <= TARGET_CHARS + OVERLAP_CHARS


def test_ticket_answer_chunks_repeat_the_question() -> None:
    body = "\n\n".join(_paragraph(60, f"step{index}") for index in range(10))
    chunks = chunk_ticket_answer("Password reset", "How do I reset my password?", body)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.heading == "Password reset"
        assert chunk.content.startswith("Question: How do I reset my password?\n\nAnswer: ")


def test_hash_covers_heading_and_content() -> None:
    same = chunk_document("Doc", "# A\nText.")[0]
    again = chunk_document("Doc", "# A\nText.")[0]
    moved = chunk_document("Doc", "# B\nText.")[0]
    assert same.content_hash == again.content_hash
    # Same words under a different heading embed differently, so the hash differs.
    assert same.content_hash != moved.content_hash
    assert same.embedding_text == "Doc > A\n\nText."


def test_empty_body_gives_no_chunks() -> None:
    assert chunk_document("Doc", "   \n\n  ") == []
