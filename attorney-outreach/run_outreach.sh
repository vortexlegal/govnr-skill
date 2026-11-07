#!/usr/bin/env bash
# Partner outreach runner. State (queue, send log, suppression) lives in the private
# vortexlegal/VLCRM repo, branch partner-outreach-state; this script keeps it in sync.
#
#   run_outreach.sh preview [N]      show the next N sends (default 40); changes nothing
#   run_outreach.sh batch            write this run's batch to state/outbox.json (up to OUTREACH_MAX_PER_RUN)
#   run_outreach.sh record [args]    log results, e.g. `record a@x.com` or `record --kind followup1 a@x.com`
#   run_outreach.sh suppress EMAIL.. add opt-outs
#   run_outreach.sh summary          today's totals and replies (for the end-of-day email)
#   run_outreach.sh save "message"   commit and push the state
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
STATE_REPO=${STATE_REPO:-/home/user/vlcrm}
BRANCH=partner-outreach-state
PY="python3 $HERE/scripts/partner_outreach.py"

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
  preview) sync; $PY send "${COMMON[@]}" --max-per-run "${2:-40}" --daily-cap "${2:-40}" --per-firm-daily 1 ;;
  batch)   sync; $PY send "${COMMON[@]}" --transport outlook --live ;;
  record)  shift; $PY record --state "$STATE_REPO/state" "$@" ;;
  suppress) shift; $PY suppress --state "$STATE_REPO/state" "$@" ;;
  summary) sync; $PY summary --state "$STATE_REPO/state" ;;
  save)
    cd "$STATE_REPO"; git add -A state
    if git diff --cached --quiet; then echo "nothing to save"; exit 0; fi
    git -c user.name=Claude -c user.email=noreply@anthropic.com commit -qm "${2:-outreach run}"
    for i in 1 2 3 4; do git push -q origin "$BRANCH" && break || sleep $((2**i)); done
    echo "saved: $(git log --oneline -1)" ;;
  *) sed -n '2,11p' "$0"; exit 1 ;;
esac
