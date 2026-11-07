#!/usr/bin/env bash
# Personal-email drips: keep the queues in step with VortexRecruitCRM.
# State lives in vortexlegal/VLCRM, branch partner-outreach-state (same as run_outreach.sh).
#
#   run_personal_sync.sh sync               pull CRM changes, queue new people in all three drips, save
#   run_personal_sync.sh status             people queued / sent per drip
#   run_personal_sync.sh batch <audience>   write that drip's send batch (partner|associate|untitled); sends nothing
#   run_personal_sync.sh record <audience> [args]   log sends for that drip
#
# Nothing here sends email. `sync` only adds people to queues; sending is a separate, approved step.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
STATE_REPO=${STATE_REPO:-/home/user/vlcrm}
BRANCH=partner-outreach-state
PY="python3 $HERE/scripts/partner_outreach.py"
V=$STATE_REPO; W=$V/crm/personal
set -a; . "$HERE/outreach.env"; set +a

pull() {
  cd "$STATE_REPO"
  git fetch -q origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
  git checkout -q "$BRANCH" 2>/dev/null || git checkout -q -b "$BRANCH" "origin/$BRANCH"
  git merge -q --ff-only "origin/$BRANCH"
  cd - >/dev/null
}
save() {
  cd "$STATE_REPO"; git add -A crm state state-personal-*
  if git diff --cached --quiet; then echo "nothing to save"; cd - >/dev/null; return 0; fi
  git -c user.name=Claude -c user.email=noreply@anthropic.com commit -qm "$1"
  for i in 1 2 3 4; do git push -q origin "$BRANCH" && break || sleep $((2**i)); done
  echo "saved: $(git log --oneline -1)"; cd - >/dev/null
}
count() { [ -f "$1" ] && python3 -c "import csv,sys;csv.field_size_limit(10**9);print(sum(1 for _ in csv.DictReader(open(sys.argv[1],encoding='utf-8'))))" "$1" || echo 0; }

SUPPRESS=(--suppress-file "$V/crm/dnu_all.csv" --suppress-file "$W/crm_dnu.csv")
for f in "$V"/crm/exclude_*.csv; do [ -e "$f" ] && SUPPRESS+=(--suppress-file "$f"); done
BASE=(--email-type personal "${SUPPRESS[@]}" --firm-domains-from "$V/crm/partners_loaded.csv"
      --take-over-from "$V/state" --require-verified)

case "${1:-}" in
  sync)
    pull
    declare -A before; for c in partner associate untitled; do before[$c]=$(count "$V/state-personal-$c/queue.csv"); done
    python3 "$HERE/scripts/crm_sync.py" --out "$W"
    $PY prepare --state "$V/state-personal-partner" --audience partner "${BASE[@]}" \
        --contacts "$W/titled.csv" --verified-file "$W/titled.csv" \
        --other-state "$V/state-personal-associate" --other-state "$V/state-personal-untitled" | sed -n '1,2p'
    $PY prepare --state "$V/state-personal-associate" --audience associate "${BASE[@]}" \
        --contacts "$W/titled.csv" --verified-file "$W/titled.csv" \
        --other-state "$V/state-personal-partner" --other-state "$V/state-personal-untitled" | sed -n '1,2p'
    $PY prepare --state "$V/state-personal-untitled" --audience untitled "${BASE[@]}" \
        --contacts "$W/untitled.csv" --verified-file "$W/untitled.csv" \
        --other-state "$V/state-personal-partner" --other-state "$V/state-personal-associate" | sed -n '1,2p'
    summary="CRM sync $(date -u +%F):"
    for c in partner associate untitled; do
      after=$(count "$V/state-personal-$c/queue.csv"); summary+=" $c +$((after - ${before[$c]})) (queued $after);"
    done
    echo "$summary"
    save "$summary" ;;
  status)
    pull
    for c in partner associate untitled; do
      echo "$c: queued $(count "$V/state-personal-$c/queue.csv"), log rows $(count "$V/state-personal-$c/sent_log.csv")"
    done ;;
  batch)
    a=${2:?audience: partner|associate|untitled}; pull
    $PY send --state "$V/state-personal-$a" --audience "$a" "${SUPPRESS[@]}" --transport outlook --live ;;
  record)
    a=${2:?audience}; shift 2; $PY record --state "$V/state-personal-$a" "$@" ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
