---
name: attorney-outreach
description: Recruit attorneys from a CRM by emailing them about open positions that closely match what they do today. Use when the user wants to reach out to lawyers/attorneys in their CRM (Salesforce, HubSpot, Clio Grow, Bullhorn, or a CSV export) about job openings, lateral moves, or in-house roles similar to their current practice. Matches each attorney to the closest open position, writes a personalized email for each match, and creates drafts for human review. It never sends in bulk without explicit approval.
---

# Attorney Outreach: Matching Attorneys to Similar Roles

Goal: for each attorney in the CRM, find the open position that most closely
resembles their **current** practice (same practice areas, similar seniority,
right bar admission) and send a short, personal, honest note asking if they'd be
interested.

## Inputs

1. **Contacts.** A CRM export, or records pulled from a connected CRM tool. Column
   names are flexible: the script recognizes common aliases (`First Name`,
   `Job Title`, `Account Name`, `Email Opt Out`, ...). Useful fields: name, email,
   firm, title, practice areas, bar admissions, years of experience, notes/bio,
   and an opt-out flag.
2. **Positions.** The open roles: title, company (can be "Confidential"),
   location/remote, practice areas, description, min/max years, required bar,
   comp range, link. See `examples/positions.csv`.
3. **Sender details.** Name, title, company, reply email, and a physical mailing
   address (required for commercial email under CAN-SPAM).

If a CRM connector is available (search tools for "crm", "salesforce",
"hubspot", "contacts"), pull contacts from it and write them to a CSV. Otherwise
ask the user for an export. If the user has no positions list, ask for one.
Never invent openings: every email has to describe a real role.

## Workflow

1. **Collect inputs** (above). Confirm the sender identity and which positions are live.
2. **Match and draft:**
   ```bash
   python3 scripts/match_and_draft.py \
     --contacts contacts.csv --positions positions.csv --out ./outreach \
     --sender-name "..." --sender-title "..." --sender-company "..." \
     --sender-email "..." --sender-address "..." \
     --min-score 0.35 --top 1
   ```
   Scoring = 60% practice-area overlap + 40% TF-IDF text similarity (title,
   practice areas, notes vs. role title, areas, description), minus
   penalties for an experience-band mismatch or a missing required bar
   admission. Contacts are skipped when they are flagged do-not-contact,
   have an invalid email, are duplicates, or have no close match.
3. **Review with the user.** Show a table: name, current role, matched position,
   score, flags. Show 1–2 full sample emails. Call out anything flagged
   (e.g. "not admitted in NY"). Let the user edit the template
   (`templates/outreach_email.txt`, `$placeholders`) or individual drafts, and
   mark approvals (the `approved` column in `drafts.csv`).
4. **Create drafts, not sends.** Using the connected mail tool (e.g. Outlook
   `outlook_create_draft`, Gmail create-draft), create one draft per approved
   row with that row's `to`, `subject`, and `body`. Send only if the user
   explicitly says to send, and confirm the count first ("Send 37 emails
   now?"). For large batches, pace the sends to respect rate limits.
5. **Log back to the CRM** if a connector allows it: add an activity/note
   ("Outreach re: <position_id>") so the attorney isn't contacted twice.

## Rules

- **Truthful.** Describe the real role. The email says the role is similar to
  their work *because it is* (shared practice areas). It must not claim a
  personal referral, a prior relationship, or an offer.
- **Respect opt-outs.** Skip anyone marked do-not-contact or unsubscribed, and
  honor "unsubscribe" replies. Keep the opt-out line and mailing address in the
  template.
- **Confidentiality.** Don't name a confidential client. Don't tell one
  candidate about another.
- **One role per person** by default (`--top 1`). Multiple pitches read as spam.
- **Jurisdiction.** Attorney-solicitation rules don't govern recruiting
  emails, but anti-spam/privacy law does (CAN-SPAM in the US; CASL in Canada
  and GDPR/PECR in the EU and UK need consent or a lawful basis). Flag non-US
  contacts for the user to decide.

## Files

- `scripts/match_and_draft.py`: matching + drafting (stdlib only)
- `templates/outreach_email.txt`: email body template
- `examples/`: sample CRM export and positions list
