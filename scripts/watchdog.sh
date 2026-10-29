#!/usr/bin/env bash
#
# Detect the failures that produce no red run.
#
# A dropped run, a workflow disabled for inactivity, and an agent broken in a way
# that exits zero all look identical: nothing happens. GitHub only notifies on
# *failure*, so nothing happening notifies nobody.
#
# Alerts two ways: a GitHub issue (which emails the owner) and Telegram.

set -euo pipefail

REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set}"
WORKFLOW="${WATCHDOG_WORKFLOW:-heartbeat.yml}"
MAX_AGE_HOURS="${WATCHDOG_MAX_AGE_HOURS:-4}"

alert() {
  local title="$1"
  local body="$2"

  echo "::error::${title}"
  echo "${body}"

  # Telegram. Never fail the watchdog over a failed alert.
  if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "parse_mode=HTML" \
      --data-urlencode "disable_web_page_preview=true" \
      --data-urlencode "text=⚠️ <b>ShoggothTwenty watchdog</b>

${title}

${body}" \
      || echo "telegram alert failed (non-fatal)"
  fi

  # An issue, deduplicated so we don't open one every day.
  local existing
  existing=$(gh issue list --repo "${REPO}" --state open --search "${title} in:title" \
    --json number --jq '.[0].number // empty')
  if [[ -z "${existing}" ]]; then
    gh issue create --repo "${REPO}" --title "${title}" --body "${body}" \
      || echo "issue creation failed (non-fatal)"
  else
    gh issue comment "${existing}" --repo "${REPO}" --body "Still failing: ${body}" \
      || true
  fi
}

# 1. Has GitHub disabled the schedule for inactivity?
state=$(gh api "repos/${REPO}/actions/workflows/${WORKFLOW}" --jq .state)
echo "workflow ${WORKFLOW} state: ${state}"
if [[ "${state}" != "active" ]]; then
  alert "Heartbeat workflow is ${state}" \
"GitHub reports \`${WORKFLOW}\` as \`${state}\`.

The 60-day inactivity rule disables scheduled workflows in public repositories.
Re-enable it with \`gh workflow enable ${WORKFLOW}\`, and check that the state
commit step is still running — those commits are the keepalive."
  exit 1
fi

# 2. Has there been a successful scheduled run recently?
last=$(gh run list --repo "${REPO}" --workflow "${WORKFLOW}" --event schedule \
  --status success --limit 1 --json startedAt --jq '.[0].startedAt // empty')

if [[ -z "${last}" ]]; then
  alert "Heartbeat has never had a successful scheduled run" \
"No successful scheduled run of \`${WORKFLOW}\` was found.

Check the Actions tab. Common causes: the workflow file isn't on the default
branch, the secrets aren't set, or the very first run failed."
  exit 1
fi

echo "last successful run: ${last}"

age_hours=$(( ( $(date -u +%s) - $(date -u -d "${last}" +%s) ) / 3600 ))
echo "age: ${age_hours}h (threshold ${MAX_AGE_HOURS}h)"

if (( age_hours >= MAX_AGE_HOURS )); then
  alert "Heartbeat has been silent for ${age_hours} hours" \
"The last successful scheduled run of \`${WORKFLOW}\` was \`${last}\` (${age_hours}h ago),
past the ${MAX_AGE_HOURS}h threshold.

Scheduled runs can be delayed or dropped under load, but this is long enough to
suspect a real failure. Check the Actions tab for red runs, or run the workflow
manually to see the error."

  exit 1
fi

echo "heartbeat is healthy"
