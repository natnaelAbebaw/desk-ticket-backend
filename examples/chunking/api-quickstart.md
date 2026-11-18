This page shows how to call the Acme API from a script.

# Authentication

Every request needs an API key in the Authorization header. Create keys under Settings > API keys. A key belongs to one workspace and can be revoked at any time.

```bash
# Keys start with ak_ and are shown only once, so store yours right away.
export ACME_KEY=ak_live_example

curl https://api.acme.example/v1/me \
  -H "Authorization: Bearer $ACME_KEY"
```

# Listing invoices

```python
# The client pages through results for you.
import acme

client = acme.Client(api_key="ak_live_example")

# Only invoices from the workspace that owns the key are returned.
for invoice in client.invoices.list(status="open"):
    print(invoice.id, invoice.amount_due)
```

The response includes at most 100 invoices per page. Pass `starting_after` with the last id to fetch the next page.

## Errors

| Status | Meaning | What to do |
|--------|---------|------------|
| 401 | Missing or revoked key | Create a new key |
| 404 | Invoice is in another workspace | Check which key you used |
| 429 | Too many requests | Wait for the Retry-After seconds |

#Not a heading, because there is no space after the hash.
