#!/usr/bin/env python3
"""Match attorneys from a CRM export to open positions that closely resemble
their current practice, and draft a personalized outreach email for each match.

Standard library only. Nothing is sent: the output is a review file
(drafts.csv + drafts.json) that a human approves before any email goes out.

Usage:
  python3 match_and_draft.py --contacts crm.csv --positions positions.csv \
      --out ./out --sender-name "Jane Recruiter" --sender-title "Legal Recruiter" \
      --sender-company "Acme Legal Search" [--min-score 0.35] [--top 1]
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path
from string import Template

STOPWORDS = set(
    "a an and are as at be by for from in into is of on or our the their to with "
    "law legal counsel attorney lawyer practice matters".split()
)

# Map common synonyms / abbreviations to one canonical token so that
# "M&A" == "mergers and acquisitions", "PE" == "private equity", etc.
SYNONYMS = {
    "mergers and acquisitions": "m&a",
    "mergers & acquisitions": "m&a",
    "pe": "private equity",
    "ip": "intellectual property",
    "lit": "litigation",
    "employment and labor": "labor employment",
    "labor and employment": "labor employment",
    "data privacy": "privacy",
    "fraud and abuse": "anti-kickback stark",
}

# CRM column aliases -> canonical field name (case-insensitive).
CONTACT_ALIASES = {
    "first_name": ["first_name", "firstname", "first name", "given name"],
    "last_name": ["last_name", "lastname", "last name", "surname"],
    "email": ["email", "email address", "e-mail", "primary email"],
    "firm": ["firm", "company", "employer", "organization", "account name"],
    "title": ["title", "job title", "position"],
    "practice_areas": ["practice_areas", "practice areas", "practice", "specialties", "tags"],
    "bar_admissions": ["bar_admissions", "bar admissions", "bar", "licensed in"],
    "years_experience": ["years_experience", "years experience", "years", "experience", "yoe"],
    "city": ["city", "location", "mailing city"],
    "notes": ["notes", "description", "bio", "summary"],
    "do_not_contact": ["do_not_contact", "do not contact", "email opt out", "unsubscribed", "opted out"],
}


def norm_text(s: str) -> str:
    s = (s or "").lower()
    for k, v in SYNONYMS.items():
        s = re.sub(rf"\b{re.escape(k)}\b", v, s)
    return s


def tokens(s: str) -> list[str]:
    s = norm_text(s)
    toks = re.findall(r"[a-z0-9&\-]+", s)
    return [t for t in toks if t not in STOPWORDS and len(t) > 1]


def split_list(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"[;,|/]", s or "") if x.strip()]


def truthy(s) -> bool:
    return str(s).strip().lower() in {"1", "true", "yes", "y", "x"}


def to_int(s, default=None):
    m = re.search(r"\d+", str(s or ""))
    return int(m.group()) if m else default


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def canon_contact(row: dict) -> dict:
    lower = {k.strip().lower(): v for k, v in row.items() if k}
    out = {}
    for field, aliases in CONTACT_ALIASES.items():
        out[field] = next((lower[a] for a in aliases if a in lower and lower[a]), "")
    out["_raw"] = row
    return out


class TfIdf:
    def __init__(self, docs: list[list[str]]):
        n = len(docs)
        df: dict[str, int] = {}
        for d in docs:
            for t in set(d):
                df[t] = df.get(t, 0) + 1
        self.idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}

    def vec(self, toks: list[str]) -> dict[str, float]:
        tf: dict[str, float] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        v = {t: c * self.idf.get(t, 1.0) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}


def cosine(a: dict, b: dict) -> float:
    return sum(a[t] * b.get(t, 0) for t in a)


def area_overlap(a: list[str], b: list[str]) -> tuple[float, list[str]]:
    na = {norm_text(x) for x in a}
    nb = {norm_text(x) for x in b}
    shared = sorted(na & nb)
    if not na or not nb:
        return 0.0, shared
    return len(shared) / min(len(na), len(nb)), shared


def score(contact: dict, pos: dict, tfidf: TfIdf) -> dict:
    c_areas = split_list(contact["practice_areas"])
    p_areas = split_list(pos.get("practice_areas", ""))
    overlap, shared = area_overlap(c_areas, p_areas)

    c_doc = tokens(" ".join([contact["title"], contact["practice_areas"], contact["notes"]]))
    p_doc = tokens(" ".join([pos.get("title", ""), pos.get("practice_areas", ""), pos.get("description", "")]))
    sim = cosine(tfidf.vec(c_doc), tfidf.vec(p_doc))

    s = 0.6 * overlap + 0.4 * sim
    flags = []

    yrs = to_int(contact["years_experience"])
    lo, hi = to_int(pos.get("min_years")), to_int(pos.get("max_years"))
    if yrs is not None and lo is not None and yrs < lo:
        s -= 0.15 * min(1, (lo - yrs) / 3); flags.append(f"below min years ({yrs}<{lo})")
    if yrs is not None and hi is not None and yrs > hi:
        s -= 0.10 * min(1, (yrs - hi) / 5); flags.append(f"above max years ({yrs}>{hi})")

    bar_req = {b.upper() for b in split_list(pos.get("bar_required", ""))}
    bars = {b.upper() for b in split_list(contact["bar_admissions"])}
    if bar_req and not (bar_req & bars):
        s -= 0.2; flags.append(f"not admitted in {'/'.join(sorted(bar_req))}")

    return {"score": round(max(0.0, min(1.0, s)), 3), "shared_areas": shared, "flags": flags}


def nice_list(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def render(tpl: Template, subj_tpl: Template, contact: dict, pos: dict, match: dict, sender: dict) -> tuple[str, str]:
    c_areas = split_list(contact["practice_areas"])
    shared_display = [a for a in c_areas if norm_text(a) in set(match["shared_areas"])] or c_areas[:2]
    remote = (pos.get("remote") or "").strip().lower()
    loc = (pos.get("location") or "").strip()
    if loc.lower().startswith("remote") or remote in {"remote", "yes", "true"}:
        where = ("(" + loc.replace("(", "").replace(")", "") + ")") if loc.lower().startswith("remote") else f"({loc}, remote)"
    elif remote == "hybrid":
        where = f"in {loc} (hybrid)"
    else:
        where = f"in {loc}" if loc else ""
    sender = dict(sender)
    sender["sender_signature"] = "\n".join(x for x in [
        sender["sender_name"],
        ", ".join(x for x in [sender["sender_title"], sender["sender_company"]] if x),
        sender["sender_email"]] if x)
    vals = {
        "first_name": contact["first_name"] or "there",
        "current_title": contact["title"] or "your current role",
        "current_firm": contact["firm"] or "your current organization",
        "shared_areas": nice_list([" ".join(w if w.isupper() or "&" in w else w.lower() for w in a.split())
                                   for a in shared_display[:3]]),
        "position_title": pos.get("title", ""),
        "position_company": pos.get("company", "a client"),
        "position_where": where,
        "position_description": (pos.get("description") or "").rstrip("."),
        "comp_line": f"Compensation is in the {pos['comp_range']} range. " if pos.get("comp_range") else "",
        "link_line": f"Details: {pos['link']}\n\n" if pos.get("link") else "",
        **sender,
    }
    body = tpl.safe_substitute(vals)
    body = "\n".join(line.rstrip() for line in body.splitlines())
    body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
    return subj_tpl.safe_substitute(vals), body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--contacts", required=True, type=Path)
    ap.add_argument("--positions", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    here = Path(__file__).resolve().parent.parent / "templates"
    ap.add_argument("--template", type=Path, default=here / "outreach_email.txt")
    ap.add_argument("--subject", default="$position_title opportunity — close to your $shared_areas work")
    ap.add_argument("--min-score", type=float, default=0.35)
    ap.add_argument("--top", type=int, default=1, help="max positions per attorney")
    ap.add_argument("--sender-name", required=True)
    ap.add_argument("--sender-title", default="")
    ap.add_argument("--sender-company", required=True)
    ap.add_argument("--sender-email", default="")
    ap.add_argument("--sender-address", default="", help="physical mailing address (CAN-SPAM)")
    args = ap.parse_args()

    contacts = [canon_contact(r) for r in read_csv(args.contacts)]
    positions = read_csv(args.positions)
    if not positions:
        print("No positions found.", file=sys.stderr); return 1

    corpus = [tokens(" ".join([c["title"], c["practice_areas"], c["notes"]])) for c in contacts]
    corpus += [tokens(" ".join([p.get("title", ""), p.get("practice_areas", ""), p.get("description", "")])) for p in positions]
    tfidf = TfIdf(corpus)

    tpl = Template(args.template.read_text(encoding="utf-8"))
    subj_tpl = Template(args.subject)
    sender = {
        "sender_name": args.sender_name, "sender_title": args.sender_title,
        "sender_company": args.sender_company, "sender_email": args.sender_email,
        "sender_address": args.sender_address,
    }

    drafts, skipped, seen_emails = [], [], set()
    for c in contacts:
        name = f"{c['first_name']} {c['last_name']}".strip()
        email = c["email"].strip().lower()
        if truthy(c["do_not_contact"]):
            skipped.append({"name": name, "email": email, "reason": "do-not-contact / opted out"}); continue
        if not re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            skipped.append({"name": name, "email": email, "reason": "missing/invalid email"}); continue
        if email in seen_emails:
            skipped.append({"name": name, "email": email, "reason": "duplicate email"}); continue
        seen_emails.add(email)

        scored = sorted(((score(c, p, tfidf), p) for p in positions), key=lambda x: -x[0]["score"])
        picks = [(m, p) for m, p in scored if m["score"] >= args.min_score][: args.top]
        if not picks:
            best = scored[0]
            skipped.append({"name": name, "email": email,
                            "reason": f"no close match (best {best[1].get('title')} @ {best[0]['score']})"})
            continue
        for m, p in picks:
            subject, body = render(tpl, subj_tpl, c, p, m, sender)
            drafts.append({
                "approved": "", "to": email, "name": name, "current_title": c["title"], "current_firm": c["firm"],
                "position_id": p.get("position_id", ""), "position_title": p.get("title", ""),
                "match_score": m["score"], "shared_areas": "; ".join(m["shared_areas"]),
                "flags": "; ".join(m["flags"]), "subject": subject, "body": body,
            })

    args.out.mkdir(parents=True, exist_ok=True)
    cols = list(drafts[0].keys()) if drafts else ["approved", "to", "name", "subject", "body"]
    with (args.out / "drafts.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(drafts)
    (args.out / "drafts.json").write_text(json.dumps({"drafts": drafts, "skipped": skipped}, indent=2))
    with (args.out / "skipped.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "email", "reason"]); w.writeheader(); w.writerows(skipped)

    print(f"{len(drafts)} draft(s), {len(skipped)} skipped -> {args.out}")
    for d in drafts:
        flag = f"  [!] {d['flags']}" if d["flags"] else ""
        print(f"  {d['match_score']:.2f}  {d['name']:<22} -> {d['position_title']}{flag}")
    for s in skipped:
        print(f"  skip  {s['name']:<22} ({s['reason']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
