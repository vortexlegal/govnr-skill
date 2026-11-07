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
LOG_COLS = ["sent_at", "to", "name", "firm", "subject", "transport", "status", "detail"]


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


def display_practice(areas: list[str]) -> str:
    def fmt(a: str) -> str:
        return " ".join(w if w.upper() in ACRONYMS else w.lower() for w in a.split())
    areas = [fmt(a) for a in areas if a][:2]
    return " and ".join(areas)


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
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def load_suppressed(state: Path, extra: list[Path]) -> set[str]:
    emails: set[str] = set()
    for p in [state / "suppression.csv", *extra]:
        for r in read_rows(p):
            e = (r.get("email") or r.get("Email") or next(iter(r.values()), "") or "").strip().lower()
            if e:
                emails.add(e)
    return emails


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
    subj_tpl = Template(a.subject)
    contact = " | ".join(x for x in [a.sender_phone, a.sender_email] if x)
    signature = "\n".join(x for x in [
        a.sender_name, ", ".join(x for x in [a.sender_title, a.sender_company] if x), contact, a.sender_website] if x)

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
        areas = parse_areas(c["practice_areas"])
        if not areas and a.require_practice:
            skip("no practice area on record"); continue
        seen.add(email)

        practice = display_practice(areas) or "your"
        vals = {
            "first_name": c["first_name"] or "there",
            "firm": c["firm"] or "your firm",
            "title": c["title"],
            "practice": practice,
            "practice_phrase": f"{practice} practice" if areas else "practice",
            "city_phrase": f" in {c['city']}" if c["city"] else "",
            "sender_name": a.sender_name, "sender_company": a.sender_company,
            "sender_signature": signature, "sender_address": a.sender_address,
            "unsubscribe_email": a.unsubscribe_email or a.sender_email,
        }
        body = body_tpl.safe_substitute(vals)
        body = re.sub(r"\n{3,}", "\n\n", "\n".join(l.rstrip() for l in body.splitlines())).strip() + "\n"
        subject = fix_subject(re.sub(r"\s+", " ", subj_tpl.safe_substitute(vals)).strip())
        new_rows.append({
            "contact_key": c["_raw"].get("id") or c["_raw"].get("contact_id") or email,
            "to": email, "name": name, "firm": c["firm"], "title": c["title"],
            "practice": practice if areas else "", "subject": subject, "body": body,
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
    limit = budget if a.live else min(a.daily_cap, len(queue))

    for row in queue:
        to = row["to"].lower()
        if to in suppressed or domain_blocked(to, BLOCKED_RECIPIENT_DOMAINS) or to in recent:
            continue  # dropped from the queue for good
        fk = firm_key(row.get("firm"))
        if attempted >= limit or failed >= a.max_failures or (fk and firms_today.get(fk, 0) >= a.per_firm_daily):
            remaining.append(row); continue
        attempted += 1
        if fk:
            firms_today[fk] = firms_today.get(fk, 0) + 1
        row["subject"] = fix_subject(row["subject"])
        if not a.live:
            print(f"[dry-run] would send to {row['name']} <{to}>: {row['subject']}")
            remaining.append(row); continue
        if a.transport == "outlook":
            # Sent by the Outlook connector (outlook_send_mail); results come back via `record`.
            batch.append({k: row[k] for k in ("to", "name", "firm", "subject", "body")})
            remaining.append(row); continue
        ok, detail = transport(a, row)
        append_row(state / "sent_log.csv", LOG_COLS, {
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "to": to, "name": row["name"],
            "firm": row["firm"], "subject": row["subject"], "transport": a.transport,
            "status": "sent" if ok else "failed", "detail": detail})
        if ok:
            sent += 1; recent.add(to)
            print(f"sent   {row['name']} <{to}>")
        else:
            # Delivery outcome unknown or failed: keep it queued, never auto-retry in this run.
            failed += 1; remaining.append(row)
            print(f"FAILED {row['name']} <{to}>: {detail}", file=sys.stderr)
        time.sleep(a.delay + random.uniform(0, a.delay))

    if a.transport == "outlook" and a.live:
        (state / "outbox.json").write_text(json.dumps(batch, indent=2))
        print(f"OUTLOOK BATCH: {len(batch)} email(s) written to {state / 'outbox.json'} "
              f"(sent today so far: {sent_today}/{a.daily_cap}). Send each with outlook_send_mail, "
              f"then log results with `record`.")
        return 0
    if a.live:
        write_rows(state / "queue.csv", QUEUE_COLS, remaining)
    mode = "LIVE" if a.live else "DRY RUN"
    print(f"{mode}: {sent if a.live else attempted} {'sent' if a.live else 'would send'}, {failed} failed, "
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
    done = set()
    for to in a.emails:
        to = to.strip().lower()
        row = by_to.get(to, {"to": to, "name": "", "firm": "", "subject": ""})
        append_row(state / "sent_log.csv", LOG_COLS, {
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "to": to,
            "name": row.get("name", ""), "firm": row.get("firm", ""), "subject": row.get("subject", ""),
            "transport": "outlook", "status": a.status, "detail": a.detail})
        if a.status in ("sent", "skipped"):
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

    p = sub.add_parser("prepare", help="build personalized emails for partners in a CRM export")
    common(p)
    p.add_argument("--contacts", type=Path, required=True)
    p.add_argument("--sender-title", default=os.environ.get("OUTREACH_SENDER_TITLE", ""))
    p.add_argument("--sender-company", default=os.environ.get("OUTREACH_SENDER_COMPANY", ""))
    p.add_argument("--sender-phone", default=os.environ.get("OUTREACH_SENDER_PHONE", ""))
    p.add_argument("--sender-website", default=os.environ.get("OUTREACH_SENDER_WEBSITE", ""))
    p.add_argument("--sender-address", default=os.environ.get("OUTREACH_SENDER_ADDRESS", ""),
                   help="physical mailing address (CAN-SPAM)")
    p.add_argument("--template", type=Path, default=HERE / "templates" / "partner_outreach_email.txt")
    p.add_argument("--subject", default="Your $practice_phrase at $firm")
    p.add_argument("--title-regex", default="", help="override which titles count as partner")
    p.add_argument("--require-practice", action="store_true", help="skip partners with no practice area on record")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("send", help="send queued emails up to the daily cap")
    common(p)
    p.add_argument("--transport", choices=["outlook", "sendgrid", "smtp"], default="outlook")
    p.add_argument("--reply-to", default="")
    p.add_argument("--daily-cap", type=int, default=40)
    p.add_argument("--per-firm-daily", type=int, default=1, help="max partners at the same firm per day")
    p.add_argument("--delay", type=float, default=20, help="base seconds between sends (plus random jitter)")
    p.add_argument("--start-hour", type=int, default=9)
    p.add_argument("--end-hour", type=int, default=17)
    p.add_argument("--ignore-hours", action="store_true")
    p.add_argument("--max-failures", type=int, default=3)
    p.add_argument("--live", action="store_true", help="actually send (default is a dry run)")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("record", help="log sends done via the Outlook connector")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--status", choices=["sent", "failed", "skipped"], default="sent",
                   help="skipped = not sent and removed from queue (e.g. already emailed per Outlook Sent Items)")
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
