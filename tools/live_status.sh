#!/usr/bin/env bash
# live_status.sh -- publish job progress as commit statuses (numbers only).
#
#   live_status.sh watch <progress-file> <context>
#       Background loop. Reads the last "(k/n)" (or "k/n") found in the file and
#       posts a "pending" status such as "240/464 · ETA 4m". Posts when progress
#       moved by >= LIVE_PCT percent or LIVE_MAX_GAP seconds passed, and never
#       more often than once per LIVE_MIN_GAP seconds. Stops when its parent
#       shell ends, on TERM, or after LIVE_MAX_SECS.
#   live_status.sh final <progress-file> <context> <state>
#       One last post. state: success | failure | pending (or an exit code,
#       where 0 means success).
#
# Environment:
#   GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_SHA   provided by the runner
#   LIVE_DRY_RUN=1   print the request instead of sending it
#   LIVE_POLL=15 LIVE_MIN_GAP=180 LIVE_MAX_GAP=180 LIVE_PCT=5 LIVE_MAX_SECS=21600
#
# The token is only ever placed in a request header; it is never printed.
# Any failing request is ignored: progress is optional and must not break a job.

set +x
set +e

POLL="${LIVE_POLL:-15}"
MIN_GAP="${LIVE_MIN_GAP:-180}"   # 20 parts x 20/h = 400/h, under the ~1000/h token limit
MAX_GAP="${LIVE_MAX_GAP:-180}"
PCT="${LIVE_PCT:-5}"
MAX_SECS="${LIVE_MAX_SECS:-21600}"

# Prints "k n" for the last progress marker in the file, or nothing.
last_progress() {
  [ -f "$1" ] || return 0
  local m
  m=$(sed -nE 's/.*\(([0-9]+)\/([0-9]+)\).*/\1 \2/p' "$1" | tail -n 1)
  if [ -z "$m" ]; then
    m=$(sed -nE 's/(^|.*[^0-9\/])([0-9]+)\/([0-9]+)([^0-9\/].*|$)/\2 \3/p' "$1" | tail -n 1)
  fi
  printf '%s' "$m"
}

# post <context> <state> <description>
post() {
  local ctx="$1" state="$2" desc="$3"
  local body
  body=$(printf '{"state":"%s","context":"%s","description":"%s"}' "$state" "$ctx" "$desc")
  if [ -n "$LIVE_DRY_RUN" ]; then
    printf 'POST %s %s %s\n' "$ctx" "$state" "$desc"
    return 0
  fi
  [ -n "$GITHUB_TOKEN" ] && [ -n "$GITHUB_REPOSITORY" ] && [ -n "$GITHUB_SHA" ] || return 0
  curl -sS -m 20 -o /dev/null -X POST \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$GITHUB_REPOSITORY/statuses/$GITHUB_SHA" \
    -d "$body" >/dev/null 2>&1
  return 0
}

fmt_eta() {
  local s="$1"
  if [ "$s" -lt 60 ]; then printf '<1m'
  elif [ "$s" -lt 3600 ]; then printf '%dm' $(( (s + 30) / 60 ))
  else printf '%dh%02dm' $(( s / 3600 )) $(( (s % 3600 + 30) / 60 ))
  fi
}

cmd_watch() {
  local file="$1" ctx="$2"
  local t0 now gap last_t=0 last_pct=-100 k n pct el eta desc sp
  t0=$(date +%s)
  trap '[ -n "$sp" ] && kill "$sp" 2>/dev/null; exit 0' TERM INT
  while kill -0 "$PPID" 2>/dev/null; do
    read -r k n <<EOF
$(last_progress "$file")
EOF
    now=$(date +%s)
    if [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null; then
      pct=$(( 100 * k / n ))
      gap=$(( now - last_t ))
      if [ "$gap" -ge "$MIN_GAP" ] && { [ "$last_t" -eq 0 ] || [ $(( pct - last_pct )) -ge "$PCT" ] || [ "$gap" -ge "$MAX_GAP" ]; }; then
        el=$(( now - t0 ))
        if [ "$k" -gt 0 ]; then
          eta=$(( el * (n - k) / k ))
          desc="$k/$n · ETA $(fmt_eta "$eta")"
        else
          desc="$k/$n"
        fi
        post "$ctx" pending "$desc"
        last_t=$now
        last_pct=$pct
      fi
    fi
    [ $(( now - t0 )) -ge "$MAX_SECS" ] && break
    sleep "$POLL" &
    sp=$!
    wait "$sp"
  done
  return 0
}

cmd_final() {
  local file="$1" ctx="$2" state="$3" k n desc
  case "$state" in 0) state=success ;; success|failure|pending) ;; *) state=failure ;; esac
  read -r k n <<EOF
$(last_progress "$file")
EOF
  [ -n "$n" ] || { k=0; n=0; }
  if [ "$state" = success ] && [ "$n" -gt 0 ]; then k=$n; fi
  desc="$k/$n"
  post "$ctx" "$state" "$desc"
}

case "$1" in
  watch) cmd_watch "$2" "$3" ;;
  final) cmd_final "$2" "$3" "$4" ;;
  *) echo "usage: live_status.sh watch|final <file> <context> [state]" >&2 ;;
esac
exit 0
