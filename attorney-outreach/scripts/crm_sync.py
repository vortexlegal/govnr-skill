#!/usr/bin/env python3
"""Pull candidates with verified personal emails from VortexRecruitCRM.

Calls GET <CRM_BASE_URL>/api/external/personal-email-candidates (header x-api-key) for
everything changed since the last run, merges it into one master file keyed by CRM id,
and writes the files the personal-email campaigns are built from:

  <out>/candidates.csv   every candidate with a personal email (master, one row per CRM id)
  <out>/titled.csv       rows with a title (current_title, or a reliable title from the notes)
  <out>/untitled.csv     rows with no title at all
  <out>/crm_dnu.csv      personal emails the CRM marks DNU (pass to prepare AND send as --suppress-file)
  <out>/sync_state.json  when the last pull ran (the next run asks for changes since then)

The API key is sent by the session's network proxy; set CRM_EXPORT_API_KEY only to run
it elsewhere. Standard library only.

  python3 crm_sync.py --out /home/user/vlcrm/crm/personal
  python3 crm_sync.py --out ... --full     # ignore the last-run time and pull everything
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENDPOINT = "/api/external/personal-email-candidates"
COLS = ["id", "first_name", "last_name", "personal_email", "work_email", "current_title", "current_company",
        "practice_areas", "city", "state", "is_dnu", "dnu_reason", "verification_status", "verified_at",
        "updated_at", "title_source"]
OVERLAP = timedelta(hours=6)  # re-ask for a little before the last run, so nothing slips between runs


def fetch(base: str, key: str, since: str | None, limit: int = 1000) -> tuple[list[dict], str]:
    items, cursor, generated = [], None, ""
    for _ in range(1000):
        q = {"limit": str(limit)}
        if since:
            q["since"] = since
        if cursor:
            q["cursor"] = cursor
        url = base.rstrip("/") + ENDPOINT + "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers={"x-api-key": key, "Accept": "application/json",
                                                   "User-Agent": "attorney-outreach-sync"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    page = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise SystemExit(f"CRM refused the API key ({e.code}). Check the saved credential.")
                if e.code == 503:
                    raise SystemExit("CRM export is not configured (503): OUTREACH_EXPORT_API_KEY is not set there.")
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 ** (attempt + 1)); continue
                raise
            except urllib.error.URLError:
                time.sleep(2 ** (attempt + 1))
        else:
            raise SystemExit("CRM export did not answer after 4 tries.")
        items += page.get("items") or []
        generated = page.get("generated_at") or generated
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return items, generated or datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_row(it: dict) -> dict:
    title = (it.get("current_title") or "").strip()
    source = ""
    if not title and (it.get("inferred_title") or "").strip():
        title = it["inferred_title"].strip()
        source = "CRM notes: " + (it.get("inferred_title_source") or "").strip()[:120]
    row = {c: it.get(c) for c in COLS}
    row.update({
        "current_title": title, "title_source": source,
        "practice_areas": "; ".join(it["practice_areas"]) if isinstance(it.get("practice_areas"), list) else (it.get("practice_areas") or ""),
        "is_dnu": "t" if it.get("is_dnu") in (True, "t", "true", "1", 1) else "f",
    })
    return {k: ("" if v is None else str(v)) for k, v in row.items()}


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write(path: Path, cols: list[str], rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True, help="folder for candidates.csv and the derived files")
    ap.add_argument("--base-url", default=os.environ.get("CRM_BASE_URL", ""))
    ap.add_argument("--full", action="store_true", help="pull everything, not just changes since the last run")
    a = ap.parse_args()
    if not a.base_url:
        ap.error("--base-url or CRM_BASE_URL is required")
    a.out.mkdir(parents=True, exist_ok=True)
    state_path = a.out / "sync_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    since = None
    if not a.full and state.get("last_generated_at"):
        since = (datetime.fromisoformat(state["last_generated_at"].replace("Z", "+00:00")) - OVERLAP).isoformat()

    items, generated = fetch(a.base_url, os.environ.get("CRM_EXPORT_API_KEY") or "proxy-injected", since)

    master = {r["id"]: r for r in read(a.out / "candidates.csv")}
    before = set(master)
    changed = 0
    for it in items:
        if not it.get("id") or not it.get("personal_email"):
            continue
        row = to_row(it)
        if master.get(row["id"]) != row:
            changed += 1
        master[row["id"]] = row
    rows = sorted(master.values(), key=lambda r: r["id"])
    write(a.out / "candidates.csv", COLS, rows)
    write(a.out / "titled.csv", COLS, [r for r in rows if r["current_title"].strip()])
    write(a.out / "untitled.csv", COLS, [r for r in rows if not r["current_title"].strip()])
    write(a.out / "crm_dnu.csv", ["email", "reason"],
          [{"email": r["personal_email"], "reason": r["dnu_reason"] or "CRM DNU"} for r in rows if r["is_dnu"] == "t"])
    state.update({"last_generated_at": generated, "last_run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "last_since": since or "", "last_items": len(items)})
    state_path.write_text(json.dumps(state, indent=2) + "\n")
    new = len(set(master) - before)
    print(f"Pulled {len(items)} record(s) {'changed since ' + since if since else '(full pull)'}: "
          f"{new} new candidate(s), {changed - new} updated. Master now {len(rows)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
