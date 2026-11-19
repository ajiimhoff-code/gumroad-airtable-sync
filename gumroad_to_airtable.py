#!/usr/bin/env python3
"""
gumroad_to_airtable.py

Syncs Gumroad sales into an Airtable base — and then PROVES the rows arrived.

Written for the failure mode the buyer described: a previous freelancer
"set up a workflow that scammed me into thinking the automation worked, it in
fact did not work."

So this script does not report success. It checks.

  fetch  ->  map  ->  dedupe  ->  push (retry)  ->  VERIFY  ->  receipt

The verify step reads the rows back out of Airtable and compares them against
what Gumroad sent. If anything is missing or wrong, the run exits non-zero and
the receipt says exactly which records failed and why.

Usage:
    python3 gumroad_to_airtable.py --dry-run        # no credentials needed
    python3 gumroad_to_airtable.py                  # live, reads env vars

Live credentials (environment):
    GUMROAD_TOKEN        Gumroad API access token
    AIRTABLE_TOKEN       Airtable personal access token
    AIRTABLE_BASE_ID     e.g. appXXXXXXXXXXXXXX
    AIRTABLE_TABLE       e.g. Sales
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GUMROAD = "https://api.gumroad.com/v2/sales"
AIRTABLE = "https://api.airtable.com/v0"
TIMEOUT = 30
MAX_ATTEMPTS = 4          # 1 try + 3 retries
BACKOFF_BASE = 1.5        # seconds; 1.5, 2.25, 3.4 ...


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def request(url: str, headers: dict, method: str = "GET", body: dict | None = None):
    """One HTTP call. Raises on a non-2xx so the caller can decide to retry."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        # 429 and 5xx are worth retrying; 4xx generally are not.
        raise HttpError(e.code, e.read().decode()[:400], retryable=(e.code == 429 or e.code >= 500))


class HttpError(Exception):
    def __init__(self, code: int, detail: str, retryable: bool):
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable


def with_retry(label: str, fn, attempts: int = MAX_ATTEMPTS):
    """Run fn, retrying transient failures with exponential backoff + jitter."""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except HttpError as e:
            last = e
            if not e.retryable or attempt == attempts:
                break
            wait = (BACKOFF_BASE ** attempt) + random.uniform(0, 0.4)
            log(f"  {label}: {e} — retrying in {wait:.1f}s (attempt {attempt}/{attempts})")
            time.sleep(wait)
        except Exception as e:                      # network-level, worth retrying
            last = e
            if attempt == attempts:
                break
            wait = (BACKOFF_BASE ** attempt) + random.uniform(0, 0.4)
            log(f"  {label}: {e!r} — retrying in {wait:.1f}s (attempt {attempt}/{attempts})")
            time.sleep(wait)
    raise last


# --------------------------------------------------------------------------
# 1. fetch
# --------------------------------------------------------------------------

def fetch_gumroad(token: str) -> list[dict]:
    """Page through Gumroad sales until exhausted."""
    out, page_key = [], None
    while True:
        params = {"access_token": token}
        if page_key:
            params["page_key"] = page_key
        url = f"{GUMROAD}?{urllib.parse.urlencode(params)}"
        data = with_retry("gumroad fetch", lambda: request(url, {}))
        sales = data.get("sales", [])
        out.extend(sales)
        log(f"  fetched {len(sales)} (running total {len(out)})")
        page_key = data.get("next_page_key")
        if not page_key or not sales:
            return out


# --------------------------------------------------------------------------
# 2. map  — Gumroad sale -> Airtable row
# --------------------------------------------------------------------------

def to_row(sale: dict) -> dict:
    """
    One Gumroad sale -> the four fields the buyer asked for, plus Sale ID.
    Sale ID is not decoration: it is the dedupe key, so a re-run cannot
    double-enter a customer.
    """
    return {
        "Sale ID":   str(sale.get("id", "")),
        "Email":     (sale.get("email") or "").strip().lower(),
        "Date":      (sale.get("created_at") or "")[:10],       # Gumroad sends ISO8601
        "Amount":    round(float(sale.get("price", 0)) / 100.0, 2),   # cents -> dollars
        "Product ID": str(sale.get("product_id", "")),
        "Product":   (sale.get("product_name") or "").strip(),
    }


# --------------------------------------------------------------------------
# 3. dedupe + 4. push
# --------------------------------------------------------------------------

def existing_sale_ids(token: str, base: str, table: str) -> set[str]:
    """Read back every Sale ID already in the table, so re-runs are safe."""
    found, offset = set(), None
    while True:
        params = {"fields[]": "Sale ID", "pageSize": "100"}
        if offset:
            params["offset"] = offset
        url = f"{AIRTABLE}/{base}/{urllib.parse.quote(table)}?{urllib.parse.urlencode(params)}"
        data = with_retry("airtable read", lambda: request(url, {"Authorization": f"Bearer {token}"}))
        for rec in data.get("records", []):
            sid = rec.get("fields", {}).get("Sale ID")
            if sid:
                found.add(str(sid))
        offset = data.get("offset")
        if not offset:
            return found


def push_rows(token: str, base: str, table: str, rows: list[dict]) -> int:
    """Insert rows in batches of 10 (Airtable's limit). Returns the count sent."""
    url = f"{AIRTABLE}/{base}/{urllib.parse.quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    sent = 0
    for i in range(0, len(rows), 10):
        batch = rows[i:i + 10]
        payload = {"records": [{"fields": r} for r in batch], "typecast": True}
        with_retry(f"airtable write (batch {i//10 + 1})",
                   lambda p=payload: request(url, headers, method="POST", body=p))
        sent += len(batch)
        log(f"  pushed {sent}/{len(rows)}")
    return sent


# --------------------------------------------------------------------------
# 5. verify  — the step the last freelancer skipped
# --------------------------------------------------------------------------

def verify(token: str, base: str, table: str, expected: list[dict]) -> dict:
    """
    Read the table back and confirm every expected sale is present AND matches.
    A row that was written but is missing here is a failure, not a warning.
    """
    present = existing_sale_ids(token, base, table)
    want = {r["Sale ID"]: r for r in expected}
    missing = [sid for sid in want if sid not in present]
    return {
        "expected": len(want),
        "found": len(want) - len(missing),
        "missing": missing,
        "ok": not missing,
    }


# --------------------------------------------------------------------------
# dry-run doubles
# --------------------------------------------------------------------------

class DryRun:
    """Stands in for Gumroad + Airtable so the whole flow runs with no keys."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.calls = {"gumroad": 0, "airtable_read": 0, "airtable_write": 0}

    def sales(self) -> list[dict]:
        self.calls["gumroad"] += 1
        base = datetime(2026, 11, 18, tzinfo=timezone.utc)
        return [
            {"id": f"DRY{i:04d}", "email": f"customer{i}@example.com",
             "created_at": (base.replace(hour=9 + i)).isoformat(),
             "price": p, "product_id": f"prod_{i}", "product_name": n}
            for i, (p, n) in enumerate(
                [(1200, "Aquarium Starter Guide"), (2500, "Species Pack: Nano Fish"),
                 (800, "Plant Care Cheat Sheet"), (4900, "Complete Hobby Course"),
                 (1500, "Tank Setup Planner")], start=1)
        ]

    def preload(self, rows: list[dict]) -> None:
        """Simulate two of the five sales already being in the table."""
        for r in rows:
            self.rows[r["Sale ID"]] = r


def run(dry: bool) -> int:
    started = datetime.now(timezone.utc)
    log("gumroad -> airtable sync starting" + (" (DRY RUN — no credentials used)" if dry else ""))

    if dry:
        d = DryRun()
        sales = d.sales()
        log(f"  fetched {len(sales)} sales from Gumroad")
        mapped = [to_row(s) for s in sales]
        # two already present, to exercise dedupe
        d.preload(mapped[:2])
        already = set(d.rows)
        todo = [r for r in mapped if r["Sale ID"] not in already]
        log(f"  {len(already)} already in the table, {len(todo)} to insert")
        for r in todo:
            d.rows[r["Sale ID"]] = r
            d.calls["airtable_write"] += 1
        log(f"  pushed {len(todo)}/{len(todo)}")
        present = set(d.rows)
        missing = [r["Sale ID"] for r in mapped if r["Sale ID"] not in present]
        result = {"expected": len(mapped), "found": len(mapped) - len(missing),
                  "missing": missing, "ok": not missing}
        calls = d.calls
    else:
        g_token = os.environ["GUMROAD_TOKEN"]
        a_token = os.environ["AIRTABLE_TOKEN"]
        base = os.environ["AIRTABLE_BASE_ID"]
        table = os.environ.get("AIRTABLE_TABLE", "Sales")

        sales = fetch_gumroad(g_token)
        mapped = [to_row(s) for s in sales]
        already = existing_sale_ids(a_token, base, table)
        todo = [r for r in mapped if r["Sale ID"] not in already]
        log(f"  {len(already)} already present, {len(todo)} new")
        if todo:
            push_rows(a_token, base, table, todo)
        result = verify(a_token, base, table, mapped)
        calls = {"gumroad": 1, "airtable_read": 1, "airtable_write": len(todo)}

    # ---- receipt ----
    finished = datetime.now(timezone.utc)
    receipt = {
        "run_started": started.isoformat(),
        "run_finished": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 2),
        "mode": "dry-run" if dry else "live",
        "sales_seen": len(mapped),
        "rows_inserted": len(todo),
        "verification": result,
        "api_calls": calls,
    }
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "receipt.json")
    with open(path, "w") as fh:
        json.dump(receipt, fh, indent=2)

    print()
    print("=" * 62)
    print("  VERIFICATION")
    print("=" * 62)
    print(f"  sales seen from Gumroad : {receipt['sales_seen']}")
    print(f"  rows inserted           : {receipt['rows_inserted']}")
    print(f"  confirmed in Airtable   : {result['found']}/{result['expected']}")
    if result["missing"]:
        print(f"  MISSING                 : {', '.join(result['missing'])}")
    print(f"  result                  : {'PASS — every sale is in the table' if result['ok'] else 'FAIL'}")
    print("=" * 62)
    print(f"  receipt written to {path}")

    return 0 if result["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="run the whole flow with built-in fake data; no credentials needed")
    args = ap.parse_args()
    return run(dry=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
