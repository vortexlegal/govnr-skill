#!/usr/bin/env bash
# Partner outreach runner. State (queue, send log, suppression) lives in the private
# vortexlegal/VLCRM repo, branch partner-outreach-state; this script keeps it in sync.
#
#   run_outreach.sh preview [N]      show the next N sends (default 40); changes nothing
#   run_outreach.sh batch            write this run's batch to state/outbox.json (up to OUTREACH_MAX_PER_RUN)
#   run_outreach.sh record [args]    log results, e.g. `record a@x.com` or `record --kind followup1 a@x.com`
#   run_outreach.sh suppress EMAIL.. add opt-outs
#   run_outreach.sh summary          today's totals and replies (for the end-of-day email)
#   run_outreach.sh checks           print the two combined Outlook searches for this outbox
#   run_outreach.sh send             send the current outbox through SendGrid and save (no per-email prompts)
#   run_outreach.sh save "message"   commit and push the state
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
STATE_REPO=${STATE_REPO:-/home/user/vlcrm}
BRANCH=partner-outreach-state
PY="python3 $HERE/scripts/partner_outreach.py"
LOCK="$STATE_REPO/.sending.lock"
if [ "${1:-}" = batch ] && [ -e "$LOCK" ] && kill -0 "$(cat "$LOCK")" 2>/dev/null; then
  echo "SEND IN PROGRESS (pid $(cat "$LOCK")): the previous run is still sending; skip this run's batch."; exit 3
fi

sync() {
  cd "$STATE_REPO"
  git fetch -q origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
  if git rev-parse -q --verify "$BRANCH" >/dev/null; then git checkout -q "$BRANCH"; else git checkout -q -b "$BRANCH" "origin/$BRANCH"; fi
  git merge -q --ff-only "origin/$BRANCH"
  cd - >/dev/null
}
set -a; . "$HERE/outreach.env"; set +a
COMMON=(--state "$STATE_REPO/state" --suppress-file "$STATE_REPO/crm/dnu_all.csv")
for f in "$STATE_REPO"/crm/exclude_*.csv; do [ -e "$f" ] && COMMON+=(--suppress-file "$f"); done  # standing do-not-email lists

case "${1:-}" in
  preview) sync; $PY send "${COMMON[@]}" --firm-reference "$STATE_REPO/crm/partners_loaded.csv" --max-per-run "${2:-40}" --daily-cap "${2:-40}" --per-firm-daily 1 ;;
  batch)   sync; $PY send "${COMMON[@]}" --transport outlook --live --firm-reference "$STATE_REPO/crm/partners_loaded.csv" ;;
  record)  shift; $PY record --state "$STATE_REPO/state" "$@" ;;
  suppress) shift; $PY suppress --state "$STATE_REPO/state" "$@" ;;
  summary) sync; $PY summary --state "$STATE_REPO/state" ;;
  checks)  # print the two combined Outlook searches for the current outbox (one call each instead of one per person)
           python3 - "$STATE_REPO/state/outbox.json" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone
box = json.load(open(sys.argv[1]))
fu = [e for e in box if e["kind"] != "initial"]
new = [e for e in box if e["kind"] == "initial"]
if fu:
    since = min(e.get("original_sent_at") or "" for e in fu)[:10]
    print("FOLLOW-UP REPLY CHECK (%d): outlook_email_search afterDateTime=%s limit=25 query=" % (len(fu), since))
    print(" OR ".join("from:" + e["to"] for e in fu))
if new:
    since = (datetime.now(timezone.utc) - timedelta(days=180)).date().isoformat()
    print("PRIOR-CONTACT CHECK (%d): outlook_email_search afterDateTime=%s limit=25 query=" % (len(new), since))
    print(" OR ".join("to:" + e["to"] for e in new))
PY
           ;;
  send)    # send the current outbox through SendGrid (paced over OUTREACH_SEND_WINDOW_MIN; replies go to Outlook), then save.
           # Takes up to ~45 min: run it in the background. A lock stops the next run's batch from starting meanwhile.
           echo $$ > "$LOCK"; trap 'rm -f "$LOCK"' EXIT
           rc=0; SENDGRID_API_KEY=${SENDGRID_API_KEY:-proxy-injected} $PY send-outbox --state "$STATE_REPO/state" || rc=$?
           "$0" save "sendgrid run $(TZ=America/New_York date '+%F %H:%M') ET"; exit $rc ;;
  save)
    cd "$STATE_REPO"; git add -A state
    if git diff --cached --quiet; then echo "nothing to save"; exit 0; fi
    git -c user.name=Claude -c user.email=noreply@anthropic.com commit -qm "${2:-outreach run}"
    # Another campaign may push to the same branch in between: on a rejected push, fetch and
    # rebase our commit onto theirs (they touch different files), then retry.
    pushed=0
    for i in 1 2 3 4; do
      git push -q origin "$BRANCH" && { pushed=1; break; }
      sleep $((2**i))
      git fetch -q origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" && git rebase -q "origin/$BRANCH" || { git rebase --abort 2>/dev/null; true; }
    done
    if [ $pushed = 1 ]; then echo "saved: $(git log --oneline -1)"
    else echo "SAVE FAILED: committed locally but NOT pushed: $(git log --oneline -1)"; exit 2; fi ;;
  *) sed -n '2,11p' "$0"; exit 1 ;;
esac
