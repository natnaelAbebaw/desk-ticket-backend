# Seed knowledge documents

`make seed` loads every `.md` file in this folder as a knowledge document. There is one subfolder per workspace, named after its slug:

| Folder | Workspace |
|---|---|
| `kettle-demo/` | Kettle & Co |
| `harbor-demo/` | Harbor Books |

## Add a document

Create a file such as `kettle-demo/09-wifi-setup.md`. Files load in name order, and the number prefix sets that order. Start the file with a header:

```markdown
---
title: Wi-Fi setup
author: owner
---

# Wi-Fi setup

Your Markdown here. Headings become chunk boundaries, so structure matters.
```

| Header key | Required | Meaning |
|---|---|---|
| `title` | yes | The document's title |
| `author` | no | Who adds it, using a person key from `app/seed/data.py` (`owner`, `admin`, `priya`, `leo`, …). The default is `owner`. Owners and admins publish straight away. An agent's document waits for review |
| `review` | no | For an agent's document only: `approve` or `reject`. Leave it out and the document stays pending |
| `note` | no | The rejection note, used with `review: reject` |

Lines starting with `#` inside the header are comments.

Then reload. Only approved documents are chunked, embedded and searchable.

```bash
make seed-reset
```

To preview how a file will be chunked first, run `python examples/chunking/preview.py <file>`.

**Rules for new documents:**
- Two documents with identical text are rejected as duplicates (409).
- Keep everything fictional. These files are committed to the repository.
