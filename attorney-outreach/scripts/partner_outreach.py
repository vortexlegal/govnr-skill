#!/usr/bin/env python3
"""Partner outreach that isn't tied to a job opening.

Emails law-firm partners from a CRM export, asking whether they'd be open to
hearing about partner roles similar to the practice they have now. Nothing in
the email refers to a specific opening, so nothing in it can be invented.

Two steps, run on a schedule (e.g. every weekday morning):

  prepare  CRM export  -> queue.csv  (filter to partners, apply suppressions,
                                      write a personalized email per partner)
  send     queue.csv   -> sent_log.csv (send up to the daily cap, during
                                        business hours, skipping anyone
                                        suppressed or recently emailed)

`send` is a dry run unless --live is passed. Standard library only.

  python3 partner_outreach.py prepare --contacts crm.csv --state ./state \
      --sender-name "Jon Broder" --sender-company "Vortex Legal" \
      --sender-email jbroder@vortexlegal.com --sender-address "..."
  python3 partner_outreach.py send --state ./state --transport sendgrid --daily-cap 40 --live
  python3 partner_outreach.py suppress --state ./state someone@firm.com
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from match_and_draft import CONTACT_ALIASES, canon_contact, read_csv, split_list, truthy  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")

# CRM export column names, in addition to the generic aliases.
CONTACT_ALIASES["first_name"] += ["firstName", "candidate first name"]
CONTACT_ALIASES["last_name"] += ["lastName", "candidate last name"]
# Work email first: outreach goes to the firm address when there is one.
CONTACT_ALIASES["email"] = ["work_email", "work email", "workemail", *CONTACT_ALIASES["email"],
                            "personal_email", "personal email", "personalemail", "emails"]
CONTACT_ALIASES["firm"] += ["current_company", "currentcompany", "current company", "current firm"]
CONTACT_ALIASES["title"] += ["current_title", "currenttitle", "current title"]
CONTACT_ALIASES["practice_areas"] += ["practiceareas", "specialty", "primary practice area"]
CONTACT_ALIASES["years_experience"] += ["yearsexperience"]
CONTACT_ALIASES["do_not_contact"] += ["dnu", "is_dnu", "isdnu", "do not use", "dnu status"]
CONTACT_ALIASES["city"] += ["candidate city"]
for k in CONTACT_ALIASES:
    CONTACT_ALIASES[k] = [a.lower() for a in CONTACT_ALIASES[k]]

# Business rules carried over from VortexRecruitCRM (replit.md).
BLOCKED_RECIPIENT_DOMAINS = {"wbd-us.com"}
BLOCKED_SENDER_DOMAINS = {"prividential.ai", "chrismooreesq.com", "daniellenimoxjohnsonlegal.com"}

PARTNER_RE = re.compile(
    r"\b(partner|shareholder|principal|managing member|equity member|member of the firm)\b", re.I)
NOT_PARTNER_RE = re.compile(
    r"\b(associate partner|partner track|partnerships? (manager|coordinator)|paralegal|"
    r"legal assistant|secretary|business development|marketing|recruit\w*|staff)\b", re.I)

QUEUE_COLS = ["contact_key", "to", "name", "firm", "title", "practice", "subject", "body", "prepared_at"]
LOG_COLS = ["sent_at", "to", "name", "firm", "subject", "transport", "status", "detail", "kind", "practice"]


# ---------- helpers ----------

def domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def domain_blocked(email: str, blocked: set[str]) -> bool:
    d = domain(email)
    return any(d == b or d.endswith("." + b) for b in blocked)


def is_partner(title: str, pattern: re.Pattern = PARTNER_RE) -> bool:
    return bool(title) and bool(pattern.search(title)) and not NOT_PARTNER_RE.search(title)


ACRONYMS = {"M&A", "IP", "ERISA", "ESG", "HIPAA", "GDPR", "CCPA", "SEC", "FCPA", "ADR", "REIT", "PE", "VC",
            "FDA", "FCC", "FERC", "IPO", "ITC", "PTAB", "ADA", "FINRA", "CFIUS", "UCC", "EB-5", "NLRB", "OSHA"}


def parse_areas(value: str) -> list[str]:
    """Handles 'A; B', 'A, B' and Postgres arrays like {"REAL ESTATE",CONSTRUCTION}."""
    v = (value or "").strip()
    if v.startswith("{") and v.endswith("}"):
        return [x.strip() for x in next(csv.reader([v[1:-1]])) if x.strip() and x.strip().upper() != "NULL"]
    return split_list(v)


# Tags too generic to describe someone's practice; used only if nothing else is on record.
GENERIC_AREAS = {"general practice", "transactional", "compliance", "regulatory", "general liability",
                 "unassigned", "other", "admin", "general", "counseling", "consulting"}


def display_practice(areas: list[str]) -> str:
    def fmt(a: str) -> str:
        return " ".join(w if w.upper() in ACRONYMS else w.lower() for w in a.split())
    areas = [fmt(a) for a in areas if a]
    specific = [a for a in areas if a not in GENERIC_AREAS]
    return " and ".join((specific or areas)[:2])


ENTITY_SUFFIX_RE = re.compile(
    r"\b(l\.?\s?l\.?\s?p|l\.?\s?l\.?\s?c|p\.?\s?l\.?\s?l\.?\s?c|p\.?\s?l\.?\s?c|a\.?\s?p\.?\s?l\.?\s?c|a\.?\s?p\.?\s?c|"
    r"l\.?\s?p\.?\s?a|p\.?\s?c|p\.?\s?a|s\.?\s?c|l\.?\s?c|ltd|limited|inc|incorporated|chartered|chtd|co)\b\.?", re.I)
FIRM_FILLER_RE = re.compile(r"\b(attorneys?|counsel+ors?|lawyers|at law|law firm|law group|law offices?|"
                            r"legal group|& associates|and associates)\b.*$", re.I)


def short_firm(name: str) -> str:
    """How a partner would say their own firm: first two words, no entity suffix.

    "Dawda Mann Mulcahy & Sadler PLC" -> "Dawda Mann"; "Honigman LLP Attorneys & Counselors" -> "Honigman";
    "Vorys, Sater, Seymour and Pease, LLP" -> "Vorys Sater"; "Kaufman & Dolowich" -> "Kaufman";
    "Law Offices of Jane Doe, P.A." -> "Jane Doe".
    """
    n = (name or "").strip()
    n = re.sub(r"^(the\s+)?law\s+offices?\s+of\s+", "", n, flags=re.I)
    n = ENTITY_SUFFIX_RE.split(n)[0] if ENTITY_SUFFIX_RE.search(n) else n
    n = FIRM_FILLER_RE.sub("", n)
    n = re.sub(r"\b([A-Za-z]{1,2})\s*&\s*([A-Za-z]{1,2})\b", r"\1&\2", n)  # "K & L Gates" -> "K&L Gates"
    words = [w.strip(",.;") for w in n.replace(",", " ").split() if w.strip(",.;")]
    words = words[:2]
    while words and words[-1].lower() in {"&", "and", "of", "the", "+"}:
        words.pop()
    out = " ".join(words) or (name or "").strip()
    return tidy_name(out) if out.isupper() and len(out) > 4 else out


NOT_A_NAME = {"open", "unknown", "test", "na", "n/a", "none", "null", "partner", "attorney", "esq", "mr", "mrs",
              "ms", "dr", "hon", "info", "admin", "office", "contact", "firm", "law", "legal"}


def greeting_name(first: str) -> str:
    """The name to greet someone by, or "" if the record only has initials ("K C", "E.")."""
    parts = [p for p in re.split(r"\s+", (first or "").strip()) if p]
    if parts and parts[0].lower().strip(".") in NOT_A_NAME:
        return ""
    real = [p for p in parts if len(p.strip(".")) > 1]
    if not real:
        return ""
    if len(parts[0].strip(".")) <= 1:      # "E Chase" -> "Chase"
        return real[0]
    return " ".join(parts)                   # "Anne Marie", "Mary-Ellen"


def name_from_email(email: str, last_name: str) -> str:
    """'lisa.colon@saul.com' + 'Colon' -> 'Lisa'. Only for first.last style addresses."""
    local = email.split("@")[0].lower()
    last = re.sub(r"[^a-z]", "", (last_name or "").lower().split(" ")[0])
    m = re.match(r"^([a-z]{2,})[._-]([a-z]+)", local)
    if m and last and m.group(2).startswith(last[:4]):
        return m.group(1).title()
    return ""


def tidy_name(s: str) -> str:
    s = (s or "").strip()
    return s.title() if s.isupper() or s.islower() else s


def read_rows(path: Path) -> list[dict]:
    return read_csv(path) if path.exists() else []


def write_rows(path: Path, cols: list[str], rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def append_row(path: Path, cols: list[str], row: dict) -> None:
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header and header != cols:
            write_rows(path, cols, read_rows(path))
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


class Suppressed(set):
    """Emails never to contact. Rows like "@firm.com" block a whole domain (and its subdomains)."""
    def __init__(self):
        super().__init__()
        self.domains: set[str] = set()

    def add(self, e: str) -> None:
        e = e.strip().lower().rstrip("/")
        if e.startswith("@"):
            self.domains.add(e[1:])
        elif e:
            super().add(e)

    def __contains__(self, email) -> bool:
        email = str(email).lower()
        return super().__contains__(email) or domain_blocked(email, self.domains)


def load_suppressed(state: Path, extra: list[Path]) -> Suppressed:
    out = Suppressed()
    for p in [state / "suppression.csv", *extra]:
        for r in read_rows(p):
            out.add(r.get("email") or r.get("Email") or next(iter(r.values()), "") or "")
    return out


def recently_emailed(state: Path, days: int) -> set[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = set()
    for r in read_rows(state / "sent_log.csv"):
        if r.get("status") != "sent":
            continue
        try:
            if datetime.fromisoformat(r["sent_at"]) >= cutoff:
                out.add(r["to"].lower())
        except (KeyError, ValueError):
            continue
    return out


def fix_subject(subject: str) -> str:
    # CRM rule: never send "I'm house roles"; it should be "In-house roles".
    return re.sub(r"I[’']m house roles", "In-house roles", subject, flags=re.I)


def sender_signature(a) -> str:
    contact = " | ".join(x for x in [a.sender_phone, a.sender_email] if x)
    return "\n".join(x for x in [
        a.sender_name, ", ".join(x for x in [a.sender_title, a.sender_company] if x), contact, a.sender_website,
        getattr(a, "sender_address", "")] if x)


# CRM practice tags -> how a recruiter would say them.
PLAIN_PRACTICE = {
    "labor & employment": "employment", "labor and employment": "employment", "employment": "employment",
    "trusts & estates": "estate planning", "trusts and estates": "estate planning", "estate planning": "estate planning",
    "mergers & acquisitions": "M&A", "mergers and acquisitions": "M&A", "m&a": "M&A",
    "intellectual property": "IP", "ip": "IP", "patent": "patent", "patents": "patent", "trademark": "trademark",
    "environment": "environmental", "environmental": "environmental", "healthcare": "health care",
    "health care": "health care", "bankruptcy": "restructuring", "restructuring": "restructuring",
    "bankruptcy & restructuring": "restructuring", "banking": "banking", "banking & finance": "finance",
    "finance": "finance", "corporate": "corporate", "real estate": "real estate", "tax": "tax",
    "litigation": "litigation", "commercial litigation": "commercial litigation", "insurance": "insurance",
    "insurance coverage": "insurance coverage", "white collar": "white collar", "securities": "securities",
    "construction": "construction", "employee benefits": "employee benefits", "erisa": "employee benefits",
    "erisa-c&b": "employee benefits", "exec compensation": "executive compensation",
    "private equity": "private equity", "antitrust": "antitrust", "energy": "energy",
    "product liability": "product liability", "professional liability": "professional liability",
    "medical malpractice": "medical malpractice", "elder care": "elder law", "franchise": "franchise",
    "land use": "land use", "public finance": "public finance", "government contracts": "government contracts",
    "privacy": "privacy", "data privacy": "privacy", "cybersecurity": "cybersecurity", "appellate": "appellate",
    "family law": "family law", "investigations": "investigations", "wage & hour": "wage and hour",
}


def plain_practice(areas: list[str]) -> str:
    """One practice area, in plain words: the first specific tag on record."""
    cleaned = [a.strip() for a in areas if a and a.strip()]
    specific = [a for a in cleaned if a.lower() not in GENERIC_AREAS] or cleaned
    if not specific:
        return ""
    tag = specific[0].lower()
    if tag in PLAIN_PRACTICE:
        return PLAIN_PRACTICE[tag]
    return " ".join(w if w.upper() in ACRONYMS else w.lower() for w in specific[0].split())


def spin(text: str, seed: str) -> str:
    """Pick one option from each [[a||b||c]] block, the same way every time for a given seed."""
    import hashlib
    counter = iter(range(10_000))

    def pick(m):
        opts = [o.strip() for o in m.group(1).split("||")]
        h = hashlib.sha256(f"{seed}:{next(counter)}".encode()).digest()
        return opts[h[0] % len(opts)]
    return re.sub(r"\[\[(.+?)\]\]", pick, text, flags=re.S)


def practice_vars(practice: str) -> dict:
    return {
        "practice": practice,
        "practice_desc": f"{practice} practice" if practice else "practice",
        "practice_roles": f"{practice[0].upper() + practice[1:]} partner roles" if practice else "Partner roles",
    }


def to_html(body: str) -> str:
    """Plain text -> minimal HTML Outlook won't reflow: one <p> per paragraph, <br> inside (signature).
    No styles, images or tracking."""
    import html
    paras = [p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    return "".join("<p>" + "<br>".join(html.escape(l, quote=False) for l in p.splitlines()) + "</p>" for p in paras)


def render_body(tpl: Template, vals: dict, seed: str = "") -> str:
    body = tpl.safe_substitute(vals)
    body = spin(body, seed or vals.get("first_name", ""))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(l.rstrip() for l in body.splitlines())).strip() + "\n"


def parse_followup_days(value: str) -> list[int]:
    days = [int(x) for x in re.split(r"[,\s]+", str(value).strip()) if x and int(x) > 0]
    if days != sorted(set(days)):
        raise ValueError("follow-up days must be increasing, e.g. 5,30")
    return days


# Follow-ups must open with substance, never a reminder ("just bumping this", "following up",
# "bringing this to the top of your inbox", ...). Checked against the first sentence of the body.
REMINDER_OPENER_RE = re.compile(
    r"\b(bump\w*|follow\w*[- ]?up|following|circl\w* back|check\w* in|touch\w* base|"
    r"top of (?:your|you.re|the) inbox|(?:bring|brought|bringing|move|moving|put|putting) (?:this|it) (?:back )?(?:up|to the top)|"
    r"resurfac\w*|re-?surfac\w*|got (?:lost|buried)|buried|slipped through|fell through|"
    r"reminder|remind\w*|nudg\w*|ping\w*|re-?send\w*|re-?sent|"
    r"my (?:last|previous|earlier|prior|other|first) (?:note|email|e-mail|message)|"
    r"(?:last|previous|earlier|prior) (?:note|email|e-mail|message)|per my|as (?:i )?mentioned|"
    r"reached out|reaching out again|wanted to (?:bring|make sure|loop)|loop\w* back|"
    r"in case (?:you|it|this) (?:missed|got|was)|in case you|did you (?:get|see|have a chance)|"
    r"haven.t heard|not heard back|any thoughts on|see below|below email|"
    r"one more time|again about|still interested)\b", re.I)


def opening_sentence(body: str) -> str:
    lines = [l for l in body.strip().splitlines() if l.strip()]
    text = " ".join(lines[1:3]) if lines and re.match(r"^(hi|hello|dear)\b", lines[0], re.I) else " ".join(lines[:2])
    return re.split(r"(?<=[.?!])\s", text, maxsplit=1)[0]


def check_followup_opening(body: str) -> None:
    """Follow-ups never refer to the earlier email or nudge ("following up", "top of your inbox", ...),
    anywhere in the message body (the signature is not checked)."""
    text = body.split("Best regards")[0]
    m = REMINDER_OPENER_RE.search(text)
    if m:
        raise ValueError(f"Follow-up contains a reminder phrase ({m.group(0)!r}): "
                         f"{text[max(0, m.start() - 40):m.end() + 40]!r}")


def due_followups(log: list[dict], days: list[int], now: datetime) -> list[tuple[int, dict]]:
    """(step, first-email row) for people due their next follow-up.

    Step n (1-based) is due once the first email is at least days[n-1] old, every earlier
    step is done, step n isn't, there's no recorded reply, and the previous touch was at
    least days[n-1] - days[n-2] days ago (so a late follow-up 1 still gets spacing).
    """
    first: dict[str, dict] = {}
    done: dict[str, dict[int, datetime]] = {}
    replied: set[str] = set()
    for r in log:
        to = (r.get("to") or "").lower()
        kind = r.get("kind") or "initial"
        if r.get("status") == "replied":
            replied.add(to)
        elif kind == "initial" and r.get("status") == "sent" and to not in first:
            first[to] = r
        elif kind.startswith("followup") and r.get("status") in ("sent", "skipped"):
            step = int(kind[len("followup"):] or 1)
            done.setdefault(to, {})[step] = datetime.fromisoformat(r["sent_at"])
    out = []
    for to, r in first.items():
        if to in replied:
            continue
        steps = done.get(to, {})
        step = next((n for n in range(1, len(days) + 1) if n not in steps), None)
        if step is None:
            continue
        start = datetime.fromisoformat(r["sent_at"])
        last = steps.get(step - 1, start)
        gap = days[step - 1] - (days[step - 2] if step > 1 else 0)
        if now - start >= timedelta(days=days[step - 1]) and now - last >= timedelta(days=gap):
            out.append((step, r))
    return out


# ---------- prepare ----------

def cmd_prepare(a) -> int:
    state: Path = a.state
    state.mkdir(parents=True, exist_ok=True)
    if domain_blocked(a.sender_email, BLOCKED_SENDER_DOMAINS):
        print(f"Refusing: {domain(a.sender_email)} is on the CRM's do-not-send-from list.", file=sys.stderr)
        return 2
    if not a.sender_address:
        print("Refusing: --sender-address (physical mailing address) is required for commercial email.",
              file=sys.stderr)
        return 2

    pattern = re.compile(a.title_regex, re.I) if a.title_regex else PARTNER_RE
    suppressed = load_suppressed(state, a.suppress_file)
    recent = recently_emailed(state, a.recontact_days)
    queued = {r["to"].lower() for r in read_rows(state / "queue.csv")}

    body_tpl = Template(a.template.read_text(encoding="utf-8"))
    subjects = [l.strip() for l in a.subjects.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.lstrip().startswith("#")]
    signature = sender_signature(a)

    new_rows, skipped, seen = [], [], set()
    for raw in read_csv(a.contacts):
        c = canon_contact(raw)
        # A cell may hold several addresses ("a@x.com; b@y.com"): use the first valid one.
        email = next((e for e in re.split(r"[\s,;|]+", c["email"].strip().lower())
                      if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e)), c["email"].strip().lower())
        c["first_name"], c["last_name"], c["city"] = map(tidy_name, (c["first_name"], c["last_name"], c["city"]))
        name = f"{c['first_name']} {c['last_name']}".strip()

        def skip(reason):
            skipped.append({"name": name, "email": email, "title": c["title"], "reason": reason})

        if not is_partner(c["title"], pattern):
            skip("not a partner title"); continue
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            skip("missing/invalid email"); continue
        if truthy(c["do_not_contact"]):
            skip("DNU / opted out in CRM"); continue
        if email in suppressed:
            skip("on suppression list"); continue
        if domain_blocked(email, BLOCKED_RECIPIENT_DOMAINS):
            skip("blocked recipient domain"); continue
        if email in recent:
            skip(f"emailed within {a.recontact_days} days"); continue
        if email in queued or email in seen:
            skip("already queued / duplicate"); continue
        if not greeting_name(c["first_name"]):
            c["first_name"] = name_from_email(email, c["last_name"])
            if not c["first_name"]:
                skip("first name is initials only"); continue
        c["first_name"] = greeting_name(c["first_name"])
        areas = parse_areas(c["practice_areas"])
        excluded = {x.strip().lower() for x in a.exclude_practice.split(",") if x.strip()}
        if areas and areas[0].strip().lower() in excluded:
            skip(f"main practice excluded ({areas[0].strip().lower()})"); continue
        if not areas and a.require_practice:
            skip("no practice area on record"); continue
        seen.add(email)

        practice = plain_practice(areas)
        vals = {
            "first_name": c["first_name"] or "there",
            "firm": short_firm(c["firm"]) or "your firm",
            "title": c["title"],
            **practice_vars(practice),
            "city_phrase": f" in {c['city']}" if c["city"] else "",
            "sender_name": a.sender_name, "sender_company": a.sender_company,
            "sender_signature": signature, "sender_address": a.sender_address,
            "unsubscribe_email": a.unsubscribe_email or a.sender_email,
        }
        body = render_body(body_tpl, vals, seed=email)
        import hashlib
        subj_tpl = Template(subjects[hashlib.sha256(("subject:" + email).encode()).digest()[0] % len(subjects)])
        subject = fix_subject(re.sub(r"\s+", " ", subj_tpl.safe_substitute(vals)).strip())
        new_rows.append({
            "contact_key": c["_raw"].get("id") or c["_raw"].get("contact_id") or email,
            "to": email, "name": name, "firm": c["firm"], "title": c["title"],
            "practice": practice, "subject": subject, "body": body,
            "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    write_rows(state / "queue.csv", QUEUE_COLS, read_rows(state / "queue.csv") + new_rows)
    write_rows(state / "prepare_skipped.csv", ["name", "email", "title", "reason"], skipped)
    reasons: dict[str, int] = {}
    for s in skipped:
        reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
    print(f"Queued {len(new_rows)} partner(s); skipped {len(skipped)}.")
    for r, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  skip {n:>5}  {r}")
    if new_rows:
        print("\nSample:\nSubject: " + new_rows[0]["subject"] + "\n\n" + new_rows[0]["body"])
    return 0


# ---------- send ----------

def send_sendgrid(a, row) -> tuple[bool, str]:
    key = os.environ.get("SENDGRID_API_KEY")
    if not key:
        return False, "SENDGRID_API_KEY not set"
    unsub = a.unsubscribe_email or a.sender_email
    payload = {
        "personalizations": [{"to": [{"email": row["to"], "name": row["name"]}]}],
        "from": {"email": a.sender_email, "name": a.sender_name},
        "reply_to": {"email": a.reply_to or a.sender_email},
        "subject": row["subject"],
        "content": [{"type": "text/plain", "value": row["body"]}],
        "headers": {"List-Unsubscribe": f"<mailto:{unsub}?subject=unsubscribe>"},
        "categories": ["partner-outreach"],
        "tracking_settings": {"click_tracking": {"enable": False}, "open_tracking": {"enable": False}},
    }
    req = urllib.request.Request("https://api.sendgrid.com/v3/mail/send", data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status in (200, 202), f"HTTP {r.status} {r.headers.get('X-Message-Id', '')}".strip()
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}"
    except urllib.error.URLError as e:
        return False, f"network: {e.reason}"


def send_smtp(a, row) -> tuple[bool, str]:
    host, user, pw = os.environ.get("SMTP_HOST"), os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if not (host and user and pw):
        return False, "SMTP_HOST / SMTP_USER / SMTP_PASSWORD not set"
    msg = EmailMessage()
    msg["From"] = f"{a.sender_name} <{a.sender_email}>"
    msg["To"] = f"{row['name']} <{row['to']}>"
    msg["Reply-To"] = a.reply_to or a.sender_email
    msg["Subject"] = row["subject"]
    msg["Message-ID"] = make_msgid(domain=domain(a.sender_email) or None)
    msg["List-Unsubscribe"] = f"<mailto:{a.unsubscribe_email or a.sender_email}?subject=unsubscribe>"
    msg.set_content(row["body"])
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(user, pw)
            s.send_message(msg)
        return True, msg["Message-ID"]
    except (smtplib.SMTPException, OSError) as e:
        return False, f"smtp: {e}"


def cmd_send(a) -> int:
    state: Path = a.state
    if domain_blocked(a.sender_email, BLOCKED_SENDER_DOMAINS):
        print(f"Refusing: {domain(a.sender_email)} is on the CRM's do-not-send-from list.", file=sys.stderr)
        return 2
    now = datetime.now(ET)
    if a.live and not a.ignore_hours and (now.weekday() >= 5 or not (a.start_hour <= now.hour < a.end_hour)):
        print(f"Outside sending window ({now:%a %H:%M} ET; weekdays {a.start_hour}:00-{a.end_hour}:00). Nothing sent.")
        return 0

    log = read_rows(state / "sent_log.csv")
    today = now.date()
    sent_today = sum(1 for r in log if r.get("status") == "sent"
                     and datetime.fromisoformat(r["sent_at"]).astimezone(ET).date() == today)
    budget = max(0, a.daily_cap - sent_today)
    if budget == 0 and a.live:
        print(f"Daily cap of {a.daily_cap} already reached ({sent_today} sent today).")
        return 0

    missing = {"sendgrid": [] if os.environ.get("SENDGRID_API_KEY") else ["SENDGRID_API_KEY"],
               "smtp": [v for v in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD") if not os.environ.get(v)],
               "outlook": []}[a.transport]
    if a.live and missing:
        print(f"Refusing to send: {', '.join(missing)} not set.", file=sys.stderr)
        return 2

    suppressed = load_suppressed(state, a.suppress_file)
    recent = recently_emailed(state, a.recontact_days)
    queue = read_rows(state / "queue.csv")
    firm_key = lambda f: re.sub(r"[^a-z0-9]+", " ", (f or "").lower()).strip()
    firms_today: dict[str, int] = {}
    for r in log:
        if r.get("status") == "sent" and datetime.fromisoformat(r["sent_at"]).astimezone(ET).date() == today:
            firms_today[firm_key(r.get("firm"))] = firms_today.get(firm_key(r.get("firm")), 0) + 1
    remaining, attempted, sent, failed = [], 0, 0, 0
    transport = {"sendgrid": send_sendgrid, "smtp": send_smtp, "outlook": None}[a.transport]
    batch = []
    limit = min(budget if a.live else a.daily_cap, a.max_per_run)

    # Follow-ups go first: at most one per person per day, on the `--followup-days` schedule
    # (days after the first email), and only while there's no reply.
    followups = []
    fu_days = parse_followup_days(a.followup_days)
    if fu_days:
        templates = {n: Template((HERE / "templates" / f"partner_followup_{n}_email.txt").read_text(encoding="utf-8"))
                     for n in range(1, len(fu_days) + 1)}
        for step, r in due_followups(log, fu_days, datetime.now(timezone.utc)):
            fu_tpl = templates[step]
            practice_work = (r.get("practice") or "").strip()
            to = r["to"].lower()
            if to in suppressed or domain_blocked(to, BLOCKED_RECIPIENT_DOMAINS):
                continue
            practice = r.get("practice") or ""
            body = render_body(fu_tpl, {
                "first_name": (r.get("name") or "").split(" ")[0] or "there",
                **practice_vars(practice),
                "practice_work": f"{practice} work" if practice else "your practice",
                "firm": r.get("firm") or "your firm",
                "sender_signature": sender_signature(a), "sender_address": a.sender_address,
                "unsubscribe_email": a.unsubscribe_email or a.sender_email}, seed=f"{to}:followup{step}")
            check_followup_opening(body)
            subj = r.get("subject") or ""
            followups.append({"to": to, "name": r.get("name", ""), "firm": r.get("firm", ""),
                              "practice": practice, "kind": f"followup{step}", "original_subject": subj,
                              "original_sent_at": r["sent_at"],
                              "subject": subj if subj.lower().startswith("re:") else f"RE: {subj}", "body": body})
    if followups and not a.sender_address:
        print("Refusing: follow-ups need --sender-address / OUTREACH_SENDER_ADDRESS.", file=sys.stderr)
        return 2

    for row in followups + queue:
        to = row["to"].lower()
        kind = row.get("kind", "initial")
        if kind == "initial" and (to in suppressed or domain_blocked(to, BLOCKED_RECIPIENT_DOMAINS) or to in recent):
            continue  # dropped from the queue for good
        fk = firm_key(row.get("firm"))
        if attempted >= limit or failed >= a.max_failures or (fk and firms_today.get(fk, 0) >= a.per_firm_daily):
            if kind == "initial":
                remaining.append(row)
            continue
        attempted += 1
        if fk:
            firms_today[fk] = firms_today.get(fk, 0) + 1
        row["subject"] = fix_subject(row["subject"])
        if not a.live:
            print(f"[dry-run] would send {kind} to {row['name']} <{to}>: {row['subject']}")
            if kind == "initial":
                remaining.append(row)
            continue
        if a.transport == "outlook":
            # Sent by the Outlook connector; results come back via `record`.
            # Follow-ups are sent as a reply in the original thread.
            batch.append({"kind": kind, "pause_seconds_before": random.randint(60, 300), **{k: row.get(k, "") for k in
                          ("to", "name", "firm", "practice", "subject", "body", "original_subject", "original_sent_at")},
                          "body_html": to_html(row.get("body", ""))})
            if kind == "initial":
                remaining.append(row)
            continue
        ok, detail = transport(a, row)
        append_row(state / "sent_log.csv", LOG_COLS, {
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "to": to, "name": row["name"],
            "firm": row["firm"], "subject": row["subject"], "transport": a.transport,
            "status": "sent" if ok else "failed", "detail": detail, "kind": kind, "practice": row.get("practice", "")})
        if ok:
            sent += 1; recent.add(to)
            print(f"sent   {row['name']} <{to}>")
        else:
            # Delivery outcome unknown or failed: keep it queued, never auto-retry in this run.
            failed += 1
            if kind == "initial":
                remaining.append(row)
            print(f"FAILED {row['name']} <{to}>: {detail}", file=sys.stderr)
        time.sleep(a.delay + random.uniform(0, a.delay))

    if a.transport == "outlook" and a.live:
        (state / "outbox.json").write_text(json.dumps(batch, indent=2))
        n_fu = sum(1 for b in batch if b["kind"] != "initial")
        print(f"OUTLOOK BATCH: {len(batch)} email(s) ({n_fu} follow-up, {len(batch) - n_fu} first) written to {state / 'outbox.json'} "
              f"(sent today so far: {sent_today}/{a.daily_cap}). Send each with outlook_send_mail, "
              f"then log results with `record`.")
        return 0
    if a.live:
        write_rows(state / "queue.csv", QUEUE_COLS, remaining)
    mode = "LIVE" if a.live else "DRY RUN"
    print(f"{mode}: {len(followups)} follow-up(s) due. {sent if a.live else attempted} {'sent' if a.live else 'would send'}, {failed} failed, "
          f"{len(remaining)} still queued. Sent today: {sent_today + sent}/{a.daily_cap}.")
    if failed >= a.max_failures:
        print(f"Stopped after {failed} failures; check sender credentials before the next run.", file=sys.stderr)
        return 1
    return 0


def cmd_record(a) -> int:
    """Log results of sends done outside this script (Outlook connector)."""
    state: Path = a.state
    queue = read_rows(state / "queue.csv")
    by_to = {r["to"].lower(): r for r in queue}
    last_sent = {r["to"].lower(): r for r in read_rows(state / "sent_log.csv") if r.get("status") == "sent"}
    outbox = state / "outbox.json"
    for b in (json.loads(outbox.read_text()) if outbox.exists() else []):
        if b.get("kind") == a.kind:
            by_to[b["to"].lower()] = b
    done = set()
    for to in a.emails:
        to = to.strip().lower()
        row = by_to.get(to) or last_sent.get(to) or {"to": to, "name": "", "firm": "", "subject": ""}
        append_row(state / "sent_log.csv", LOG_COLS, {
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "to": to,
            "name": row.get("name", ""), "firm": row.get("firm", ""), "subject": row.get("subject", ""),
            "transport": "outlook", "status": a.status, "detail": a.detail,
            "kind": a.kind if a.status != "replied" else "", "practice": row.get("practice", "")})
        if a.kind == "initial" and a.status in ("sent", "skipped"):
            done.add(to)
        print(f"{a.status:<7} {to}")
    write_rows(state / "queue.csv", QUEUE_COLS, [r for r in queue if r["to"].lower() not in done])
    return 0


def cmd_suppress(a) -> int:
    a.state.mkdir(parents=True, exist_ok=True)
    have = load_suppressed(a.state, [])
    for e in a.emails:
        e = e.strip().lower()
        if e and e not in have:
            append_row(a.state / "suppression.csv", ["email", "added_at", "reason"],
                       {"email": e, "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "reason": a.reason})
            print(f"suppressed {e}")
    return 0


# ---------- cli ----------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--state", type=Path, required=True, help="folder for queue, sent log, suppression list")
        p.add_argument("--suppress-file", type=Path, action="append", default=[],
                       help="extra CSV of emails never to contact (e.g. CRM DNU/unsubscribe export); repeatable")
        p.add_argument("--recontact-days", type=int, default=180, help="don't email the same person again within N days")
        p.add_argument("--sender-name", default=os.environ.get("OUTREACH_SENDER_NAME", ""))
        p.add_argument("--sender-email", default=os.environ.get("OUTREACH_SENDER_EMAIL", ""))
        p.add_argument("--unsubscribe-email", default="", help="address for unsubscribe replies (default: sender)")
        p.add_argument("--sender-title", default=os.environ.get("OUTREACH_SENDER_TITLE", ""))
        p.add_argument("--sender-company", default=os.environ.get("OUTREACH_SENDER_COMPANY", ""))
        p.add_argument("--sender-phone", default=os.environ.get("OUTREACH_SENDER_PHONE", ""))
        p.add_argument("--sender-website", default=os.environ.get("OUTREACH_SENDER_WEBSITE", ""))
        p.add_argument("--sender-address", default=os.environ.get("OUTREACH_SENDER_ADDRESS", ""),
                       help="physical mailing address (CAN-SPAM)")

    p = sub.add_parser("prepare", help="build personalized emails for partners in a CRM export")
    common(p)
    p.add_argument("--contacts", type=Path, required=True)
    p.add_argument("--template", type=Path, default=HERE / "templates" / "partner_outreach_email.txt")
    p.add_argument("--subjects", type=Path, default=HERE / "templates" / "partner_subjects.txt",
                   help="one subject per line; each person gets one, picked the same way every time")
    p.add_argument("--title-regex", default="", help="override which titles count as partner")
    p.add_argument("--require-practice", action="store_true", help="skip partners with no practice area on record")
    p.add_argument("--exclude-practice", default=os.environ.get(
        "OUTREACH_EXCLUDE_PRACTICE", "family law,personal injury,immigration"),
        help="comma-separated practices; partners whose main (first-listed) practice is one of these are skipped")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("send", help="send queued emails up to the daily cap")
    common(p)
    p.add_argument("--transport", choices=["outlook", "sendgrid", "smtp"], default="outlook")
    p.add_argument("--reply-to", default="")
    p.add_argument("--daily-cap", type=int, default=int(os.environ.get("OUTREACH_DAILY_CAP", "40")))
    p.add_argument("--max-per-run", type=int, default=int(os.environ.get("OUTREACH_MAX_PER_RUN", "10")),
                   help="emails per run; schedule several runs a day so sends are spread out")
    p.add_argument("--followup-days", default=os.environ.get("OUTREACH_FOLLOWUP_DAYS", "5,30"),
                   help="follow-up schedule in days after the first email, e.g. 5,30 (0 = none); "
                        "templates/partner_followup_<n>_email.txt is used for step n")
    p.add_argument("--per-firm-daily", type=int, default=1, help="max partners at the same firm per day")
    p.add_argument("--delay", type=float, default=20, help="base seconds between sends (plus random jitter)")
    p.add_argument("--start-hour", type=int, default=int(os.environ.get("OUTREACH_START_HOUR", "9")))
    p.add_argument("--end-hour", type=int, default=int(os.environ.get("OUTREACH_END_HOUR", "17")))
    p.add_argument("--ignore-hours", action="store_true")
    p.add_argument("--max-failures", type=int, default=3)
    p.add_argument("--live", action="store_true", help="actually send (default is a dry run)")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("record", help="log sends done via the Outlook connector")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--status", choices=["sent", "failed", "skipped", "replied"], default="sent",
                   help="skipped = not sent and removed from queue (e.g. prior Outlook contact); "
                        "replied = they answered, so no follow-up")
    p.add_argument("--kind", choices=["initial", "followup1", "followup2", "followup3"], default="initial")
    p.add_argument("--detail", default="")
    p.add_argument("emails", nargs="+")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("suppress", help="add emails to the do-not-contact list (e.g. unsubscribe replies)")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--reason", default="unsubscribe")
    p.add_argument("emails", nargs="+")
    p.set_defaults(func=cmd_suppress)

    a = ap.parse_args()
    if a.cmd in ("prepare", "send") and (not a.sender_name or not a.sender_email):
        ap.error("--sender-name and --sender-email are required (or set OUTREACH_SENDER_NAME / OUTREACH_SENDER_EMAIL)")
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
