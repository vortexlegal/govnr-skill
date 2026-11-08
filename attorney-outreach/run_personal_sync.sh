#!/usr/bin/env bash
# Personal-email drips: keep the queues in step with VortexRecruitCRM.
# State lives in vortexlegal/VLCRM, branch partner-outreach-state (same as run_outreach.sh).
#
#   run_personal_sync.sh sync               pull CRM changes, queue new people in all three drips, save
#   run_personal_sync.sh rebuild            re-render all queued emails from the current templates (before any send)
#   run_personal_sync.sh status             people queued / sent per drip
#   run_personal_sync.sh batch <audience> [send options]   write that drip's Outlook batch (outbox.json); sends nothing itself
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
prepare_all() {
  $PY prepare --state "$V/state-personal-partner" --audience partner "${BASE[@]}" \
      --contacts "$W/titled.csv" --verified-file "$W/titled.csv" "${PARTNER_TPL[@]}" \
      --other-state "$V/state-personal-associate" --other-state "$V/state-personal-untitled" | sed -n '1,2p'
  $PY prepare --state "$V/state-personal-associate" --audience associate "${BASE[@]}" \
      --contacts "$W/titled.csv" --verified-file "$W/titled.csv" "${ASSOC_TPL[@]}" \
      --other-state "$V/state-personal-partner" --other-state "$V/state-personal-untitled" | sed -n '1,2p'
  $PY prepare --state "$V/state-personal-untitled" --audience untitled "${BASE[@]}" \
      --contacts "$W/untitled.csv" --verified-file "$W/untitled.csv" \
      --other-state "$V/state-personal-partner" --other-state "$V/state-personal-associate" | sed -n '1,2p'
}
count() { [ -f "$1" ] && python3 -c "import csv,sys;csv.field_size_limit(10**9);print(sum(1 for _ in csv.DictReader(open(sys.argv[1],encoding='utf-8'))))" "$1" || echo 0; }

SUPPRESS=(--suppress-file "$V/crm/dnu_all.csv" --suppress-file "$W/crm_dnu.csv")
for f in "$V"/crm/exclude_*.csv; do [ -e "$f" ] && SUPPRESS+=(--suppress-file "$f"); done
# Seasonal first emails through Dec 31 (Eastern): associates "get your bonus early", partners "start the
# new year on the right platform"; then the regular openers. When the season changes, unsent first
# emails are re-rendered on the next sync.
if [ "$(TZ=America/New_York date +%Y%m%d)" -le 20261231 ]; then
  ASSOC_TPL=(--template "$HERE/templates/associate_bonus_email.txt" --subjects "$HERE/templates/associate_bonus_subjects.txt")
  PARTNER_TPL=(--template "$HERE/templates/partner_newyear_email.txt" --subjects "$HERE/templates/partner_newyear_subjects.txt")
  SEASON=yearend-2026
else
  ASSOC_TPL=(); PARTNER_TPL=(); SEASON=regular
fi
season_check() {
  local c m
  for c in partner associate; do
    m="$V/state-personal-$c/template_season"
    if [ "$(cat "$m" 2>/dev/null)" != "$SEASON" ]; then
      rm -f "$V/state-personal-$c/queue.csv" "$V/state-personal-$c/prepare_skipped.csv"
      mkdir -p "$V/state-personal-$c"; echo "$SEASON" > "$m"; echo "$c first email: $SEASON (unsent emails re-rendered)"
    fi
  done
}
BASE=(--email-type personal "${SUPPRESS[@]}" --firm-domains-from "$V/crm/partners_loaded.csv" --firm-domains-from "$V/crm/extra_law_firms.csv"
      --take-over-from "$V/state" --require-verified)

case "${1:-}" in
  sync)
    pull
    declare -A before; for c in partner associate untitled; do before[$c]=$(count "$V/state-personal-$c/queue.csv"); done
    python3 "$HERE/scripts/crm_sync.py" --out "$W"
    season_check
    prepare_all
    summary="CRM sync $(date -u +%F):"
    for c in partner associate untitled; do
      after=$(count "$V/state-personal-$c/queue.csv"); summary+=" $c +$((after - ${before[$c]})) (queued $after);"
    done
    echo "$summary"
    save "$summary" ;;
  rebuild)
    # Re-render every queued (unsent) first email from the current templates after a wording change.
    # Safe after sending starts: queue.csv only holds first emails not yet sent, follow-ups are written
    # at send time, and anyone already emailed is skipped by the send log.
    pull
    season_check
    for c in partner associate untitled; do rm -f "$V/state-personal-$c/queue.csv" "$V/state-personal-$c/prepare_skipped.csv"; done
    prepare_all
    summary="Rebuild from current templates:"
    for c in partner associate untitled; do summary+=" $c $(count "$V/state-personal-$c/queue.csv");"; done
    echo "$summary"; save "$summary" ;;
  status)
    pull
    for c in partner associate untitled; do
      echo "$c: queued $(count "$V/state-personal-$c/queue.csv"), log rows $(count "$V/state-personal-$c/sent_log.csv")"
    done ;;
  batch)
    a=${2:?audience: partner|associate|untitled}; shift 2; pull
    $PY send --state "$V/state-personal-$a" --audience "$a" "${SUPPRESS[@]}" --per-firm-daily 0 --max-per-run 50 --pause-min 15 --pause-max 45 --transport outlook --live "$@" ;;  # Jon: faster  # no per-firm limit (Jon, personal addresses)
  record)
    a=${2:?audience}; shift 2; $PY record --state "$V/state-personal-$a" "$@" ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
