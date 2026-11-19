# Gumroad → Airtable sync, with proof

Moves Gumroad sales into an Airtable table, and then checks they actually arrived.

You said a previous freelancer *"setup a workflow that scammed me into thinking the
automation worked, it in fact did not work."*

That is the specific thing this is built against. **A sync that only reports what it
sent cannot tell the difference between working and broken.** So this one reads the
rows back out of Airtable and compares them to what Gumroad sent. If a row didn't
land, the run fails loudly and names it.

---

## What it collects

| Field | From Gumroad |
|---|---|
| Sale ID | sale id — also the dedupe key |
| Email | customer email, lowercased |
| Date | transaction date |
| Amount | price, converted from cents to dollars |
| Product ID / Product | product id and name |

## Run it right now, no credentials

```bash
python3 gumroad_to_airtable.py --dry-run
```

That runs the whole flow — fetch, map, dedupe, push, verify — against built-in
fake data. Nothing to configure. You'll see this:

```
  sales seen from Gumroad : 5
  rows inserted           : 3
  confirmed in Airtable   : 5/5
  result                  : PASS — every sale is in the table
```

Two of the five were already in the table, so it inserted only the three new ones —
that's the dedupe working.

## Prove it catches a broken sync

```bash
python3 test_verification.py
```

Twelve checks, all passing. The one that matters:

```
2. Silent drop — the exact failure the buyer described
  PASS  the drop is caught
  PASS  and it names the missing rows
  PASS  found count is honest
```

That test simulates an API that accepts a write, returns success, and stores nothing —
which is precisely how the last freelancer's automation would have behaved. The sync
catches it.

## Going live

```bash
export GUMROAD_TOKEN=...
export AIRTABLE_TOKEN=...
export AIRTABLE_BASE_ID=appXXXXXXXXXXXXXX
export AIRTABLE_TABLE=Sales
python3 gumroad_to_airtable.py
```

Needs a Gumroad access token and an Airtable personal access token with `data.records:write`
on the base. Both are free to create.

## What it does about things going wrong

- **Retries** — a 429 or 5xx is retried three times with exponential backoff and jitter.
  A 4xx is not, because retrying a bad request just wastes time.
- **Dedupe** — every row carries its Sale ID. Re-running the sync can never double-enter
  a customer, which is the bug that makes people distrust automations in the first place.
- **Batching** — writes go in tens, which is Airtable's limit.
- **A receipt** — every run writes `receipt.json`: when it started, how long it took,
  how many sales it saw, how many rows it inserted, and the verification result.
  That file is the answer to "did it actually run?"
- **An honest exit code** — 0 if every sale is confirmed present, 1 if any is missing.
  Wire that into a scheduler and you get told when it breaks instead of finding out later.

## Files

| File | What it is |
|---|---|
| `gumroad_to_airtable.py` | the sync |
| `test_verification.py` | twelve checks, including the silent-drop case |
| `receipt.json` | written by each run — the proof |
| `README.md` | this |
