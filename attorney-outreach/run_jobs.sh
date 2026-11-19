#!/usr/bin/env bash
# Job-order campaign runner (Jon 2026-11-18): hourly, weekdays 9am-6pm ET.
# Pulls NEW job orders from VortexRecruitCRM, matches CRM attorneys by the posting's years of experience
# (plus practice fit and state), and sends one email per match through SendGrid, paced.
# State: vortexlegal/VLCRM, branch partner-outreach-state, folder state-jobs/ (+ crm/attorney_candidates.csv).
#
#   run_jobs.sh sync      pull CRM job orders + attorneys, import new openings, match, queue  (no email)
#   run_jobs.sh batch     write this run's batch to state-jobs/outbox.json                  (no email)
#   run_jobs.sh checks    print the combined Outlook prior-contact searches for the outbox (25 addresses each)
#   run_jobs.sh send      send the outbox through SendGrid, paced over JOBS_SEND_WINDOW_MIN, then save (run in background)
#   run_jobs.sh status | summary | save "msg"
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
STATE_REPO=${STATE_REPO:-/home/user/vlcrm}
BRANCH=partner-outreach-state
S="$STATE_REPO/state-jobs"
PY="python3 $HERE/scripts/partner_outreach.py"
JO="python3 $HERE/scripts/job_outreach.py"
LOCK="$STATE_REPO/.sending-jobs.lock"
set -a; . "$HERE/outreach.env"; set +a
: "${JOBS_DAILY_CAP:=1200}" "${JOBS_MAX_PER_RUN:=120}" "${JOBS_SEND_WINDOW_MIN:=45}" "${JOBS_PER_FIRM_DAILY:=3}"

sync_repo() {
  cd "$STATE_REPO"
  git fetch -q origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
  git checkout -q "$BRANCH" 2>/dev/null || git checkout -q -b "$BRANCH" "origin/$BRANCH"
  git merge -q --ff-only "origin/$BRANCH"
  cd - >/dev/null
}
SUPP=(--suppress-file "$STATE_REPO/crm/dnu_all.csv")
for f in "$STATE_REPO"/crm/exclude_*.csv "$STATE_REPO"/state*/suppression.csv; do [ -e "$f" ] && SUPP+=(--suppress-file "$f"); done

if [ "${1:-}" = batch ] && [ -e "$LOCK" ] && kill -0 "$(cat "$LOCK")" 2>/dev/null; then
  echo "SEND IN PROGRESS (pid $(cat "$LOCK")): the previous job-order run is still sending; skip this run's batch."; exit 3
fi

case "${1:-}" in
  sync)    sync_repo; mkdir -p "$S"; $JO sync --state "$S" --crm-dir "$STATE_REPO/crm" "${SUPP[@]}" ; "$0" save "jobs: sync $(TZ=America/New_York date '+%F %H:%M') ET" ;;
  match)   $JO match --state "$S" --crm-dir "$STATE_REPO/crm" "${SUPP[@]}" ;;
  batch)   sync_repo; $PY send --state "$S" "${SUPP[@]}" --transport outlook --live --audience partner \
             --daily-cap "$JOBS_DAILY_CAP" --max-per-run "$JOBS_MAX_PER_RUN" --followup-days 0 \
             --per-firm-daily "$JOBS_PER_FIRM_DAILY" --recontact-days 14 --firm-reference "$STATE_REPO/crm/partners_loaded.csv" --start-hour 9 --end-hour 19 ;;
  checks)  python3 - "$S/outbox.json" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone
box = [e for e in json.load(open(sys.argv[1])) if e["kind"] == "initial"]
since = (datetime.now(timezone.utc) - timedelta(days=180)).date().isoformat()
for i in range(0, len(box), 25):
    part = box[i:i + 25]
    print(f"PRIOR-CONTACT CHECK {i // 25 + 1} ({len(part)}): outlook_email_search afterDateTime={since} limit=25 query=")
    print(" OR ".join("to:" + e["to"] for e in part))
PY
           ;;
  send)    echo $$ > "$LOCK"; trap 'rm -f "$LOCK"' EXIT
           rc=0; SENDGRID_API_KEY=${SENDGRID_API_KEY:-proxy-injected} $PY send-outbox --state "$S" \
             --category job-outreach --window-min "$JOBS_SEND_WINDOW_MIN" --alt-sender-email "${JOBS_ALT_SENDER_EMAIL:-}" || rc=$?
           "$0" save "jobs: sendgrid run $(TZ=America/New_York date '+%F %H:%M') ET"; exit $rc ;;
  status)  $JO status --state "$S" ;;
  summary) $PY summary --state "$S" | sed '1s/^Partner outreach/Job-order outreach/' ;;
  save)
    cd "$STATE_REPO"; git add -A state-jobs crm/attorney_candidates.csv 2>/dev/null || true
    if git diff --cached --quiet; then echo "nothing to save"; exit 0; fi
    git -c user.name=Claude -c user.email=noreply@anthropic.com commit -qm "${2:-jobs run}"
    pushed=0
    for i in 1 2 3 4; do
      git push -q origin "$BRANCH" && { pushed=1; break; }
      sleep $((2**i))
      git fetch -q origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" && git rebase -q "origin/$BRANCH" || { git rebase --abort 2>/dev/null; true; }
    done
    if [ $pushed = 1 ]; then echo "saved: $(git log --oneline -1)"
    else echo "SAVE FAILED: committed locally but NOT pushed: $(git log --oneline -1)"; exit 2; fi ;;
  *) sed -n '2,13p' "$0"; exit 1 ;;
esac
