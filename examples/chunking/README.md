# Chunking examples

Sample Markdown documents for trying the ingestion chunker (`app/ingestion/chunking.py`).
Each file shows different splitting rules.

| File | Chunks | What it shows |
|---|---|---|
| `billing-guide.md` | 7 | Heading paths (`Billing guide > Refunds > Partial refunds`), text before the first heading, an empty `# Refunds` heading that adds no chunk of its own, and a `bash` code block whose `#` comments are not treated as headings. |
| `onboarding-handbook.md` | 5 | One long section (`# Your first week`) split into several chunks on paragraph boundaries. Each chunk starts with the previous chunk's last paragraph (the overlap). |
| `security-policy.md` | 3 | A single paragraph longer than 2,400 characters, split between sentences. The overlap is not added because the carried-over piece would be too long. |
| `api-quickstart.md` | 4 | Python and bash code blocks kept whole, a table, and a `#Heading` with no space, which stays plain text. |

The chunk limits are about 1,600 characters per chunk, at most 2,400 characters, and up to 400 characters of overlap.

## Preview the chunks locally

No database or API needed:

```bash
.venv/bin/python examples/chunking/preview.py                       # every sample
.venv/bin/python examples/chunking/preview.py --full examples/chunking/api-quickstart.md
.venv/bin/python examples/chunking/preview.py path/to/your-doc.md   # your own file
```

## Upload one through the API

Posting as an owner or admin publishes the document straight away and queues an ingestion job. With `make worker` running, the chunks show up a moment later.

```bash
jq -n --arg title "Billing guide" --rawfile body examples/chunking/billing-guide.md \
     '{title: $title, body: $body}' \
  | curl -s -X POST "http://localhost:8000/api/v1/workspaces/$WS/knowledge" \
      -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d @-

curl -s "http://localhost:8000/api/v1/workspaces/$WS/knowledge/$SOURCE_ID/chunks" \
  -H "Authorization: Bearer $TOKEN"
```

The API uses the title you send, so heading paths start with that title. The preview script builds its title from the file name instead.
