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
import collections
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
import urllib.parse
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
    r"legal assistant|secretary|business development|marketing|recruit\w*|staff|admin\w*|assistant|"
    r"emerit\w*|retired|of counsel)\b", re.I)

QUEUE_COLS = ["contact_key", "to", "name", "firm", "title", "practice", "subject", "body", "prepared_at"]
LOG_COLS = ["sent_at", "to", "name", "firm", "subject", "transport", "status", "detail", "kind", "practice"]


# ---------- helpers ----------

def domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def domain_blocked(email: str, blocked: set[str]) -> bool:
    d = domain(email)
    return any(d == b or d.endswith("." + b) for b in blocked)


# Firm leadership is never emailed (Jon's rule): managing partners of any kind, founders,
# CEOs and other chiefs, presidents, owners, executive partners. Chairs and co-chairs ARE emailed.
LEADERSHIP_RE = re.compile(
    r"\b(managing|founding|founder\w*|co-?founder\w*|ceo|c\.e\.o|chief|coo|cfo|cmo|cto|cio|clo|"
    r"president|owner|proprietor|executive partner|executive director|"
    r"name partner|senior managing|firm leader|head of (?:the )?firm)\b", re.I)


def is_leadership(title: str) -> bool:
    return bool(LEADERSHIP_RE.search(title or ""))


def is_partner(title: str, pattern: re.Pattern = PARTNER_RE) -> bool:
    return bool(title) and bool(pattern.search(title)) and not NOT_PARTNER_RE.search(title)


# Law-firm associates. In-house titles ("Associate General Counsel"), summer associates (students),
# "Associate Partner" and non-attorney "associate" titles are not included.
ASSOCIATE_RE = re.compile(r"\b(associate|associate attorney)\b", re.I)
NOT_ASSOCIATE_RE = re.compile(
    r"\b(associate (partner|general counsel|director|dean|professor|editor|broker|attorney general|"
    r"vice president|counsel to)|summer associate|sales associate|paralegal|legal assistant|"
    r"recruit\w*|marketing|business development|staff|admin\w*|assistant|in-house)\b", re.I)


def is_associate(title: str) -> bool:
    return bool(title) and bool(ASSOCIATE_RE.search(title)) and not NOT_ASSOCIATE_RE.search(title)


def audience_template(audience: str, name: str) -> Path:
    """templates/<audience>_<name>, e.g. associate_outreach_email.txt (partner_ for partners)."""
    return HERE / "templates" / f"{audience}_{name}"


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


FIRM_STOPWORDS = {"and", "the", "of", "law", "llp", "llc", "pllc", "pc", "pa", "group", "firm", "offices", "office",
                  "attorneys", "counselors", "associates", "partners", "legal"}
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com", "me.com", "msn.com",
             "comcast.net", "att.net", "verizon.net", "live.com", "protonmail.com", "example.com", "example.org"}


def firm_matches_domain(firm: str, email: str) -> bool:
    """False when the email's domain clearly belongs to a different firm than the CRM says
    (the partner has likely moved, so the email would name the wrong firm)."""
    d = domain(email)
    if not d or d in FREE_MAIL or not firm:
        return True
    label = re.sub(r"[^a-z0-9]", "", d.split(".")[-2] if d.count(".") >= 1 else d)
    words = [w for w in re.findall(r"[a-z0-9]+", firm.lower()) if w not in FIRM_STOPWORDS]
    if not words:
        return True
    if any(len(w) >= 4 and w in label for w in words):
        return True
    if any(len(w) >= 4 and label.startswith(w[:4]) for w in words):
        return True
    initials = "".join(w[0] for w in words)
    return len(initials) >= 2 and (label.startswith(initials[:2]) or initials.startswith(label[:3]))


def domain_firm_index(path: Path) -> dict[str, tuple[str, float, int]]:
    """email domain -> (most common firm key, its share, row count), from a CRM partner export."""
    by: dict[str, collections.Counter] = {}
    for r in read_rows(path):
        e = (r.get("email") or "").lower()
        f = r.get("current_company") or r.get("firm") or ""
        if "@" in e and f:
            by.setdefault(domain(e), collections.Counter())[firm_key(short_firm(f))] += 1
    out = {}
    for d, c in by.items():
        top, n = c.most_common(1)[0]
        out[d] = (top, n / sum(c.values()), sum(c.values()))
    return out


def firm_key(f: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (f or "").lower()).strip()


def firm_is_current(firm: str, email: str, index: dict | None) -> bool:
    """Does the CRM firm still match the email address? Uses the domain's majority firm when there's
    enough data (3+ partners, 60%+ agreement), otherwise a name-vs-domain check."""
    d = domain(email)
    if index and d in index and d not in FREE_MAIL:
        top, share, n = index[d]
        if n >= 3 and share >= 0.6:
            k = firm_key(short_firm(firm))
            return bool(k) and (k == top or top.startswith(k + " ") or k.startswith(top + " "))
    return firm_matches_domain(firm, email)


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
    """Name, title, phone/email and website. The mailing address is not in the signature (Jon's rule):
    it goes at the very bottom of the email, in small type (see with_footer / to_html)."""
    contact = " | ".join(x for x in [a.sender_phone, a.sender_email] if x)
    return "\n".join(x for x in [
        a.sender_name, ", ".join(x for x in [a.sender_title, a.sender_company] if x), contact, a.sender_website] if x)


# The mailing address, set from --sender-address; to_html prints this paragraph small and grey.
FOOTER_TEXT = ""


def with_footer(body: str, address: str) -> str:
    """Body plus the mailing address as its own last paragraph (CAN-SPAM), below the signature."""
    body = body.rstrip()
    if not address or body.endswith(address):
        return body + "\n"
    return body + "\n\n" + address + "\n"


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


# Employer values that describe how someone works rather than name a firm. Never written into an
# email and never counted as one firm ("Independent Legal", "Law Offices", "Contract Attorney").
GENERIC_FIRM_RE = re.compile(
    r"^(the )?(law (office|offices|firm|group|practice)( of)?|attorneys? at law|private (practice|legal)|"
    r"self[- ]employed|solo( practi\w*)?|independent|freelance|consultant|retired|unemployed|n/?a|none|"
    r"(independent|freelance|contract|solo|private) (legal|attorney|lawyer|counsel|practitioner)( \w+)?|"
    r"legal (consultant|consulting|services|counsel)|attorney|lawyer|legal|law)$", re.I)


def about_line(practice: str, firm: str, seed: str) -> str:
    """Opening line built only from details on record (no placeholders like "your firm")."""
    if GENERIC_FIRM_RE.match((firm or "").strip(" .,")):
        firm = ""
    if practice and firm:
        opts = [f"Your {practice} practice at {firm} stood out to me.", f"Your {practice} practice at {firm} caught my eye.",
                f"I came across your {practice} practice at {firm}."]
    elif firm:
        opts = [f"I came across your work at {firm}.", f"Your work at {firm} caught my eye."]
    elif practice:
        opts = [f"Your {practice} practice stood out to me.", f"I came across your {practice} practice."]
    else:
        return ""
    import hashlib
    return opts[hashlib.sha256(("about:" + seed).encode()).digest()[0] % len(opts)]


def practice_vars(practice: str, audience: str = "partner") -> dict:
    return {
        "practice": practice,
        "practice_desc": f"{practice} practice" if practice else "practice",
        "practice_roles": (f"{practice[0].upper() + practice[1:]} {audience} roles" if practice
                           else f"{audience.capitalize()} roles"),
    }


def to_html(body: str) -> str:
    """Plain text -> minimal HTML Outlook won't reflow: one <p> per paragraph, <br> inside (signature).
    No styles, images or tracking."""
    import html
    paras = [p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    out = []
    for p in paras:
        inner = "<br>".join(html.escape(l, quote=False) for l in p.splitlines())
        if FOOTER_TEXT and p.strip() == FOOTER_TEXT.strip():
            out.append(f'<p style="font-size:10px;color:#888888;margin-top:18px"><small>{inner}</small></p>')
        else:
            out.append("<p>" + inner + "</p>")
    return "".join(out)


def quoted_reply_html(followup_body: str, original: dict | None, sender_name: str, sender_email: str) -> str:
    """Follow-up body with the first email quoted under it, Outlook-style, so a direct send reads as a reply."""
    import html
    out = to_html(followup_body)
    if not original or not original.get("body"):
        return out
    try:
        sent = datetime.fromisoformat(original["sent_at"]).astimezone(ET).strftime("%A, %B %-d, %Y %-I:%M %p")
    except (KeyError, ValueError):
        sent = ""
    head = (f"<p><b>From:</b> {html.escape(sender_name, quote=False)} &lt;{html.escape(sender_email, quote=False)}&gt;<br>"
            f"<b>Sent:</b> {sent}<br><b>To:</b> {html.escape(original.get('name') or original.get('to', ''), quote=False)}<br>"
            f"<b>Subject:</b> {html.escape(original.get('subject', ''), quote=False)}</p>")
    return out + "<hr>" + head + to_html(original["body"])


def load_originals(state: Path) -> dict[str, dict]:
    out = {}
    p = state / "originals.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                out.setdefault(r["to"].lower(), r)
    return out


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


# ---------- personal email + verification ----------

PERSONAL_EMAIL_COLS = ["personal_email", "personal email", "personalemail", "home email", "home_email",
                       "private email", "other email"]
# Consumer mailbox providers. An address in a generic email column only counts as personal on one of these.
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "rocketmail.com", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me",
    "pm.me", "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "bellsouth.net", "cox.net",
    "charter.net", "earthlink.net", "optonline.net", "gmx.com", "gmx.us", "mail.com", "zoho.com",
    "fastmail.com", "hey.com", "yahoo.co.uk", "hotmail.co.uk", "btinternet.com", "rogers.com", "shaw.ca",
}
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
VALID_STATUSES = {"valid", "deliverable", "ok", "verified", "safe"}


FREEMAIL_BRANDS = {"gmail", "googlemail", "yahoo", "ymail", "hotmail", "live", "outlook", "msn", "aol",
                   "icloud", "gmx", "protonmail", "yandex", "zoho"}


def is_freemail(email: str) -> bool:
    """Consumer mailbox, including country variants (live.fr, yahoo.co.in, hotmail.it)."""
    d = domain(email)
    return d in FREEMAIL_DOMAINS or d.split(".")[0] in FREEMAIL_BRANDS


ACADEMIC_RE = re.compile(r"(\.edu|^byulaw\.net)$|(^|\.)alumni?\.", re.I)


def is_personal_domain(email: str, allowed: set[str] = frozenset()) -> bool:
    """Consumer mailbox, school/alumni address, or a domain the user approved. Anything else in a
    personal-email field is usually a current or former firm mailbox (shefskylaw.com, bakernet.com)."""
    d = domain(email)
    return is_freemail(email) or bool(ACADEMIC_RE.search(d)) or d in allowed


def split_emails(value: str) -> list[str]:
    return [e for e in re.split(r"[\s,;|]+", (value or "").strip().lower()) if EMAIL_RE.fullmatch(e)]


def personal_email(raw: dict) -> str:
    """The contact's personal address: a personal-email column first, else a consumer-domain
    address in any email column. Empty if they only have a firm address."""
    lower = {k.strip().lower(): v for k, v in raw.items() if k}
    for col in PERSONAL_EMAIL_COLS:
        found = split_emails(lower.get(col, ""))
        if found:
            return found[0]
    for col, v in lower.items():
        if "email" in col or col == "emails":
            for e in split_emails(v):
                if is_freemail(e):
                    return e
    return ""


LAW_FIRM_RE = re.compile(
    r"(\bl\.?l\.?p\b|\bp\.?l\.?l\.?c\b|\bp\.?c\.?$|\bp\.?a\.?$|\blaw\b|\blegal\b|\battorneys?\b|"
    r"\blawyers?\b|\bchartered\b|& associates\b|\blaw (firm|group|offices?)\b|\bcounsel\b)", re.I)
NOT_LAW_FIRM_RE = re.compile(
    r"\b(district attorney|attorney general|public defender|county|department|state of|city of|"
    r"u\.?s\.? |federal|court|university|school|college|e-?discovery|staffing|recruit\w*|consulting|"
    r"insurance|bank|axiom|quimbee|epiq|legal aid|legal services|legal assistance|public interest|"
    r"justice|clinic|foundation|society|association|legal advis[oe]r|^office of)\b", re.I)
LAWYER_HINT_RE = re.compile(r"(esq|atty|lawyer|\blaw|law\b|jd\b|\bjd)", re.I)


# Solo practices and truncated imports ("Law Offices of Amanda Chacon", "Law Office", "The Law"):
# skipped in the no-title drip (Jon: a partner/associate move pitch fits them least).
SOLO_PRACTICE_RE = re.compile(r"^\s*(the\s+)?law\s+offices?\b|\blaw\s+offices?\s+of\b|^\s*(the\s+)?law\s*$", re.I)


def _firm_words(name: str) -> list[str]:
    return re.sub(r"[^a-z ]+", " ", (name or "").lower().replace("’", "").replace("'", "")).split()


def known_firms(paths: list[Path]) -> set[str]:
    """Firm names from the partner exports (and the reviewed extra list): each firm's short name, plus
    its first two words when 3+ partners share them ("cozen o connor", "fisher phillips")."""
    out, prefixes = set(), {}
    for p in paths:
        for r in read_rows(p):
            low = {k.strip().lower(): v for k, v in r.items() if k}
            name = low.get("current_company") or low.get("firm") or ""
            f = short_firm(name).lower()
            if len(f) > 3:
                out.add(f)
            w = _firm_words(name)
            if len(w) >= 2:
                k = " ".join(w[:2]); prefixes[k] = prefixes.get(k, 0) + (3 if "firm" in low else 1)
    return out | {k for k, n in prefixes.items() if n >= 3}


def looks_like_firm_lawyer(company: str, email: str, name: str, firms: set[str]) -> bool:
    """For records with no title: true when the employer looks like a law firm, or when no employer
    is on record but the email or name signals a lawyer (esq, atty, law)."""
    company = (company or "").strip()
    if company:
        if NOT_LAW_FIRM_RE.search(company):
            return False
        w = _firm_words(company)
        return (bool(LAW_FIRM_RE.search(company)) or short_firm(company).lower() in firms
                or (len(w) >= 2 and " ".join(w[:2]) in firms))
    local = email.split("@")[0]
    return bool(LAWYER_HINT_RE.search(local) or re.search(r"\besq\b", name, re.I))


def firm_domains(paths: list[Path], contacts: list[dict]) -> set[str]:
    """Domains seen on work addresses (this export plus any --firm-domains-from CSVs). A 'personal'
    address on one of these is a firm mailbox (often a former firm), not a personal one."""
    out = set()
    work_cols = ("work_email", "work email", "workemail", "email", "emails")
    for rows in [contacts, *[read_rows(p) for p in paths]]:
        for r in rows:
            low = {k.strip().lower(): v for k, v in r.items() if k}
            personal = set(split_emails(low.get("personal_email", "") or low.get("personal email", "")))
            for col in work_cols:
                for e in split_emails(low.get(col, "")):
                    # Imports often copy the personal address into the work column; school and alumni
                    # domains are never a firm's.
                    if e in personal or is_freemail(e) or ACADEMIC_RE.search(domain(e)):
                        continue
                    out.add(domain(e))
    return out


def load_verified(paths: list[Path]) -> dict[str, str]:
    """email -> status from an email-verifier export (ZeroBounce, NeverBounce, Kickbox, Bouncer...).
    Recognizes columns like email/Email Address and status/result/ZB Status."""
    out: dict[str, str] = {}
    for p in paths:
        for r in read_rows(p):
            low = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            email = next((low[k] for k in ("email", "personal_email", "email address", "email_address", "address") if low.get(k)), "")
            status = next((low[k] for k in ("verification_status", "status", "result", "zb status", "verification status",
                                            "verdict") if low.get(k)), "")
            if email:
                out[email.lower()] = status.lower()
    return out


def person_key(name: str, firm: str) -> tuple[str, str]:
    return (name.strip().lower(), short_firm(firm or "").lower())


def contacted_people(states: list[Path], include_queued: bool = True) -> set[tuple[str, str]]:
    """(name, firm) of everyone sent (or replied) in other campaigns' state, plus anyone still
    queued there unless include_queued is False, so no one is emailed at both addresses."""
    out = set()
    for st in states:
        for r in read_rows(st / "sent_log.csv"):
            if r.get("status") in ("sent", "replied"):
                out.add(person_key(r.get("name", ""), r.get("firm", "")))
        if include_queued:
            for r in read_rows(st / "queue.csv"):
                out.add(person_key(r.get("name", ""), r.get("firm", "")))
    return out


def take_over_queued(states: list[Path], people: set[tuple[str, str]], campaign: Path) -> int:
    """Remove not-yet-sent queue rows for these people from the other campaigns, logging each in
    that campaign's moved_to_other_campaign.csv. Returns how many rows moved."""
    moved = 0
    for st in states:
        q = read_rows(st / "queue.csv")
        if not q:
            continue
        keep, gone = [], []
        for r in q:
            (gone if person_key(r.get("name", ""), r.get("firm", "")) in people else keep).append(r)
        if gone:
            write_rows(st / "queue.csv", QUEUE_COLS, keep)
            for r in gone:
                append_row(st / "moved_to_other_campaign.csv", ["moved_at", "to", "name", "firm", "campaign"],
                           {"moved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "to": r.get("to", ""), "name": r.get("name", ""), "firm": r.get("firm", ""),
                            "campaign": str(campaign)})
            moved += len(gone)
    return moved


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
    a.template = a.template or audience_template(a.audience, "outreach_email.txt")
    a.subjects = a.subjects or audience_template(a.audience, "subjects.txt")
    suppressed = load_suppressed(state, a.suppress_file)
    recent = recently_emailed(state, a.recontact_days)
    queued = {r["to"].lower() for r in read_rows(state / "queue.csv")}

    body_tpl = Template(a.template.read_text(encoding="utf-8"))
    subjects = [l.strip() for l in a.subjects.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.lstrip().startswith("#")]
    signature = sender_signature(a)

    verified = load_verified(a.verified_file)
    if a.require_verified and not verified:
        print("Refusing: --require-verified needs --verified-file with an email-verifier export.", file=sys.stderr)
        return 2
    # Sent or queued in --other-state: skipped. Only queued (not sent) in --take-over-from: moved here.
    other_people = contacted_people(a.other_state) | contacted_people(a.take_over_from, include_queued=False)
    contacts = read_csv(a.contacts)
    firm_doms = firm_domains(a.firm_domains_from, contacts) if a.email_type == "personal" else set()
    allowed_doms = {d.strip().lower().lstrip("@") for d in a.allow_domain}
    firms = known_firms(a.firm_domains_from) if a.audience == "untitled" else set()

    new_rows, skipped, seen = [], [], set()
    for raw in contacts:
        c = canon_contact(raw)
        if a.email_type == "personal":
            email = personal_email(raw)
        else:
            # A cell may hold several addresses ("a@x.com; b@y.com"): use the first valid one.
            email = next((e for e in re.split(r"[\s,;|]+", c["email"].strip().lower())
                          if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e)), c["email"].strip().lower())
        c["first_name"], c["last_name"], c["city"] = map(tidy_name, (c["first_name"], c["last_name"], c["city"]))
        name = f"{c['first_name']} {c['last_name']}".strip()

        def skip(reason):
            skipped.append({"name": name, "email": email, "title": c["title"], "reason": reason})

        if a.audience == "untitled":
            if c["title"].strip():
                skip("has a title (use the partner/associate campaigns)"); continue
            if not looks_like_firm_lawyer(c["firm"], email, name, firms):
                skip("no title, and employer isn't a law firm"); continue
            if SOLO_PRACTICE_RE.search(c["firm"].strip(" .,")):
                skip("solo practice / bare 'Law Office' employer"); continue
        elif a.audience == "associate" and not a.title_regex:
            if not is_associate(c["title"]):
                skip("not an associate title"); continue
        elif not is_partner(c["title"], pattern):
            skip("not a partner title"); continue
        if a.audience != "untitled" and is_leadership(c["title"]):
            skip("firm leadership title (managing/founding partner, CEO, president...)"); continue
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            skip("no personal email on record" if a.email_type == "personal" else "missing/invalid email"); continue
        if a.email_type == "personal" and not is_freemail(email) and domain(email) in firm_doms:
            skip("personal-email field holds a firm address"); continue
        if a.email_type == "personal" and not is_personal_domain(email, allowed_doms):
            skip(f"not a consumer or alumni address, likely a firm mailbox ({domain(email)}); "
                 "approve with --allow-domain"); continue
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
        if other_people and person_key(name, c["firm"]) in other_people:
            skip("already contacted in another campaign"); continue
        if a.require_verified and not a.export_emails and verified.get(email) not in VALID_STATUSES:
            skip(f"email not verified valid ({verified.get(email) or 'not in verifier results'})"); continue
        seen.add(email)

        practice = plain_practice(areas)
        vals = {
            "first_name": c["first_name"] or "there",
            "firm": (short_firm(c["firm"]) if not GENERIC_FIRM_RE.match(short_firm(c["firm"]).strip(" .,")) else "") or "your firm",
            "title": c["title"],
            **practice_vars(practice, a.audience),
            "city_phrase": f" in {c['city']}" if c["city"] else "",
            "about_line": about_line(practice, short_firm(c["firm"]), email),
            "sender_name": a.sender_name, "sender_company": a.sender_company,
            "sender_signature": signature, "sender_address": a.sender_address,
            "unsubscribe_email": a.unsubscribe_email or a.sender_email,
        }
        body = with_footer(render_body(body_tpl, vals, seed=email), a.sender_address)
        import hashlib
        # Only subjects this person's record can fill: none that would print "practice" or "your firm".
        fits = [t for t in subjects if (practice or "$practice" not in t) and (vals["firm"] != "your firm" or "$firm" not in t)]
        fits = fits or [t for t in subjects if "$" not in t.replace("$first_name", "")] or subjects
        subj_tpl = Template(fits[hashlib.sha256(("subject:" + email).encode()).digest()[0] % len(fits)])
        subject = fix_subject(re.sub(r"\s+", " ", subj_tpl.safe_substitute(vals)).strip())
        new_rows.append({
            "contact_key": c["_raw"].get("id") or c["_raw"].get("contact_id") or email,
            "to": email, "name": name, "firm": c["firm"], "title": c["title"],
            "practice": practice, "subject": subject, "body": body,
            "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    write_rows(state / "prepare_skipped.csv", ["name", "email", "title", "reason"], skipped)
    if a.export_emails:
        # Export-only pass: hand these to the verifier; nothing is queued.
        write_rows(a.export_emails, ["email", "name", "firm"],
                   [{"email": r["to"], "name": r["name"], "firm": r["firm"]} for r in new_rows])
        print(f"Exported {len(new_rows)} address(es) to {a.export_emails} for verification; nothing queued. "
              f"Skipped {len(skipped)}.")
        return 0
    write_rows(state / "queue.csv", QUEUE_COLS, read_rows(state / "queue.csv") + new_rows)
    if a.take_over_from and new_rows:
        n = take_over_queued(a.take_over_from, {person_key(r["name"], r["firm"]) for r in new_rows}, state)
        print(f"Took over {n} not-yet-sent row(s) from --take-over-from queues.")
    reasons: dict[str, int] = {}
    for s in skipped:
        reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
    print(f"Queued {len(new_rows)} {a.audience}(s); skipped {len(skipped)}.")
    for r, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  skip {n:>5}  {r}")
    if new_rows:
        print("\nSample:\nSubject: " + new_rows[0]["subject"] + "\n\n" + new_rows[0]["body"])
    return 0


# ---------- verify (EmailListVerify) ----------

ELV_URL = "https://api.emaillistverify.com/api/verifyEmail"


def elv_check(email: str, key: str, timeout: float = 60) -> str:
    """One address -> EmailListVerify status ("ok", "ok_for_all", "email_disabled", ...).
    Only "ok" is a verified-valid mailbox; ok_for_all is a catch-all domain and is not."""
    url = ELV_URL + "?" + urllib.parse.urlencode({"email": email})
    req = urllib.request.Request(url, headers={"x-api-key": key, "User-Agent": "attorney-outreach"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace").strip().strip('"').lower()


def cmd_verify(a) -> int:
    """Verify every address in --input; results append to --output (email,status,checked_at).
    Re-runs skip addresses already in --output, so an interrupted run resumes without re-spending credits."""
    key = os.environ.get("EMAILLISTVERIFY_API_KEY") or "proxy-injected"
    done = {r["email"].lower() for r in read_rows(a.output) if r.get("email")}
    todo, seen = [], set()
    for r in read_rows(a.input):
        e = (r.get("email") or r.get("Email") or "").strip().lower()
        if e and e not in done and e not in seen:
            seen.add(e); todo.append(e)
    if a.limit:
        todo = todo[:a.limit]
    print(f"Verifying {len(todo)} address(es); {len(done)} already checked.")
    counts: dict[str, int] = {}
    errors = 0
    for i, e in enumerate(todo, 1):
        try:
            status = elv_check(e, key)
        except urllib.error.HTTPError as ex:
            if ex.code in (401, 403):
                print(f"Stopping: EmailListVerify refused the API key ({ex.code}).", file=sys.stderr)
                return 2
            status, errors = "", errors + 1
        except (urllib.error.URLError, TimeoutError) as ex:
            status, errors = "", errors + 1
            print(f"  error {e}: {ex}", file=sys.stderr)
        if status == "error_credit":
            print("Stopping: out of EmailListVerify credits.", file=sys.stderr)
            return 2
        if status:
            append_row(a.output, ["email", "status", "checked_at"],
                       {"email": e, "status": status,
                        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            counts[status] = counts.get(status, 0) + 1
        if errors >= a.max_errors:
            print(f"Stopping after {errors} request errors; re-run to resume.", file=sys.stderr)
            break
        if i % 100 == 0:
            print(f"  {i}/{len(todo)}")
        time.sleep(a.pause)
    for st, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {n:>5}  {st}{'  (valid)' if st in VALID_STATUSES else ''}")
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
        "content": [{"type": "text/plain", "value": row["body"]},
                    {"type": "text/html", "value": row.get("body_html") or to_html(row["body"])}],
        "headers": {"List-Unsubscribe": f"<mailto:{unsub}?subject=unsubscribe>"},
        "categories": [getattr(a, "category", "") or "attorney-outreach"],
        "tracking_settings": {"click_tracking": {"enable": False}, "open_tracking": {"enable": False},
                              "subscription_tracking": {"enable": False}},
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
    def sent_on_today(r):
        return r.get("status") == "sent" and datetime.fromisoformat(r["sent_at"]).astimezone(ET).date() == today
    init_today = sum(1 for r in log if sent_on_today(r) and (r.get("kind") or "initial") == "initial")
    fu_today = sum(1 for r in log if sent_on_today(r) and (r.get("kind") or "").startswith("followup"))
    sent_today = init_today + fu_today
    budget_init = max(0, a.daily_cap - init_today)
    budget_fu = max(0, a.followup_daily_cap - fu_today)
    if budget_init == 0 and budget_fu == 0 and a.live:
        print(f"Daily caps reached ({init_today}/{a.daily_cap} first emails, {fu_today}/{a.followup_daily_cap} follow-ups).")
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
    def firm_key(f):
        # Placeholders like "Law Office" are not one firm: they don't count toward the per-firm limit.
        f = (f or "").strip(" .,")
        return "" if GENERIC_FIRM_RE.match(f) else re.sub(r"[^a-z0-9]+", " ", f.lower()).strip()
    firms_today: dict[str, int] = {}
    domains_today: dict[str, int] = {}
    dropped: set[str] = set()
    firm_index = domain_firm_index(a.firm_reference) if a.firm_reference and a.firm_reference.exists() else None
    for r in log:
        if sent_on_today(r) and (r.get("kind") or "initial") == "initial":
            firms_today[firm_key(r.get("firm"))] = firms_today.get(firm_key(r.get("firm")), 0) + 1
            domains_today[domain(r.get("to", ""))] = domains_today.get(domain(r.get("to", "")), 0) + 1
    remaining, attempted, sent, failed = [], 0, 0, 0
    transport = {"sendgrid": send_sendgrid, "smtp": send_smtp, "outlook": None}[a.transport]
    batch = []
    limits = {"initial": min(budget_init if a.live else a.daily_cap, a.max_per_run),
              "followup": min(budget_fu if a.live else a.followup_daily_cap, a.max_followups_per_run)}
    used = {"initial": 0, "followup": 0}

    # Follow-ups go first: at most one per person per day, on the `--followup-days` schedule
    # (days after the first email), and only while there's no reply.
    followups = []
    originals = load_originals(state)
    fu_days = parse_followup_days(a.followup_days)
    if fu_days:
        templates = {n: Template(audience_template(a.audience, f"followup_{n}_email.txt").read_text(encoding="utf-8"))
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
                **practice_vars(practice, a.audience),
                "practice_work": f"{practice} work" if practice else "your practice",
                "firm": r.get("firm") or "your firm",
                "sender_signature": sender_signature(a), "sender_address": a.sender_address,
                "unsubscribe_email": a.unsubscribe_email or a.sender_email}, seed=f"{to}:followup{step}")
            body = with_footer(body, a.sender_address)
            check_followup_opening(body)
            subj = r.get("subject") or ""
            followups.append({"to": to, "name": r.get("name", ""), "firm": r.get("firm", ""),
                              "practice": practice, "kind": f"followup{step}", "original_subject": subj,
                              "original_sent_at": r["sent_at"],
                              "subject": subj if subj.lower().startswith("re:") else f"RE: {subj}", "body": body,
                              "body_html": quoted_reply_html(body, originals.get(to), a.sender_name, a.sender_email)})
    if followups and not a.sender_address:
        print("Refusing: follow-ups need --sender-address / OUTREACH_SENDER_ADDRESS.", file=sys.stderr)
        return 2

    for row in followups + queue:
        to = row["to"].lower()
        kind = row.get("kind", "initial")
        if kind == "initial" and (to in suppressed or domain_blocked(to, BLOCKED_RECIPIENT_DOMAINS) or to in recent
                                  or is_leadership(row.get("title", ""))):
            continue  # dropped from the queue for good
        if kind == "initial" and not firm_is_current(row.get("firm", ""), to, firm_index):
            append_row(state / "sent_log.csv", LOG_COLS, {
                "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "to": to, "name": row.get("name", ""),
                "firm": row.get("firm", ""), "subject": row.get("subject", ""), "transport": a.transport,
                "status": "skipped", "detail": "firm/email domain mismatch (CRM firm likely out of date)",
                "kind": "initial", "practice": row.get("practice", "")}) if a.live else None
            if a.live:
                dropped.add(to)
            else:
                remaining.append(row)
            continue
        fk = firm_key(row.get("firm"))
        dk = domain(to)
        if kind == "initial" and dk and dk not in FREE_MAIL and domains_today.get(dk, 0) >= a.per_firm_daily:
            remaining.append(row); continue
        bucket = "initial" if kind == "initial" else "followup"
        # One partner per firm per day applies to first emails; follow-ups are already spread out.
        firm_full = (kind == "initial" and fk and a.per_firm_daily > 0
                     and firms_today.get(fk, 0) >= a.per_firm_daily)
        if used[bucket] >= limits[bucket] or failed >= a.max_failures or firm_full:
            if kind == "initial":
                remaining.append(row)
            continue
        used[bucket] += 1
        attempted += 1
        if fk and kind == "initial":
            firms_today[fk] = firms_today.get(fk, 0) + 1
        if kind == "initial" and dk:
            domains_today[dk] = domains_today.get(dk, 0) + 1
        row["subject"] = fix_subject(row["subject"])
        if not a.live:
            print(f"[dry-run] would send {kind} to {row['name']} <{to}>: {row['subject']}")
            if kind == "initial":
                remaining.append(row)
            continue
        if a.transport == "outlook":
            # Sent by the Outlook connector; results come back via `record`.
            # Follow-ups are sent as a reply in the original thread.
            batch.append({"kind": kind, "pause_seconds_before": random.randint(a.pause_min, a.pause_max), **{k: row.get(k, "") for k in
                          ("to", "name", "firm", "practice", "subject", "body", "original_subject", "original_sent_at")},
                          "body_html": row.get("body_html") or to_html(row.get("body", ""))})
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

    if a.transport == "outlook" and a.live and dropped:
        write_rows(state / "queue.csv", QUEUE_COLS, [r for r in queue if r["to"].lower() not in dropped])
        print(f"Skipped {len(dropped)} partner(s) whose CRM firm doesn't match their email domain.")
    if a.transport == "outlook" and a.live:
        (state / "outbox.json").write_text(json.dumps(batch, indent=2))
        n_fu = sum(1 for b in batch if b["kind"] != "initial")
        print(f"OUTLOOK BATCH: {len(batch)} email(s) ({n_fu} follow-up, {len(batch) - n_fu} first) written to {state / 'outbox.json'} "
              f"(sent today so far: {init_today}/{a.daily_cap} first emails, {fu_today}/{a.followup_daily_cap} follow-ups). "
              f"Send each with outlook_send_mail, "
              f"then log results with `record`.")
        return 0
    if a.live:
        write_rows(state / "queue.csv", QUEUE_COLS, remaining)
    mode = "LIVE" if a.live else "DRY RUN"
    print(f"{mode}: {len(followups)} follow-up(s) due. {sent if a.live else attempted} {'sent' if a.live else 'would send'}, {failed} failed, "
          f"{len(remaining)} still queued. Sent today: {sent_today + sent} "
          f"(caps: {a.daily_cap} first emails + {a.followup_daily_cap} follow-ups).")
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
        if a.kind == "initial" and a.status == "sent" and row.get("body"):
            with (state / "originals.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"to": to, "name": row.get("name", ""), "subject": row.get("subject", ""),
                                    "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "body": row["body"]}) + "\n")
        print(f"{a.status:<7} {to}")
    write_rows(state / "queue.csv", QUEUE_COLS, [r for r in queue if r["to"].lower() not in done])
    return 0


def cmd_send_outbox(a) -> int:
    """Send this run's outbox.json through SendGrid in one go (no per-email approvals), logging each send.
    Entries already logged since the outbox was written (e.g. skipped for prior contact) are left out."""
    state: Path = a.state
    outbox = state / "outbox.json"
    if not outbox.exists():
        print("No outbox.json: run `batch` first."); return 1
    built = datetime.fromtimestamp(outbox.stat().st_mtime, timezone.utc)
    entries = json.loads(outbox.read_text())
    handled = set()
    for r in read_rows(state / "sent_log.csv"):
        try:
            when = datetime.fromisoformat(r["sent_at"])
        except (KeyError, ValueError):
            continue
        if when >= built - timedelta(seconds=5):
            handled.add((r["to"].lower(), r.get("kind") or "initial"))
            if r.get("status") == "replied":
                handled.add((r["to"].lower(), "*"))
    todo = [e for e in entries if (e["to"].lower(), e["kind"]) not in handled and (e["to"].lower(), "*") not in handled]
    if not todo:
        print("Nothing left to send in this outbox."); return 0
    if domain_blocked(a.sender_email, BLOCKED_SENDER_DOMAINS):
        print(f"Refusing: {domain(a.sender_email)} is on the do-not-send-from list.", file=sys.stderr); return 2
    queue = read_rows(state / "queue.csv")
    sent_initial, sent_n, failed = set(), 0, 0
    for i, e in enumerate(todo):
        if i:
            time.sleep(random.uniform(a.min_delay, a.max_delay))
        ok, detail = send_sendgrid(a, e)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        append_row(state / "sent_log.csv", LOG_COLS, {
            "sent_at": now, "to": e["to"].lower(), "name": e.get("name", ""), "firm": e.get("firm", ""),
            "subject": e.get("subject", ""), "transport": "sendgrid", "status": "sent" if ok else "failed",
            "detail": detail, "kind": e["kind"], "practice": e.get("practice", "")})
        if ok:
            sent_n += 1
            print(f"sent    {e['kind']:<9} {e['to']}")
            if e["kind"] == "initial":
                sent_initial.add(e["to"].lower())
                with (state / "originals.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"to": e["to"].lower(), "name": e.get("name", ""), "subject": e.get("subject", ""),
                                        "sent_at": now, "body": e.get("body", "")}) + "\n")
        else:
            failed += 1
            print(f"FAILED  {e['kind']:<9} {e['to']}: {detail}", file=sys.stderr)
            if re.match(r"HTTP (401|403|429)", detail) or failed >= a.max_failures:
                print("Stopping: SendGrid refused or rate-limited the send.", file=sys.stderr)
                break
    if sent_initial:
        write_rows(state / "queue.csv", QUEUE_COLS, [r for r in queue if r["to"].lower() not in sent_initial])
    print(f"SENDGRID: {sent_n} sent, {failed} failed, {len(todo) - sent_n - failed} not attempted.")
    return 1 if failed else 0


def cmd_summary(a) -> int:
    """Today's activity (ET) as plain text, for the end-of-day email to Jon."""
    state: Path = a.state
    today = datetime.now(ET).date()
    log = [r for r in read_rows(state / "sent_log.csv")
           if datetime.fromisoformat(r["sent_at"]).astimezone(ET).date() == today]
    sup = [r for r in read_rows(state / "suppression.csv")
           if r.get("added_at") and datetime.fromisoformat(r["added_at"]).astimezone(ET).date() == today]
    def n(kind=None, status="sent"):
        return sum(1 for r in log if r.get("status") == status and
                   (kind is None or (r.get("kind") or "initial") == kind))
    replies = [r for r in log if r.get("status") == "replied"]
    interested = [r for r in replies if "interest" in (r.get("detail") or "").lower()
                  and "not interest" not in (r.get("detail") or "").lower()]
    queue_left = len(read_rows(state / "queue.csv"))
    lines = [
        f"Partner outreach, {today:%A %B %-d, %Y}",
        "",
        f"First emails sent: {n('initial')}",
        f"Follow-ups sent: {n('followup1')} (day 5), {n('followup2')} (day 30)",
        f"Replies: {len(replies)}  |  Opt-outs: {len(sup)}  |  Failed: {n(status='failed')}",
        f"Skipped: {sum(1 for r in log if r.get('status') == 'skipped' and 'prior' in (r.get('detail') or ''))} prior contact, "
        f"{sum(1 for r in log if r.get('status') == 'skipped' and 'mismatch' in (r.get('detail') or ''))} firm out of date in CRM",
        f"Partners still queued: {queue_left:,}",
    ]
    if interested:
        lines += ["", "Interested, follow up with:"]
        lines += [f"- {r['name']} ({r['firm']}) <{r['to']}>" for r in interested]
    other = [r for r in replies if r not in interested]
    if other:
        lines += ["", "Other replies:"]
        lines += [f"- {r['name'] or r['to']} ({r['firm']}): {r.get('detail') or 'replied'}" for r in other]
    fails = [r for r in log if r.get("status") == "failed"]
    if fails:
        lines += ["", "Failed sends:"] + [f"- {r['to']}: {r.get('detail', '')[:120]}" for r in fails]
    print("\n".join(lines))
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
    p.add_argument("--audience", choices=["partner", "associate", "untitled"], default="partner",
                   help="who this campaign is for: picks the title filter and templates/<audience>_*.txt")
    p.add_argument("--template", type=Path, default=None,
                   help="default templates/<audience>_outreach_email.txt")
    p.add_argument("--subjects", type=Path, default=None,
                   help="one subject per line; each person gets one, picked the same way every time")
    p.add_argument("--title-regex", default="", help="override which titles count as partner")
    p.add_argument("--require-practice", action="store_true", help="skip partners with no practice area on record")
    p.add_argument("--exclude-practice", default=os.environ.get(
        "OUTREACH_EXCLUDE_PRACTICE", "family law,personal injury,immigration"),
        help="comma-separated practices; partners whose main (first-listed) practice is one of these are skipped")
    p.add_argument("--email-type", choices=["work", "personal"], default="work",
                   help="personal = use the personal-email column, or a consumer-domain (gmail, yahoo...) "
                        "address; partners with only a firm address are skipped")
    p.add_argument("--verified-file", type=Path, action="append", default=[],
                   help="email-verifier export (ZeroBounce, NeverBounce...) with email + status columns; repeatable")
    p.add_argument("--require-verified", action="store_true",
                   help="queue only addresses the verifier marked valid/deliverable (catch-all, unknown, "
                        "risky and missing are skipped)")
    p.add_argument("--other-state", type=Path, action="append", default=[],
                   help="another campaign's state folder; anyone sent or queued there (same name + firm) is skipped")
    p.add_argument("--take-over-from", type=Path, action="append", default=[],
                   help="another campaign's state: people only queued there (not yet sent) are queued here "
                        "instead and removed from that queue; anyone already sent there is skipped")
    p.add_argument("--allow-domain", action="append", default=[],
                   help="with --email-type personal: a custom domain confirmed as personal (repeatable)")
    p.add_argument("--firm-domains-from", type=Path, action="append", default=[],
                   help="extra CSVs whose work emails mark firm domains (e.g. the partner export); with "
                        "--email-type personal, an address on a firm domain is skipped")
    p.add_argument("--export-emails", type=Path,
                   help="also write the candidate addresses (after all other filters) to this CSV, "
                        "to upload to the verifier")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("verify", help="check addresses with EmailListVerify before queuing them")
    p.add_argument("--input", type=Path, required=True, help="CSV with an email column (from prepare --export-emails)")
    p.add_argument("--output", type=Path, required=True, help="results CSV; pass it to prepare --verified-file")
    p.add_argument("--limit", type=int, default=0, help="check at most N addresses this run")
    p.add_argument("--pause", type=float, default=0.2, help="seconds between requests")
    p.add_argument("--max-errors", type=int, default=10)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("send", help="send queued emails up to the daily cap")
    common(p)
    p.add_argument("--transport", choices=["outlook", "sendgrid", "smtp"], default="outlook")
    p.add_argument("--audience", choices=["partner", "associate", "untitled"], default="partner",
                   help="must match the prepare run: picks templates/<audience>_followup_<n>_email.txt")
    p.add_argument("--reply-to", default="")
    p.add_argument("--daily-cap", type=int, default=int(os.environ.get("OUTREACH_DAILY_CAP", "40")))
    p.add_argument("--max-per-run", type=int, default=int(os.environ.get("OUTREACH_MAX_PER_RUN", "10")),
                   help="first emails per run; schedule several runs a day so sends are spread out")
    p.add_argument("--firm-reference", type=Path, default=None,
                   help="CRM partner export used to tell which firm each email domain belongs to")
    p.add_argument("--followup-daily-cap", type=int, default=int(os.environ.get("OUTREACH_FOLLOWUP_DAILY_CAP", "200")),
                   help="follow-ups per day, on top of --daily-cap first emails")
    p.add_argument("--max-followups-per-run", type=int,
                   default=int(os.environ.get("OUTREACH_MAX_FOLLOWUPS_PER_RUN", "20")))
    p.add_argument("--followup-days", default=os.environ.get("OUTREACH_FOLLOWUP_DAYS", "5,30"),
                   help="follow-up schedule in days after the first email, e.g. 5,30 (0 = none); "
                        "templates/partner_followup_<n>_email.txt is used for step n")
    p.add_argument("--per-firm-daily", type=int, default=1,
                   help="max first emails to people at the same firm per day; 0 = no limit")
    p.add_argument("--delay", type=float, default=20, help="base seconds between sends (plus random jitter)")
    p.add_argument("--pause-min", type=int, default=60, help="Outlook batch: shortest pause before each email, seconds")
    p.add_argument("--pause-max", type=int, default=300, help="Outlook batch: longest pause before each email, seconds")
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

    p = sub.add_parser("send-outbox", help="send this run's outbox.json through SendGrid and log each send")
    common(p)
    p.add_argument("--min-delay", type=float, default=4)
    p.add_argument("--max-delay", type=float, default=10)
    p.add_argument("--max-failures", type=int, default=3)
    p.add_argument("--reply-to", default="")
    p.add_argument("--category", default="partner-outreach")
    p.set_defaults(func=cmd_send_outbox)

    p = sub.add_parser("summary", help="today's activity, for the end-of-day email")
    p.add_argument("--state", type=Path, required=True)
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("suppress", help="add emails to the do-not-contact list (e.g. unsubscribe replies)")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--reason", default="unsubscribe")
    p.add_argument("emails", nargs="+")
    p.set_defaults(func=cmd_suppress)

    a = ap.parse_args()
    global FOOTER_TEXT
    FOOTER_TEXT = getattr(a, "sender_address", "") or os.environ.get("OUTREACH_SENDER_ADDRESS", "")
    if a.cmd in ("prepare", "send") and (not a.sender_name or not a.sender_email):
        ap.error("--sender-name and --sender-email are required (or set OUTREACH_SENDER_NAME / OUTREACH_SENDER_EMAIL)")
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
