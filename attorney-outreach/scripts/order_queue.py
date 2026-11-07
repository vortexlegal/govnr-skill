#!/usr/bin/env python3
"""Reorder the outreach queue: rotate through priority markets first, then the rest.

  python3 order_queue.py --state STATE --contacts partners_target_markets.csv \
      --priority chicago,dallas,houston,los-angeles,san-francisco,new-york,philadelphia-area-partners.csv
"""
import argparse, csv, collections, itertools
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--state", type=Path, required=True)
ap.add_argument("--contacts", type=Path, required=True)
ap.add_argument("--priority", required=True)
a = ap.parse_args()
src = {r["email"].lower(): r.get("source_file", "") for r in csv.DictReader(open(a.contacts))}
qp = a.state / "queue.csv"
q = list(csv.DictReader(open(qp))); cols = list(q[0]) if q else []
pri = [p.strip() for p in a.priority.split(",") if p.strip()]
by = collections.defaultdict(list)
for r in q:
    by[src.get(r["to"].lower(), "?")].append(r)
rot = lambda keys: [r for g in itertools.zip_longest(*(by[k] for k in keys)) for r in g if r]
out = rot(pri) + rot([k for k in by if k not in pri])
assert len(out) == len(q)
with open(qp, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(out)
print(f"queue {len(out)}: " + ", ".join(f"{k} {len(v)}" for k, v in by.items()))
