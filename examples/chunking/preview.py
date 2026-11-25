"""Print the chunks the ingestion worker would build from Markdown files.

Runs the same `chunk_document` the worker uses, with no database or API involved, so
you can see how a document will be split before uploading it.

Usage (from the project root):
    .venv/bin/python examples/chunking/preview.py                  # every .md file here
    .venv/bin/python examples/chunking/preview.py path/to/doc.md   # specific files
    .venv/bin/python examples/chunking/preview.py --full doc.md    # print whole chunks
"""

import argparse
import sys
from pathlib import Path

# Running a file directly puts its own folder on sys.path, not the project root, so
# `import app` would fail. Adding the root (two levels up) makes the import work
# without installing the project as a package.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.ingestion.chunking import chunk_document  # noqa: E402  (needs the path above)
from app.seed.data import parse_doc  # noqa: E402

HERE = Path(__file__).resolve().parent
PREVIEW_CHARS = 100


def show(path: Path, *, full: bool) -> None:
    # The title becomes the first part of every heading path. The file name stands in
    # for the title the API caller would send.
    title = path.stem.replace("-", " ").capitalize()
    body = path.read_text(encoding="utf-8")
    # Seed documents (app/seed/knowledge/) start with a --- header. Use its title and
    # chunk only the body, exactly as `make seed` will.
    if body.startswith("---\n"):
        doc = parse_doc(path)
        title, body = doc.title, doc.body
    chunks = chunk_document(title, body)
    print(f"\n=== {path.name}: {len(chunks)} chunks (title {title!r})")
    for chunk in chunks:
        print(
            f"\n--- #{chunk.position}  {chunk.heading}  "
            f"[{len(chunk.content)} chars, ~{chunk.token_estimate} tokens, "
            f"hash {chunk.content_hash[:8]}]"
        )
        text = chunk.content
        if not full and len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS] + " ..."
        print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=Path, help="Markdown files (default: this folder)")
    parser.add_argument("--full", action="store_true", help="print whole chunks, not previews")
    args = parser.parse_args()
    for path in args.files or sorted(HERE.glob("*.md")):
        if path.name == "README.md":  # the folder's own notes are not a sample
            continue
        show(path, full=args.full)


if __name__ == "__main__":
    main()
