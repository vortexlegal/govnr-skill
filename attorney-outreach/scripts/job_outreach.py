#!/usr/bin/env python3
"""Job-order campaign: pull NEW job orders from VortexRecruitCRM, match them to CRM attorneys whose
years of experience fall inside the posting's range (and whose practice fits), and queue one email
per matched attorney. Sending reuses partner_outreach.py (`send` builds the batch, `send-outbox`
sends it through SendGrid, paced), with this campaign's own state folder.

  job_outreach.py sync  --state DIR --crm-dir DIR     pull job orders + candidates, import new openings, match, queue
  job_outreach.py match --state DIR --crm-dir DIR     re-run matching for imported jobs not yet matched (no CRM calls)
  job_outreach.py status --state DIR

CRM endpoints (header x-api-key, injected by the session's network proxy):
  GET <CRM_BASE_URL>/api/external/job-orders          ?since=&cursor=&limit=
  GET <CRM_BASE_URL>/api/external/attorney-candidates ?since=&cursor=&limit=

Rules (Jon 2026-11-18): only NEW openings are imported (a job id is imported once, ever); matching is
based on the years of experience in the posting: an attorney must be inside the posting's range.
Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template

sys.path.insert(0, str(Path(__file__).resolve().parent))
from match_and_draft import TfIdf, area_overlap, cosine, norm_text, split_list, tokens, truthy  # noqa: E402
from partner_outreach import (ET, QUEUE_COLS, domain, is_leadership, read_rows, render_body,  # noqa: E402
                              with_footer, write_rows, FREE_MAIL)

HERE = Path(__file__).resolve().parent.parent
JOB_COLS = ["job_id", "title", "client", "city", "state", "remote", "practice_areas", "min_years", "max_years",
            "experience_text", "comp_range", "status", "source", "created_at", "imported_at", "matched_at",
            "matches", "queued"]
CAND_COLS = ["id", "first_name", "last_name", "email", "work_email", "personal_email", "title", "company",
             "practice_areas", "city", "state", "years", "years_source", "is_dnu", "unsubscribed", "updated_at"]
MATCH_COLS = ["job_id", "email", "name", "years", "score", "shared", "matched_at", "queued"]
OVERLAP = timedelta(hours=6)
CLOSED = re.compile(r"\b(closed|filled|cancel+ed|on hold|inactive|archived|lost)\b", re.I)


# ---------- CRM ----------

def fetch(base: str, endpoint: str, since: str | None, key: str, limit: int = 1000) -> tuple[list[dict], str]:
    items, cursor, generated = [], None, ""
    for _ in range(2000):
        q = {"limit": str(limit), **({"since": since} if since else {}), **({"cursor": cursor} if cursor else {})}
        req = urllib.request.Request(base.rstrip("/") + endpoint + "?" + urllib.parse.urlencode(q),
                                     headers={"x-api-key": key, "Accept": "application/json",
                                              "User-Agent": "attorney-outreach-jobs"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    if "json" not in (r.headers.get("Content-Type") or ""):
                        raise SystemExit(f"{endpoint} is not available on the CRM yet (got a web page, not JSON).")
                    page = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise SystemExit(f"CRM refused the API key for {endpoint} ({e.code}).")
                if e.code == 503:
                    raise SystemExit("CRM export is not configured (503).")
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 ** (attempt + 1)); continue
                raise
            except urllib.error.URLError:
                time.sleep(2 ** (attempt + 1))
        else:
            raise SystemExit(f"CRM {endpoint} did not answer after 4 tries.")
        items += page.get("items") or []
        generated = page.get("generated_at") or generated
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return items, generated or datetime.now(timezone.utc).isoformat(timespec="seconds")


def pick(d: dict, *names, default=""):
    """First non-empty value among several possible field names (the CRM's exact names may vary)."""
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, "", [], {}):
            return "; ".join(map(str, v)) if isinstance(v, list) else v
    return default


# ---------- years of experience ----------

WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
           "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20}


def num(s) -> int | None:
    if s in (None, ""):
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        m = re.search(r"\d+", str(s))
        return int(m.group()) if m else None


def years_range(min_y, max_y, text: str, description: str = "") -> tuple[int | None, int | None]:
    """The posting's experience range. Uses the CRM's min/max fields, else reads it from the posting text:
    '3-6 years', '3 to 6 years', '5+ years', 'at least 4 years', 'minimum of 7 years', 'up to 3 years'."""
    lo, hi = num(min_y), num(max_y)
    if lo is not None or hi is not None:
        return lo, hi
    year = datetime.now(ET).year
    for src in (text or "", description or ""):
        s = src.lower()
        # Class years ("class of 2019-2021", "2019 - 2021 class years", "2020 class year"): years = now - class year.
        m = re.search(r"(?:class(?:es)?\s*(?:of|years?)?\s*)?((?:19|20)\d\d)\s*(?:-|–|—|to|through)\s*((?:19|20)\d\d)"
                      r"\s*(?:class|associate)?", s)
        if m and "class" in s[max(0, m.start() - 20): m.end() + 20]:
            y1, y2 = sorted((int(m.group(1)), int(m.group(2))))
            if 1980 <= y1 <= year and y2 <= year:
                return year - y2, year - y1
        m = re.search(r"(?:class of\s*((?:19|20)\d\d))|(?:((?:19|20)\d\d)\s*class year)", s)
        if m:
            y = int(m.group(1) or m.group(2))
            if 1980 <= y <= year:
                return year - y, year - y
        for w, n in WORDNUM.items():
            s = re.sub(rf"\b{w}\b", str(n), s)
        m = re.search(r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\+?\s*(?:years|yrs|year)", s)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.search(r"(\d{1,2})\s*\+\s*(?:years|yrs)|(?:at least|minimum of|min\.?|minimum)\s*(\d{1,2})\s*(?:years|yrs)"
                      r"|(\d{1,2})\s*(?:or more|plus)\s*(?:years|yrs)", s)
        if m:
            return int(next(g for g in m.groups() if g)), None
        m = re.search(r"(?:up to|no more than|less than|under)\s*(\d{1,2})\s*(?:years|yrs)", s)
        if m:
            return 0, int(m.group(1))
        m = re.search(r"(\d{1,2})\s*(?:years|yrs)\s*(?:of)?\s*(?:experience|practice)", s)
        if m:
            return int(m.group(1)), None
    return None, None


def candidate_years(c: dict, year: int) -> tuple[int | None, str]:
    y = num(pick(c, "years_experience", "years_of_experience", "years"))
    if y is not None:
        return y, "crm"
    bars = [num(x) for x in re.findall(r"(?:19|20)\d\d", str(pick(c, "bar_admission_years", "bar_admission_year",
                                                                    "bar_admissions", "bar_year")))]
    bars = [b for b in bars if b and 1950 <= b <= year]
    if bars:
        return year - min(bars), "bar year"
    g = num(pick(c, "law_school_graduation_year", "graduation_year", "grad_year", "jd_year", "class_year"))
    if g and 1950 <= g <= year:
        return year - g, "grad year"
    return None, ""


# ---------- sync ----------

def job_row(it: dict) -> dict:
    lo, hi = years_range(pick(it, "min_years", "minimum_years", "years_min", "min_experience"),
                         pick(it, "max_years", "maximum_years", "years_max", "max_experience"),
                         str(pick(it, "experience_text", "experience", "years_experience", "experience_required")),
                         str(pick(it, "description", "job_description", "details")))
    return {"job_id": str(pick(it, "id", "job_id", "job_order_id")),
            "title": str(pick(it, "title", "job_title", "position_title")).strip(),
            "client": str(pick(it, "client_name", "client", "firm_name", "firm", "company")).strip(),
            "city": str(pick(it, "city", "location_city")).strip(), "state": str(pick(it, "state", "location_state")).strip(),
            "remote": str(pick(it, "remote", "work_arrangement", "location_type")).strip(),
            "practice_areas": str(pick(it, "practice_areas", "practice_area", "practice")).strip(),
            "min_years": "" if lo is None else str(lo), "max_years": "" if hi is None else str(hi),
            "experience_text": str(pick(it, "experience_text", "experience", "years_experience")).strip(),
            "comp_range": str(pick(it, "comp_range", "compensation", "salary_range", "compensation_range")).strip(),
            "status": str(pick(it, "status")).strip(), "source": str(pick(it, "source")).strip(),
            "created_at": str(pick(it, "created_at", "posted_at", "date_created")),
            "_description": str(pick(it, "description", "job_description", "details")),
            "_open": pick(it, "is_open", "open", default=None)}


def job_is_open(j: dict) -> bool:
    if j["_open"] is not None and str(j["_open"]).strip() != "":
        return truthy(j["_open"])
    return not CLOSED.search(j["status"] or "")


def cand_row(it: dict, year: int) -> dict:
    yrs, src = candidate_years(it, year)
    work = str(pick(it, "work_email", "email")).strip().lower()
    personal = str(pick(it, "personal_email")).strip().lower()
    return {"id": str(pick(it, "id")), "first_name": str(pick(it, "first_name")).strip(),
            "last_name": str(pick(it, "last_name")).strip(), "email": work or personal, "work_email": work,
            "personal_email": personal, "title": str(pick(it, "current_title", "title")).strip(),
            "company": str(pick(it, "current_company", "company", "firm")).strip(),
            "practice_areas": str(pick(it, "practice_areas", "practice")).strip(),
            "city": str(pick(it, "city")).strip(), "state": str(pick(it, "state")).strip(),
            "years": "" if yrs is None else str(yrs), "years_source": src,
            "is_dnu": "true" if truthy(pick(it, "is_dnu", default="")) else "",
            "unsubscribed": "true" if truthy(pick(it, "unsubscribed", "is_unsubscribed", "email_opt_out", default="")) else "",
            "updated_at": str(pick(it, "updated_at"))}


def cmd_sync(a) -> int:
    state, crm = a.state, a.crm_dir
    state.mkdir(parents=True, exist_ok=True)
    sync_path = state / "sync_state.json"
    sync = json.loads(sync_path.read_text()) if sync_path.exists() else {}
    key = os.environ.get("CRM_EXPORT_API_KEY", "proxy-injected")
    base = a.base_url or os.environ.get("CRM_BASE_URL", "")
    if not base:
        raise SystemExit("CRM_BASE_URL is required")
    year = datetime.now(ET).year

    def since(name):
        t = sync.get(name)
        return (datetime.fromisoformat(t) - OVERLAP).isoformat(timespec="seconds") if t and not a.full else None

    # Candidates: incremental, merged into one master file keyed by CRM id.
    items, gen_c = fetch(base, "/api/external/attorney-candidates", since("candidates"), key)
    master_path = crm / "attorney_candidates.csv"
    master = {r["id"]: r for r in read_rows(master_path)}
    for it in items:
        r = cand_row(it, year)
        if r["id"]:
            master[r["id"]] = r
    write_rows(master_path, CAND_COLS, list(master.values()))

    # Job orders: import each id once, ever, and only if it is open.
    items, gen_j = fetch(base, "/api/external/job-orders", since("jobs"), key)
    jobs_path = state / "jobs.csv"
    jobs = read_rows(jobs_path)
    seen = {j["job_id"] for j in jobs} | {r["job_id"] for r in read_rows(state / "jobs_ignored.csv")}
    new, ignored = [], []
    first_run = not sync.get("jobs") and not jobs
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    desc_dir = state / "job_descriptions"
    for it in items:
        j = job_row(it)
        if not j["job_id"] or j["job_id"] in seen:
            continue
        seen.add(j["job_id"])
        if first_run and not a.include_existing:
            ignored.append({"job_id": j["job_id"], "title": j["title"], "reason": "open before launch", "at": now})
            continue
        if not job_is_open(j):
            ignored.append({"job_id": j["job_id"], "title": j["title"], "reason": f"not open ({j['status']})", "at": now})
            continue
        desc_dir.mkdir(exist_ok=True)
        (desc_dir / f"{re.sub(r'[^A-Za-z0-9_-]', '_', j['job_id'])}.txt").write_text(j["_description"], encoding="utf-8")
        j.update(imported_at=now, matched_at="", matches="", queued="")
        new.append(j)
    write_rows(jobs_path, JOB_COLS, jobs + new)
    if ignored:
        old = read_rows(state / "jobs_ignored.csv")
        write_rows(state / "jobs_ignored.csv", ["job_id", "title", "reason", "at"], old + ignored)
    sync.update(candidates=gen_c, jobs=gen_j)
    sync_path.write_text(json.dumps(sync, indent=1))
    print(f"CRM: {len(master):,} attorneys ({sum(1 for r in master.values() if r['years']):,} with years of experience); "
          f"{len(new)} new job order(s) imported, {len(ignored)} skipped"
          + (" (first run: existing openings recorded, not emailed)" if first_run and not a.include_existing else "") + ".")
    for j in new:
        rng = f"{j['min_years'] or '?'}-{j['max_years'] or '+'} yrs" if (j["min_years"] or j["max_years"]) else "no years in posting"
        print(f"  NEW {j['job_id']}: {j['title']} | {j['client'] or 'client n/a'} | {j['city']} {j['state']} | {rng}")
    return cmd_match(a)


# ---------- match ----------

def all_suppressed(vl: Path, extra: list[Path]) -> set[str]:
    out: set[str] = set()
    files = [*extra, *vl.glob("state*/suppression.csv"), vl / "crm" / "dnu_all.csv", *vl.glob("crm/exclude_*.csv")]
    for p in files:
        for r in read_rows(p):
            e = (r.get("email") or r.get("Email") or next(iter(r.values()), "") or "").strip().lower()
            if "@" in e:
                out.add(e)
    return out


def recently_contacted(vl: Path, days: int) -> set[str]:
    """Anyone any campaign emailed (or who replied) in the last `days` days."""
    cut = datetime.now(timezone.utc) - timedelta(days=days)
    out = set()
    for p in vl.glob("state*/sent_log.csv"):
        for r in read_rows(p):
            try:
                if r.get("status") in ("sent", "replied") and datetime.fromisoformat(r["sent_at"]) >= cut:
                    out.add(r["to"].lower())
            except (KeyError, ValueError):
                pass
    return out


def plain_years(lo: int | None, hi: int | None) -> str:
    if lo is not None and hi is not None:
        return f"{lo} to {hi} years" if lo != hi else f"{lo} years"
    if lo is not None:
        return f"{lo}+ years"
    return f"up to {hi} years"


def cmd_match(a) -> int:
    state, vl = a.state, a.crm_dir.parent
    jobs = read_rows(state / "jobs.csv")
    todo = [j for j in jobs if not j.get("matched_at")]
    if not todo:
        print("No new job orders to match.")
        return 0
    cands = [c for c in read_rows(a.crm_dir / "attorney_candidates.csv") if c.get("email") and c.get("years")]
    suppressed = all_suppressed(vl, a.suppress_file)
    recent = recently_contacted(vl, a.recontact_days)
    queue = read_rows(state / "queue.csv")
    pending = {r["to"].lower() for r in queue}
    emailed_for = {(r.get("practice") or "", r["to"].lower()) for r in read_rows(state / "sent_log.csv")}
    matches_log = read_rows(state / "matches.csv")
    tpl = Template((HERE / "templates" / "job_outreach_email.txt").read_text(encoding="utf-8"))
    docs = [tokens(" ".join([c["title"], c["practice_areas"]])) for c in cands]
    idf = TfIdf(docs)
    cvecs = [idf.vec(d) for d in docs]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sig = "\n".join(x for x in [a.sender_name, ", ".join(x for x in [a.sender_title, a.sender_company] if x),
                                " | ".join(x for x in [a.sender_phone, a.sender_email] if x), a.sender_website] if x)
    total_q = 0
    for j in todo:
        lo, hi = num(j["min_years"]), num(j["max_years"])
        jid = j["job_id"]
        desc_p = state / "job_descriptions" / f"{re.sub(r'[^A-Za-z0-9_-]', '_', jid)}.txt"
        desc = desc_p.read_text(encoding="utf-8") if desc_p.exists() else ""
        j_areas = split_list(j["practice_areas"])
        jvec = idf.vec(tokens(" ".join([j["title"], j["practice_areas"], desc[:2000]])))
        client_key = re.sub(r"[^a-z0-9]+", " ", (j["client"] or "").lower()).strip()
        remote = (j["remote"] or "").lower() in ("remote", "yes", "true", "fully remote")
        found = []
        if lo is None and hi is None:
            j.update(matched_at=now, matches="0", queued="0")
            print(f"  {jid} {j['title']}: no years of experience in the posting, nothing matched (review by hand).")
            continue
        for c, cv in zip(cands, cvecs):
            yrs = num(c["years"])
            if yrs is None or (lo is not None and yrs < lo) or (hi is not None and yrs > hi):
                continue                                   # the rule: inside the posting's years range
            to = c["email"].lower()
            if (to in suppressed or c.get("is_dnu") or c.get("unsubscribed") or to in recent
                    or (jid, to) in emailed_for or is_leadership(c["title"])):
                continue
            ck = re.sub(r"[^a-z0-9]+", " ", (c["company"] or "").lower()).strip()
            if client_key and ck and (ck == client_key or ck.startswith(client_key) or client_key.startswith(ck)):
                continue                                   # never pitch a firm's job to its own attorneys
            if j["state"] and c["state"] and not remote and j["state"].strip().lower() != c["state"].strip().lower():
                continue                                   # same state unless the job is remote
            overlap, shared = area_overlap(split_list(c["practice_areas"]), j_areas)
            sim = cosine(cv, jvec)
            s = 0.6 * overlap + 0.4 * sim
            if s < a.min_score:
                continue
            found.append((s, c, shared))
        found.sort(key=lambda x: -x[0])
        q = 0
        for s, c, shared in found[: a.max_per_job]:
            to = c["email"].lower()
            matches_log.append({"job_id": jid, "email": to, "name": f"{c['first_name']} {c['last_name']}".strip(),
                                "years": c["years"], "score": f"{s:.3f}", "shared": "; ".join(shared),
                                "matched_at": now, "queued": "" if to in pending else "yes"})
            if to in pending:
                continue                                   # one pending email per person, whichever job came first
            where = (f"in {j['city']}" if j["city"] else (f"in {j['state']}" if j["state"] else ""))
            if remote:
                where = "(remote)" if not where else f"{where} (remote)"
            elif (j["remote"] or "").lower() == "hybrid":
                where = f"{where} (hybrid)".strip()
            area = (shared[0] if shared else (split_list(c["practice_areas"])[:1] or [""])[0]) or ""
            summary = f"The firm is looking for {plain_years(lo, hi)} of experience" + \
                      (f" in {j['practice_areas'].split(';')[0].strip().lower()}" if j["practice_areas"] else "") + "."
            if j["comp_range"]:
                summary += f" Compensation is in the {j['comp_range']} range."
            body = render_body(tpl, {"first_name": c["first_name"] or "there", "job_title": j["title"],
                                     "job_where": where, "job_summary": summary,
                                     "practice_work": f"{area.lower()} work" if area else "experience",
                                     "firm": c["company"] or "your firm", "sender_signature": sig}, seed=f"{to}:{jid}")
            body = with_footer(body, a.sender_address)
            subject = f"{j['title']}" + (f" in {j['city']}" if j["city"] else "")
            queue.append({"contact_key": f"{jid}|{to}", "to": to, "name": f"{c['first_name']} {c['last_name']}".strip(),
                          "firm": c["company"], "title": c["title"], "practice": jid, "subject": subject,
                          "body": body, "prepared_at": now})
            pending.add(to)
            q += 1
        j.update(matched_at=now, matches=str(len(found)), queued=str(q))
        total_q += q
        print(f"  {jid} {j['title']} ({plain_years(lo, hi)}): {len(found)} matched, {q} queued")
    write_rows(state / "jobs.csv", JOB_COLS, jobs)
    write_rows(state / "matches.csv", MATCH_COLS, matches_log)
    write_rows(state / "queue.csv", QUEUE_COLS, queue)
    print(f"Queued {total_q} new email(s); {len(queue)} waiting to send.")
    return 0


def cmd_status(a) -> int:
    jobs = read_rows(a.state / "jobs.csv")
    q = read_rows(a.state / "queue.csv")
    log = [r for r in read_rows(a.state / "sent_log.csv") if r.get("status") == "sent"]
    today = datetime.now(ET).date()
    print(f"{len(jobs)} job orders imported; {len(q)} emails waiting; {len(log)} sent "
          f"({sum(1 for r in log if datetime.fromisoformat(r['sent_at']).astimezone(ET).date() == today)} today).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("sync", cmd_sync), ("match", cmd_match), ("status", cmd_status)]:
        p = sub.add_parser(name)
        p.add_argument("--state", type=Path, required=True)
        p.add_argument("--crm-dir", type=Path, default=None)
        p.add_argument("--base-url", default="")
        p.add_argument("--full", action="store_true", help="ignore the last sync time and pull everything")
        p.add_argument("--include-existing", action="store_true",
                       help="first run only: also import openings that already exist (default: record them, email no one)")
        p.add_argument("--suppress-file", type=Path, action="append", default=[])
        p.add_argument("--recontact-days", type=int, default=int(os.environ.get("JOBS_RECONTACT_DAYS", "14")),
                       help="skip anyone any campaign emailed in the last N days")
        p.add_argument("--min-score", type=float, default=float(os.environ.get("JOBS_MIN_SCORE", "0.2")),
                       help="minimum practice-area fit (0-1) on top of the years rule")
        p.add_argument("--max-per-job", type=int, default=int(os.environ.get("JOBS_MAX_PER_JOB", "150")))
        for f, env in [("sender-name", "OUTREACH_SENDER_NAME"), ("sender-title", "OUTREACH_SENDER_TITLE"),
                       ("sender-company", "OUTREACH_SENDER_COMPANY"), ("sender-email", "OUTREACH_SENDER_EMAIL"),
                       ("sender-phone", "OUTREACH_SENDER_PHONE"), ("sender-website", "OUTREACH_SENDER_WEBSITE"),
                       ("sender-address", "OUTREACH_SENDER_ADDRESS")]:
            p.add_argument(f"--{f}", default=os.environ.get(env, ""))
        p.set_defaults(func=fn)
    a = ap.parse_args()
    a.crm_dir = a.crm_dir or (a.state.parent / "crm")
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
