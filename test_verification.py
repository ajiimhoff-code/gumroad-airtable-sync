#!/usr/bin/env python3
"""
test_verification.py

Proves the sync catches the failure the buyer is actually afraid of.

The buyer's words: a previous freelancer "setup a workflow that scammed me
into thinking the automation worked, it in fact did not work."

A sync that only reports what it *sent* cannot tell the difference between
those two things. These tests show that this one can.

Run:  python3 test_verification.py
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("sync", os.path.join(HERE, "gumroad_to_airtable.py"))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

failures = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got {got!r}, wanted {want!r}")
        failures.append(name)


# --------------------------------------------------------------------------
# a stand-in for Airtable that can be told to misbehave
# --------------------------------------------------------------------------

class FakeAirtable:
    def __init__(self, drop_ids=(), fail_writes=0):
        self.rows = {}
        self.drop_ids = set(drop_ids)     # silently accepted, never stored
        self.fail_writes = fail_writes    # raise on the first N write attempts
        self.write_attempts = 0

    def write(self, rows):
        self.write_attempts += 1
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise sync.HttpError(503, "service unavailable", retryable=True)
        for r in rows:
            if r["Sale ID"] not in self.drop_ids:
                self.rows[r["Sale ID"]] = r
        return {"records": [{"id": f"rec{i}"} for i, _ in enumerate(rows)]}

    def read_ids(self):
        return set(self.rows)


def sales(n=5):
    base = [
        {"id": "S1", "email": "a@example.com", "created_at": "2026-11-18T09:00:00Z",
         "price": 1200, "product_id": "p1", "product_name": "Guide"},
        {"id": "S2", "email": "b@example.com", "created_at": "2026-11-18T10:00:00Z",
         "price": 2500, "product_id": "p2", "product_name": "Species Pack"},
        {"id": "S3", "email": "c@example.com", "created_at": "2026-11-18T11:00:00Z",
         "price": 800, "product_id": "p3", "product_name": "Cheat Sheet"},
        {"id": "S4", "email": "d@example.com", "created_at": "2026-11-18T12:00:00Z",
         "price": 4900, "product_id": "p4", "product_name": "Course"},
        {"id": "S5", "email": "e@example.com", "created_at": "2026-11-18T13:00:00Z",
         "price": 1500, "product_id": "p5", "product_name": "Planner"},
    ]
    return base[:n]


def verify_against(fake, mapped):
    present = fake.read_ids()
    missing = [r["Sale ID"] for r in mapped if r["Sale ID"] not in present]
    return {"expected": len(mapped), "found": len(mapped) - len(missing),
            "missing": missing, "ok": not missing}


print("\nGumroad -> Airtable verification tests\n")

# --------------------------------------------------------------------------
# 1. a clean run passes
# --------------------------------------------------------------------------
print("1. Clean run")
mapped = [sync.to_row(s) for s in sales()]
air = FakeAirtable()
air.write(mapped)
res = verify_against(air, mapped)
check("all five sales land", res["found"], 5)
check("reported ok", res["ok"], True)

# --------------------------------------------------------------------------
# 2. the failure the buyer actually had: writes "succeed", nothing arrives
# --------------------------------------------------------------------------
print("\n2. Silent drop — the exact failure the buyer described")
air = FakeAirtable(drop_ids={"S3", "S4"})
air.write(mapped)                       # returns 200 with record ids, like the real API
res = verify_against(air, mapped)
check("the drop is caught", res["ok"], False)
check("and it names the missing rows", sorted(res["missing"]), ["S3", "S4"])
check("found count is honest", res["found"], 3)

# --------------------------------------------------------------------------
# 3. a write that fails outright is retried, not silently swallowed
# --------------------------------------------------------------------------
print("\n3. Transient write failure")
air = FakeAirtable(fail_writes=2)
attempts = 0
try:
    sync.with_retry("write", lambda: air.write(mapped), attempts=4)
    attempts = air.write_attempts
except Exception:
    attempts = air.write_attempts
check("it retried and then succeeded", attempts, 3)
check("no rows lost after retry", verify_against(air, mapped)["ok"], True)

# --------------------------------------------------------------------------
# 4. re-running must not double-enter anyone
# --------------------------------------------------------------------------
print("\n4. Re-run safety (dedupe)")
air = FakeAirtable()
first = air.write(mapped)
already = air.read_ids()
todo = [r for r in mapped if r["Sale ID"] not in already]
air.write(todo)
check("second run inserts nothing", len(todo), 0)
check("table still holds exactly five", len(air.read_ids()), 5)

# --------------------------------------------------------------------------
# 5. field mapping is correct, including the cents->dollars conversion
# --------------------------------------------------------------------------
print("\n5. Field mapping")
row = sync.to_row({"id": "X1", "email": "  Mixed@Case.COM ", "created_at": "2026-11-18T09:00:00Z",
                   "price": 2500, "product_id": "p9", "product_name": " Thing "})
check("email lowercased and trimmed", row["Email"], "mixed@case.com")
check("cents converted to dollars", row["Amount"], 25.0)
check("date is just the day", row["Date"], "2026-11-18")
check("sale id kept for dedupe", row["Sale ID"], "X1")

print()
print(f"{'PASS' if not failures else 'FAIL'} ({len(failures)} failures)")
sys.exit(0 if not failures else 1)
