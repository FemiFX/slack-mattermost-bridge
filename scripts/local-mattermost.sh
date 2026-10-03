#!/usr/bin/env bash
# Start a local Mattermost and set up everything the bridge needs on that side:
# a team, a channel, an ordinary (non-bot) user for the bridge with a personal
# access token, and an admin account for you to log in with. Fills the
# Mattermost half of .env.mmbridge if that file does not exist yet.
#
# Slack has no local equivalent; the Slack app still needs a real workspace.
# A free one works.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.mattermost.yml)
PORT="${MATTERMOST_PORT:-8065}"
URL="http://localhost:${PORT}"
TEAM=bridge-demo
CHANNEL=partner-project
BRIDGE_USER=slack-bridge
ADMIN_USER=admin

mmctl() { "${COMPOSE[@]}" exec -T mattermost /mm/mattermost/bin/mmctl --local "$@"; }
password() { python3 -c 'import secrets; print(secrets.token_urlsafe(12) + "Aa1!")'; }
json_field() { python3 -c "import json,sys; d=json.load(sys.stdin); d=d[0] if isinstance(d,list) else d; print(d['$1'])"; }
has_user() {
  mmctl user list --all --json 2>/dev/null | python3 -c "
import json, sys
try:
    users = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
users = users if isinstance(users, list) else [users]
sys.exit(0 if any(u.get('username') == '$1' for u in users) else 1)"
}

# Compose refuses to load the project while the bridge's env_file is missing,
# even to start only Mattermost. Start from the example; it is filled in below.
[ -e .env.mmbridge ] || cp .env.mmbridge.example .env.mmbridge

"${COMPOSE[@]}" up -d mattermost

printf 'Waiting for Mattermost at %s ' "$URL"
for _ in $(seq 1 120); do
  curl -fs "$URL/api/v4/system/ping" >/dev/null 2>&1 && break
  printf '.'
  sleep 2
done
echo
if ! curl -fs "$URL/api/v4/system/ping" >/dev/null 2>&1; then
  echo "Mattermost did not come up. See: ${COMPOSE[*]} logs mattermost" >&2
  exit 1
fi

# The local-mode socket can lag the HTTP listener by a few seconds.
for _ in $(seq 1 30); do
  mmctl system version >/dev/null 2>&1 && break
  sleep 2
done

if has_user "$BRIDGE_USER"; then
  echo "Already set up. To start again from nothing: ${COMPOSE[*]} down"
  exit 0
fi

ADMIN_PASSWORD="$(password)"
mmctl team create --name "$TEAM" --display-name "Bridge demo" >/dev/null
mmctl channel create --team "$TEAM" --name "$CHANNEL" --display-name "Partner project" >/dev/null
mmctl user create --email admin@example.com --username "$ADMIN_USER" \
  --password "$ADMIN_PASSWORD" --system-admin >/dev/null
# An ordinary user, not a Bot Account: a Bot Account cannot post under the
# original author's name (see SETUP.md).
mmctl user create --email bridge@example.com --username "$BRIDGE_USER" \
  --password "$(password)" >/dev/null
mmctl team users add "$TEAM" "$ADMIN_USER" "$BRIDGE_USER" >/dev/null
mmctl channel users add "$TEAM:$CHANNEL" "$ADMIN_USER" "$BRIDGE_USER" >/dev/null

TOKEN="$(mmctl token generate "$BRIDGE_USER" mmbridge --json | json_field token)"
CHANNEL_ID="$(mmctl channel search --team "$TEAM" "$CHANNEL" --json | json_field id)"

if ! grep -q '^MATTERMOST_TOKEN=your-mattermost-access-token$' .env.mmbridge; then
  ENV_NOTE=".env.mmbridge already had Mattermost settings, so it was left alone. Set:
    MATTERMOST_URL=$URL
    MATTERMOST_TOKEN=$TOKEN
    MMBRIDGE_PAIRS=<slack-channel-id>:$CHANNEL_ID:$CHANNEL"
else
  python3 - "$URL" "$TOKEN" "$CHANNEL_ID" "$CHANNEL" <<'EOF'
import re, sys
url, token, channel_id, label = sys.argv[1:]
text = open(".env.mmbridge").read()
text = re.sub(r"(?m)^MATTERMOST_URL=.*$", f"MATTERMOST_URL={url}", text)
text = re.sub(r"(?m)^MATTERMOST_TOKEN=.*$", f"MATTERMOST_TOKEN={token}", text)
text = re.sub(r"(?m)^MMBRIDGE_PAIRS=.*$", f"MMBRIDGE_PAIRS=C0123456789:{channel_id}:{label}", text)
open(".env.mmbridge", "w").write(text)
EOF
  ENV_NOTE=".env.mmbridge now has the Mattermost side filled in."
fi

cat <<EOF

Mattermost is ready at $URL
  Log in as        $ADMIN_USER / $ADMIN_PASSWORD
  Team / channel   $TEAM / $CHANNEL  (channel ID $CHANNEL_ID)
  Bridge account   $BRIDGE_USER, an ordinary user with a personal access token

$ENV_NOTE

Next, the Slack side (README, step 1):
  1. Create the Slack app from slack-app-manifest.yml and invite it to a channel.
  2. In .env.mmbridge, set SLACK_BOT_TOKEN and SLACK_APP_TOKEN, and replace
     C0123456789 in MMBRIDGE_PAIRS with that channel's ID.
  3. Verify, then run it:
       ${COMPOSE[*]} run --rm --build mmbridge check
       ${COMPOSE[*]} up -d --build
EOF
