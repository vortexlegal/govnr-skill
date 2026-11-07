---
name: attorney-outreach
description: Email law-firm partners from the CRM (VortexRecruitCRM export or any CRM CSV) to ask whether they'd be open to hearing about partner roles similar to what they do now. Not tied to a specific job opening. Filters to partner titles, applies DNU/unsubscribe/blocked-domain rules, writes a personalized note per partner, and auto-sends up to a daily cap during business hours. Also includes a job-matching mode for a specific list of openings.
---

# Attorney Outreach

## Partner outreach (main workflow)

Emails partners about **roles similar to their current practice**. It is not
about any one opening, so the email never names or describes a job. It names
their practice and firm and asks whether they're open to hearing about
partner roles on a stronger platform.

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
- **Follow-ups:** two, counted from the first email: day 5 and day 30
  (`--followup-days 5,30`, 0 turns them off). Each one goes only if there's no reply, and
  is sent as a reply in the same thread (`templates/partner_followup_1_email.txt`,
  `templates/partner_followup_2_email.txt`). Follow-up 2 always comes at least 25 days
  after follow-up 1. Follow-ups go out before new first emails and count toward the daily
  cap and the per-firm limit. Nothing is sent after follow-up 2.
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

### Making it read like Jon, not a template
- **Greeting and voice:** "Hi <first name>," (never "Dear"). "I am a legal recruiter with
  VortexLegal." Sign off "Best regards," with Jon's real signature, which includes the
  mailing address.
- **Firm and practice:** the firm is named by its first two words with no LLP/LLC/PA/PC,
  and there's no city. One practice area, in plain words: `plain_practice()` turns
  "LABOR & EMPLOYMENT" into "employment".
- **Rotated wording:** every sentence has 2–3 versions (`[[a||b||c]]` in the templates).
  Each person gets a fixed mix chosen from their email address, so a rebuilt queue keeps
  the same wording.
- **Subjects:** rotated from `templates/partner_subjects.txt`.
- **Length:** about 80 words, with "confidential" used at most once.
- **Spread-out sending:** 4 runs per weekday at uneven times (e.g. 9:41, 11:23, 13:52 and
  15:37 ET), up to `--max-per-run 10` each, with the 40/day cap across all runs. Each
  outbox entry has `pause_seconds_before` (60–300 s): wait that long before sending it.
- **First names:** initials-only names ("K C") use the first name from a first.last email
  address. If there isn't one, that partner is skipped.

### Standing exclusions
- `VLCRM/crm/exclude_*.csv` are permanent do-not-email lists, applied on every run (e.g.
  `exclude_ny_managing_partners.csv`: Jon asked that the NY managing partners list never be emailed).
  Pass them as `--suppress-file` to `prepare` too when rebuilding the queue.

### Rules
- **No reminder language in follow-ups, anywhere in the body:** no "following up",
  "bringing this to the top of your inbox", "circling back", "checking in", "bumping",
  "nudge", "in case you missed it", "per my last email", "resending" or "my earlier note".
  Each follow-up stands on its own as a fresh question. `send` refuses any follow-up that
  contains one of these phrases.
- **Truthful:** never claim a specific opening, a referral, or a prior relationship.
  The template only asks whether they're open to similar roles.
- **Opt-outs:** the emails don't carry an opt-out sentence (Jon's choice). Anyone who replies
  asking to stop is suppressed on the next run and never emailed again. The mailing address stays
  in the signature.
- **Outside the US:** flag non-US partners. CASL and GDPR/PECR need consent or a
  lawful basis before cold email.
- **Volume:** start with a low cap (20–40/day) on a warmed-up sending domain.

## Job-matching mode (specific openings)
`scripts/match_and_draft.py` matches contacts to a positions CSV and drafts an
email per match for review. See its `--help` and `examples/positions.csv`.

## Tests
`python3 -m unittest discover -s tests`

## Daily Outlook run (what the scheduled task does each weekday)

Sender settings live in `outreach.env` (Jonathan Broder, jbroder@vortexlegal.com,
330 S. Pineapple Ave., Sarasota, FL 34236, up to 250 first emails/day plus up to 200 follow-ups/day, 1 new partner per firm per day).
State folder: `partner-state/` (keep it between runs).

1. **Process replies first.** Search the Outlook Inbox for replies since the last run
   (`outlook_email_search`, query "unsubscribe" or "remove", and replies to subjects
   starting "Your "). For anyone asking to stop: run `suppress <email>`. For interested
   replies: leave them for Jon and list them in the run summary.
2. **Build the batch:** `set -a; . ./outreach.env; set +a` then
   `python3 scripts/partner_outreach.py send --state partner-state --suppress-file <dnu parts> --live`
   (default transport is Outlook). This writes `partner-state/outbox.json`.
3. **Follow-ups (`"kind": "followup1"` or `"followup2"` in outbox.json; listed first).**
   Follow-up 1 is due 5 days after the first email, follow-up 2 at 30 days, each only if
   there's no recorded reply.
   - Check for a reply first: `outlook_email_search` with `sender=<to>` and
     `afterDateTime=<original_sent_at>`. If they wrote back, run
     `record --status replied <to>` and don't send.
   - Otherwise find the original in Sent Items (`recipient=<to>`, match `original_subject`),
     then `outlook_create_reply_draft` on it with the follow-up `body` and
     `outlook_send_draft`. This keeps it in the same thread. If the original can't be
     found, send with `outlook_send_mail` using the `RE:` subject.
   - Run `record --kind <kind> <to>`, e.g. `--kind followup1` (or `--status failed`).
4. **First emails (`"kind": "initial"`):**
   - Check Outlook for prior contact: `outlook_email_search` with `recipient=<to>`.
     If Jon has emailed this person in the last 180 days, run
     `record --status skipped --detail "prior Outlook contact" <to>` and move on.
   - Otherwise send with `outlook_send_mail` (to, subject, body, bodyType text), then
     run `record <to>`. On an error, run `record --status failed --detail "<error>" <to>`
     and stop after 3 failures.
   - Wait each entry's `pause_seconds_before` before sending it.
5. **Summarize:** follow-ups and first emails sent / skipped / failed, unsubscribes processed, and
   interested replies.
