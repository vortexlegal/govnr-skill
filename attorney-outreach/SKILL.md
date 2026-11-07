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
  is sent directly as an RE: reply with the first email quoted underneath (`templates/partner_followup_1_email.txt`,
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
- **Never "like yours":** no template says "roles like yours" (or any "... like yours"); a test enforces it.
- **Truthful:** never claim a specific opening, a referral, or a prior relationship.
  The template only asks whether they're open to similar roles.
- **Opt-outs:** the emails don't carry an opt-out sentence (Jon's choice). Anyone who replies
  asking to stop is suppressed on the next run and never emailed again. The mailing address stays
  in the signature.
- **Outside the US:** flag non-US partners. CASL and GDPR/PECR need consent or a
  lawful basis before cold email.
- **Volume:** start with a low cap (20–40/day) on a warmed-up sending domain.

## Personal-email campaign (verified addresses only)

**No title on record.** `--audience untitled` takes records with an empty title, keeps only those whose
employer looks like a law firm (LLP/PC/PLLC/Law/Legal/"& Associates", or a firm name from the partner
export passed as `--firm-domains-from`) or, with no employer, whose email/name says esq/law/atty.
Government offices, schools, staffing and in-house employers are skipped. `templates/untitled_*.txt`
never says partner, associate or "roles like yours", and the opening line uses only the details on
record (no "your firm"; placeholder employers such as "Law Offices" are left out).

**Personal means personal.** Consumer mailboxes (any country: live.fr, yahoo.co.in) and school/alumni
addresses count. A custom domain in the personal-email field is usually a current or former firm
mailbox and is skipped unless approved with `--allow-domain`. The CRM's `t`/`f` DNU flag is honored.

**Queued elsewhere but not sent:** `--take-over-from <state>` moves those people into this campaign and out of
the other queue (logged in `moved_to_other_campaign.csv`), so they get the personal drip only.

**Partners and associates.** Run one campaign per audience, each with its own state folder:
`--audience partner` (default) or `--audience associate`, passed to both `prepare` and `send`.
Associates use `templates/associate_*.txt` (partnership track, hours, pay; no "portable book"
wording). Associate titles: Associate, Senior Associate, Associate Attorney, "<practice> Associate".
Not included: Associate General Counsel and other in-house titles, Summer Associates, Associate
Partner, non-attorney "associate" titles. Point each campaign's `--other-state` at the other
campaigns' folders (and the work-email `state/`) so no one is emailed twice. State folders in VLCRM:
`state-personal-partner/`, `state-personal-associate/`, `state-personal-untitled/`; CRM exports: `crm/personal/`; verifier results: `crm/verified/`.

Same drip as above (first email, then follow-ups at day 5 and 30 if no reply), sent to partners'
**personal** addresses instead of their firm address. Keep it in its own state folder.

1. **List candidates:** `prepare --email-type personal` uses the CRM's personal-email column, or an
   address on a consumer domain (gmail, yahoo, outlook, icloud...) in any email column. Partners with
   only a firm address are skipped ("no personal email on record"). All other rules (DNU, suppression,
   leadership, recontact window, excluded practices) still apply.
   ```bash
   python3 scripts/partner_outreach.py prepare --state partner-state-personal --email-type personal \
     --contacts crm_export.csv --suppress-file crm_dnu.csv --other-state partner-state \
     --export-emails to_verify.csv
   ```
   `--export-emails` only writes the addresses; nothing is queued. `--other-state` skips anyone already
   emailed or queued in the work-email campaign (matched by name + firm), so no one gets both.
2. **Verify with EmailListVerify:**
   ```bash
   python3 scripts/partner_outreach.py verify --input to_verify.csv --output elv_results.csv
   ```
   One API call (one credit) per address. The API key is injected by the session's network proxy
   for api.emaillistverify.com (nothing to set; `EMAILLISTVERIFY_API_KEY` overrides it locally).
   Results are appended as they come in, so an interrupted run resumes without re-spending
   credits; `--limit N` checks a slice. Stops on a bad key or `error_credit`. Only `ok` counts as
   valid: `ok_for_all` (catch-all), `unknown`, `antispam_system`, `email_disabled`, `dead_server`,
   `invalid_mx`, `disposable` and `spamtrap` are all skipped. A results CSV from another verifier
   (email + status columns) also works.
3. **Queue verified only:** run the same `prepare` again without `--export-emails`, adding
   `--verified-file elv_results.csv --require-verified`. Only `valid`/`deliverable` addresses are queued;
   invalid, catch-all, unknown, risky and unchecked addresses are skipped with the verifier's status
   as the reason. `--require-verified` refuses to run without a results file.
4. **Send:** `send --state partner-state-personal ...` exactly as in the main workflow. Show the user
   the sample before the first live send.

### Daily CRM sync (new personal emails)
VortexRecruitCRM exposes `GET /api/external/personal-email-candidates` (header `x-api-key`, read-only):
candidates with a saved personal email, their EmailListVerify result, DNU flag and a reliable title
from the notes when the title field is empty. `scripts/crm_sync.py` pulls everything changed since the
last run into `VLCRM/crm/personal/candidates.csv` and derives `titled.csv`, `untitled.csv` and
`crm_dnu.csv`. The key is a network secret on this environment (the proxy sends it); `CRM_BASE_URL`
is in `outreach.env`.

`./run_personal_sync.sh sync` (once each weekday) pulls, re-runs `prepare` for the three drips (only
new people are added; anyone queued, sent or suppressed is skipped), and commits the state to VLCRM.
It never sends. The personal drips have no per-firm daily limit (`--per-firm-daily 0`, Jon's choice: the
emails go to personal addresses); the work-email campaign keeps 1 per firm per day. `status` shows the counts; `batch <audience>` builds a send batch only after Jon
approved sending for that drip.

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
   - Otherwise send it directly with `outlook_send_mail` (subject already starts with `RE:`,
     `body_html` already quotes the first email underneath). Never create drafts.
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
