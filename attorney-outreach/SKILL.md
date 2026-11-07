---
name: attorney-outreach
description: Email law-firm partners from the CRM (VortexRecruitCRM export or any CRM CSV) to ask whether they'd be open to hearing about partner roles similar to what they do now. Not tied to a specific job opening. Filters to partner titles, applies DNU/unsubscribe/blocked-domain rules, writes a personalized note per partner, and auto-sends up to a daily cap during business hours. Also includes a job-matching mode for a specific list of openings.
---

# Attorney Outreach

## Partner outreach (main workflow)

Emails partners about **roles similar to their current practice**. It is not
about any one opening, so the email never names or describes a job. It names
their practice and firm and asks whether they're open to hearing about
comparable partner roles.

### 1. Get the contacts
Export candidates from VortexRecruitCRM (or any CRM) as CSV. The script
recognizes these columns: `firstName`/`First Name`, `lastName`, `email`,
`currentCompany`, `currentTitle`, `practiceAreas`, `city`, `isDnu`/`DNU`/`Email Opt Out`.
Also export the CRM's DNU / unsubscribe list and pass it with `--suppress-file`.

### 2. Prepare the queue
```bash
python3 scripts/partner_outreach.py prepare --state ./partner-state \
  --contacts crm_export.csv --suppress-file crm_dnu.csv \
  --sender-name "Jon Broder" --sender-title "Legal Recruiter" \
  --sender-company "Vortex Legal" --sender-email jbroder@vortexlegal.com \
  --sender-address "<physical mailing address>"
```
- **Partner titles:** Partner, Equity/Managing Partner, Shareholder, Principal and
  Managing Member are included. Associate Partner, "Partnerships Manager" and staff
  titles are excluded. Override with `--title-regex`.
- **Skipped:** CRM DNU, the suppression list, blocked recipient domains
  (`wbd-us.com`), anyone emailed in the last 180 days (`--recontact-days`),
  duplicates and invalid emails. Reasons go to `prepare_skipped.csv`.
- **Safe to re-run:** re-running on a fresh export only queues new partners.
- **Output:** a sample email is printed. Show it to the user before the first live send.

### 3. Send (scheduled)
```bash
# Default transport is Outlook (the Microsoft 365 connector, as jbroder@vortexlegal.com).
# `send --live` writes outbox.json; send each with outlook_send_mail, then
# `record <emails>` (or `record --status failed ...`). SendGrid/SMTP also supported.
python3 scripts/partner_outreach.py send --state ./partner-state \
  --sender-name "Jon Broder" --sender-email jbroder@vortexlegal.com \
  --daily-cap 40 --per-firm-daily 1 --live
```
- **Dry run by default.** Without `--live` it only lists what it would send and changes nothing.
- **Limits:** sends at most `--daily-cap` per Eastern-time day, on weekdays between
  9:00 and 17:00 ET (`--start-hour`/`--end-hour`), with a randomized delay between
  sends (`--delay`).
- **Stops early:** refuses to run without credentials, or from a do-not-send-from
  domain (prividential.ai, chrismooreesq.com, daniellenimoxjohnsonlegal.com).
  Stops after `--max-failures` failed sends.
- **Failed sends:** they stay queued and are logged. They are never retried
  automatically in the same run.
- **Each email:** plain text, with a `List-Unsubscribe` header, an unsubscribe line,
  and the mailing address. Tracking pixels and link rewriting are off.
- **Schedule:** run `send` once each weekday morning, e.g. cron `5 10 * * 1-5`
  in ET. Re-run `prepare` whenever there's a fresh CRM export.

### 4. Handle replies
- **Unsubscribe replies:** `python3 scripts/partner_outreach.py suppress --state ./partner-state a@firm.com`.
  Also mark them DNU in the CRM.
- **Interested replies:** handle these in the CRM. `sent_log.csv` records who was
  emailed and when, so it can be imported back into the CRM.

### Files in `--state`
`outbox.json` (Outlook batch), `queue.csv` (pending), `sent_log.csv` (every attempt), `suppression.csv`,
`prepare_skipped.csv`. Keep this folder between runs. It is what prevents
repeat emails.

### Rules
- **Truthful:** never claim a specific opening, a referral, or a prior relationship.
  The template only asks whether they're open to similar roles.
- **Opt-outs:** honor them within 10 business days (CAN-SPAM). Keep the unsubscribe
  line and the physical address.
- **Outside the US:** flag non-US partners. CASL and GDPR/PECR need consent or a
  lawful basis before cold email.
- **Volume:** start with a low cap (20–40/day) on a warmed-up sending domain.

## Job-matching mode (specific openings)
`scripts/match_and_draft.py` matches contacts to a positions CSV and drafts an
email per match for review. See its `--help` and `examples/positions.csv`.

## Tests
`python3 -m unittest discover -s tests`
