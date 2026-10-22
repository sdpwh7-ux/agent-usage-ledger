#!/usr/bin/env bash
# error_alert.sh - rate-limited alert for new API errors in the usage ledger. Run from cron, e.g.
#   */15 * * * * ALERT_CMD='/path/to/notify "$TITLE" "$BODY"' /path/to/error_alert.sh
# ALERT_CMD is run with bash -c and gets $TITLE and $BODY in its environment.
# Limits (env): ALERT_MIN_INTERVAL (seconds, default 7200), ALERT_MAX_PER_DAY (default 3),
# ALERT_WINDOW_MIN (look-back minutes, default 180). Errors between alerts are batched into the next one.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
S="${AGENT_USAGE_STATE:-$HOME/.local/state/agent-usage}"; mkdir -p "$S"
LAST="$S/alert-last"; CNT="$S/alert-count"
NOW=$(date +%s); TODAY=$(date +%F)
L=$(cat "$LAST" 2>/dev/null || echo 0); [ $((NOW-L)) -lt "${ALERT_MIN_INTERVAL:-7200}" ] && exit 0
d=""; n=0; read -r d n < "$CNT" 2>/dev/null; [ "$d" = "$TODAY" ] || n=0
[ "${n:-0}" -ge "${ALERT_MAX_PER_DAY:-3}" ] && exit 0
OUT=$(python3 "$HERE/report.py" --errors-since "${ALERT_WINDOW_MIN:-180}" 2>&1)
[ -z "$OUT" ] && exit 0
N=$(printf '%s\n' "$OUT" | grep -c '^- ')
export TITLE="API errors: ${N}" BODY="$(printf '%s\n' "$OUT" | head -c 3500)"
if [ -n "${ALERT_CMD:-}" ]; then bash -c "$ALERT_CMD" >/dev/null 2>&1; else printf '%s\n%s\n' "$TITLE" "$BODY"; fi
echo "$NOW" > "$LAST"; echo "$TODAY $(( ${n:-0} + 1 ))" > "$CNT"
