# slack-mattermost-bridge

A bidirectional bridge between one Slack channel and one Mattermost channel, so
two organisations on two platforms can work in what feels like a single room.

Messages, threads, files, edits, deletes and reactions cross in both directions.
Every bridged message carries the author's own name and a marker saying which
platform it came from. The package and command are `mmbridge`.

```
Mattermost #partner-project              Slack #partner-project
        │                                        │
        │  WebSocket + REST v4      Socket Mode + Web API
        ▼                                        ▼
    ┌───────────────────────────────────────────────┐
    │                   mmbridge                    │
    │  normalise → map identity → convert markdown  │
    │  thread lookup · dedupe · file proxy          │
    └────────────────────┬──────────────────────────┘
                         │
                  SQLite mapping table
                  (post_id ↔ ts)
```

## Why it is built this way

**Socket Mode, not an HTTPS endpoint.** The bridge dials out to Slack, so it can
sit beside a self-hosted Mattermost with no inbound firewall rule, no public
hostname and no certificate to renew.

**A mapping table, not just an author check.** Every mirrored message is recorded
as a pair of IDs. That table is what makes threads, edits, deletes and reactions
land on the right message — and it is the exact loop guard: a message the bridge
itself posted comes straight back as an event, and an ID already in the table is
proof the bridge created it. Author identity alone is fragile, because Slack
reports bot posts inconsistently across subtypes.

**An explicit channel allowlist.** The bridge moves messages and files between
two security domains. Which channels are connected has to be a deliberate,
auditable decision, never inferred from wherever the app happens to have been
invited.

**One replica.** Slack Socket Mode and the Mattermost WebSocket each deliver an
event once per connection, so a second replica would mirror everything twice.
That constraint is also why SQLite is enough.

## Try it locally

Slack has no local equivalent, so the Slack side always needs a real workspace
(a free one works). The Mattermost side can run on your machine:

```sh
scripts/local-mattermost.sh
```

That starts Mattermost at <http://localhost:8065>, with the three server
settings the bridge needs switched on. It creates a team, a channel, an ordinary
user for the bridge with a personal access token, and an admin account for you,
then writes the Mattermost half of `.env.mmbridge`. It finishes by printing the
login and the remaining Slack steps. Then:

```sh
compose="docker compose -f docker-compose.yml -f docker-compose.mattermost.yml"
$compose run --rm --build mmbridge check   # verify first
$compose up -d --build                     # then run the bridge
```

Before the Slack tokens are filled in, `check` fails the Slack half and still
verifies the Mattermost half. The bundled Mattermost is a demo image with its
database inside the container, so it is for trying the bridge, not for running
it.

## Setup

[`ARCHITECTURE.md`](ARCHITECTURE.md) explains how the bridge works internally.
[`PARTNER-ONBOARDING.md`](PARTNER-ONBOARDING.md) covers getting the app into a partner's workspace.
[`SETUP.md`](SETUP.md) is the detailed runbook — what each token, scope and
subscription actually does, and why granting every scope still leaves the bridge
seeing nothing until somebody types `/invite`. The short version:

### 1. The Slack app

Go to <https://api.slack.com/apps> → **Create New App** → **From an app
manifest**, and paste [`slack-app-manifest.yml`](slack-app-manifest.yml). It
declares the scopes, the event subscriptions and Socket Mode in one step.

Then, in the app's settings:

- **Basic Information → App-Level Tokens** → generate a token with the
  `connections:write` scope. This is `SLACK_APP_TOKEN` (`xapp-…`).
- **Install App** → install it, and copy the **Bot User OAuth Token**. This is
  `SLACK_BOT_TOKEN` (`xoxb-…`).
- In Slack itself, invite the app to the channel: `/invite @Mattermost Bridge`.

Scopes and channel membership are two separate things. `channels:history` does
not give the app access to a conversation it has not been invited to — which is
the property that makes the security model easy to explain.

### 2. What to ask the partner organisation for

You do not need admin access to their Slack workspace, and you should not ask
for it. The whole request is:

> We don't need administrative access to your Slack workspace. We need an
> administrator or authorised app manager to approve our Slack integration once,
> and the integration needs to be invited to the single channel we're bridging.

Send them the installation link. They see the scope list, approve it, and invite
the app to one channel. The token that comes back represents the application, not
a person — nobody sends anybody a password.

### 3. The Mattermost account

**Use an ordinary user account, not a Bot Account.** This is counter-intuitive
and it is the one setup decision that cannot be fixed later by a config toggle.
Mattermost stamps `from_bot: "true"` on every post made by a Bot Account, refuses
to let it be unset, and the webapp then renders the bot's own name and ignores
`override_username` entirely. A Bot Account therefore *cannot* post under the
original author's name, no matter how the server is configured.

So:

1. **System Console → Integrations → Integration Management** → enable
   **Personal Access Tokens**, plus **override usernames** and **override
   profile picture icons**.
2. Create an ordinary user, e.g. `slack-bridge` — not a Bot Account.
3. Issue it a personal access token and put that in `MATTERMOST_TOKEN`.
4. Add the account to the channel you are bridging — as in Slack, a token alone
   does not grant access to a private channel.

`mmbridge check` verifies all four. If the token does belong to a Bot Account the
bridge still runs: it detects this at startup, logs a warning, and prefixes the
author's name into the message text so attribution is never lost. Only the
Mattermost direction degrades — posting into Slack is unaffected.

### 4. Configure and verify

```sh
cp .env.mmbridge.example .env.mmbridge   # then fill it in
pip install -r requirements.txt

python -m mmbridge channels   # lists channel IDs on both platforms
python -m mmbridge check      # verifies tokens, scopes, membership, settings
python -m mmbridge run
```

`check` is worth running before every deployment. Nearly every failure in a
bridge like this is a setup problem — a scope that was never granted, an app that
was never invited, a server setting that silently discards the identity override
— and it surfaces all of them at once, before a single message is mirrored.

### 5. Deploy with Docker

```sh
docker compose up -d --build                             # build and start
docker compose logs -f                                   # follow the log (timestamps are UTC)
docker compose exec mmbridge python -m mmbridge check    # verify from inside
docker compose down                                      # stop; the volume is kept
```

The database lives on the `mmbridge-data` volume, and the Compose file pins
`MMBRIDGE_DB=/data/mmbridge.sqlite3` whatever `.env.mmbridge` says. It holds the
message mapping behind threading, edits and loop prevention, and with token
rotation on it holds **the only working Slack token**. So:

- **Never `docker compose down -v`.** It deletes the volume, and the rotating
  token with it. The long-lived token was exchanged at bootstrap and no longer
  works, so recovering means reinstalling the Slack app.
- **Never run two copies.** A second instance mirrors every message twice, and
  the two spend each other's single-use refresh tokens. Stop the old one before
  starting the new one, whether it runs locally or on another host.

**Moving an existing database into the volume** (from a local run, or from
another host), with the source bridge stopped first:

```sh
docker compose run --rm --no-deps --user root \
  -v "$PWD/mmbridge.sqlite3:/seed/mmbridge.sqlite3:ro" --entrypoint sh mmbridge \
  -c 'cp /seed/mmbridge.sqlite3 /data/ && chown mmbridge:mmbridge /data/mmbridge.sqlite3'
```

Then rename or delete the source copy. Its refresh token is spent the first time
the container refreshes, so it must never be run again.

**Exporting it**, again with the bridge stopped (`docker compose stop`):

```sh
docker compose run --rm --no-deps --user root -v "$PWD:/out" \
  --entrypoint cp mmbridge /data/mmbridge.sqlite3 /out/mmbridge-export.sqlite3
```

**On the server:**

- **Build there.** An image built on an Apple Silicon Mac is arm64; most servers
  are amd64. Building on the server avoids the mismatch, or use
  `docker buildx build --platform linux/amd64`.
- **Copy the project without local state:** leave out `.venv/`, `.env*` and
  `*.sqlite3*`, and create `.env.mmbridge` on the server.
- **Remove `MMBRIDGE_TOKEN_REFRESH_MARGIN`** if it was set for testing.
- **As a Swarm stack:** `docker stack deploy`
  ignores `build:`, so build the image on the node first. Keep `replicas: 1`.

## Configuration

Everything is environment variables; see
[`.env.mmbridge.example`](.env.mmbridge.example) for the annotated list.

| Variable | Purpose |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-…`, the Web API |
| `SLACK_APP_TOKEN` | `xapp-…`, the Socket Mode connection |
| `MATTERMOST_URL` / `MATTERMOST_TOKEN` | server and bot access token |
| `MMBRIDGE_PAIRS` | the allowlist: `SLACK_ID:MM_ID[:label]`, comma separated |
| `MMBRIDGE_DIRECTION` | `both`, `slack_to_mm` or `mm_to_slack` |
| `MMBRIDGE_SYNC_FILES` / `_EDITS` / `_DELETES` / `_REACTIONS` | feature switches |
| `MMBRIDGE_IDENTITY_OVERRIDE` | per-author name and avatar, or a name prefix |
| `MMBRIDGE_PROVENANCE_SLACK` / `_MATTERMOST` | the "(via …)" markers |
| `MMBRIDGE_DB` | mapping database path — must be persistent |
| `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` | switch on token rotation; see [ARCHITECTURE.md](ARCHITECTURE.md#token-rotation) |
| `MMBRIDGE_TOKEN_KEY` | encrypts the stored rotating token; `python -m mmbridge genkey` makes one |
| `SLACK_REFRESH_TOKEN` | bootstrap for an app installed *after* rotation was switched on |
| `MMBRIDGE_TOKEN_REFRESH_MARGIN` | seconds before expiry to refresh (default 3600) |
| `*_FILE` | any secret can be read from a file instead, e.g. `SLACK_CLIENT_SECRET_FILE=/run/secrets/…` |
| `MMBRIDGE_SLACK_LEAVE_UNAUTHORISED` | leave any Slack channel not in the allowlist (needs `channels:manage` / `groups:write`) |
| `MMBRIDGE_SLACK_LEAVE_NOTICE` | message posted once before leaving; empty to leave silently |
| `MMBRIDGE_SLACK_ICON_URL` | icon beside Mattermost authors' messages in Slack; empty means the app's icon |
| `MMBRIDGE_MATTERMOST_ICON_URL` | icon in Mattermost for Slack authors without a picture of their own; people who chose one keep it |
| `MMBRIDGE_SLACK_BROADCAST_REPLIES` | also show Mattermost replies in the Slack channel, not only in the thread (default on) |
| `MMBRIDGE_SLACK_FILE_STYLE` | `thread` (author's name, file beneath) or `inline` (one message, name in text) |
| `MMBRIDGE_MAX_FILE_MB` | files above this are skipped with a log line |

## What crosses, and what it looks like

| | Behaviour |
|---|---|
| Messages | Both ways, with mrkdwn ↔ markdown conversion |
| Threads | Replies land in the matching thread; a reply whose root was never bridged becomes a new root rather than being dropped |
| Files | Downloaded from the source and re-uploaded to the target |
| Edits / deletes | Propagate in the direction the message originally travelled |
| Reactions | Both ways, from the bridge account; skin-tone modifiers stripped |
| @mentions | Type the name as the other side shows it — first name, last name, full name or handle. Resolved against the bridged channel's members; only an unambiguous match notifies, and an unmatched name can't notify anyone by mistake |
| Slash commands, workflows, Block Kit | Not bridged |

Two deliberate rough edges:

- **Mattermost avatars don't show in Slack.** Mattermost serves them only to
  logged-in clients, however public the server is, and Slack fetches icons
  anonymously. The author's name still shows; the icon is `MMBRIDGE_SLACK_ICON_URL`
  if set (an organisation logo works well), otherwise the app's own icon.
- **A Mattermost file arrives in Slack as two messages by default.** Slack
  strips the username override from any message a file is attached to, so you
  can have the author's name in the header *or* the file inside the same
  message, never both. `MMBRIDGE_SLACK_FILE_STYLE=thread` (default) keeps the
  name and threads the file beneath; `inline` sends one message carrying the
  file and names the author in the text. The reverse direction has no such
  limit — Mattermost takes `file_ids` on the post itself, so a Slack file
  arrives as a single message.

## A note on data protection

The bridge is an automated export path between two security domains: anything
posted in the bridged Mattermost channel, files included, leaves for the partner's
Slack workspace, and vice versa. That is the point of it, but it should be a
decision somebody made on purpose. The channel allowlist, the provenance markers
and `MMBRIDGE_SYNC_FILES` exist so the scope of that transfer is explicit,
visible to participants, and narrowable.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest
```

176 tests. They cover the routing rules — loop prevention, thread mapping, file
transfer, edits, deletes, reactions, direction switches — against fake adapters,
plus mention resolution, allowlist enforcement, token rotation, the markdown
conversion and the mapping store. No network required.

## Layout

| File | Role |
|---|---|
| [`bridge.py`](mmbridge/bridge.py) | routing: loops, threading, files, identity |
| [`slack_adapter.py`](mmbridge/slack_adapter.py) | Socket Mode + Web API |
| [`mattermost_adapter.py`](mmbridge/mattermost_adapter.py) | WebSocket + REST v4 |
| [`models.py`](mmbridge/models.py) | the platform-neutral message model |
| [`mdconv.py`](mmbridge/mdconv.py) | mrkdwn ↔ markdown |
| [`store.py`](mmbridge/store.py) | the ID mapping table |
| [`preflight.py`](mmbridge/preflight.py) | `check` and `channels` |
| [`config.py`](mmbridge/config.py) | environment loading and validation |
| [`mentions.py`](mmbridge/mentions.py) | @mention resolution against the bridged channels' members |
| [`slack_tokens.py`](mmbridge/slack_tokens.py) | Slack token rotation and encrypted storage |
| [`scripts/local-mattermost.sh`](scripts/local-mattermost.sh) | the local Mattermost for trying it |

Adapters translate into and out of `BridgeMessage`; the routing core never sees a
Slack or Mattermost payload. Adding Teams or Matrix later means writing one
adapter, not touching the routing.

## Licence

MIT — see [LICENSE](LICENSE).
