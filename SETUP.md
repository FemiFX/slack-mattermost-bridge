# Slack app setup, step by step

This is the runbook for creating the Slack side of the bridge and filling in
`.env.mmbridge`. It explains what each piece achieves, not just which button to
press, because almost every failure mode here is conceptual — two different
things called "permission" that do different jobs.

## The mental model: four things must all be true

Slack splits what most people think of as "access" into four independent
mechanisms. The bridge needs all four, and getting three right produces a
service that connects cleanly and then does nothing at all.

```
1. THE APP            a config record in Slack's system.
                      Owns an identity, a bot user, a set of declared
                      capabilities. Created once.
                              │
2. SCOPES             what the app is allowed to DO, workspace-wide.
                      "may read history of public channels it is in"
                      Granted at install time, by an admin.
                              │
3. TOKENS             the credentials that prove the app is the app.
                      Two of them, doing different jobs.
                              │
4. CHANNEL MEMBERSHIP where the app is allowed to do it.
                      NOT implied by scopes. Granted by /invite.
```

The one that trips everybody: **scopes are not access.** `channels:history` does
not mean "read every public channel". It means "may read the history of public
channels this app has been invited to". A workspace can grant the bridge every
scope it asks for and the bridge still sees nothing until somebody types
`/invite` in one specific channel.

That separation is what makes the security conversation with a partner
organisation easy. You are not asking for workspace access. You are asking for
one app approval plus one `/invite`, and the app is structurally incapable of
reading `#hr` or `#finance` because it was never invited there.

## Before you start: which workspace?

`SLACK_BOT_TOKEN` is a single value in `.env.mmbridge`, and a bot token is scoped
to exactly one workspace. So **one running bridge serves one Slack workspace.**

In practice:

- **For testing now:** create the app in your own test workspace. You are the
  admin, so nothing needs approving and you can iterate freely.
- **For production with the partner:** the cleanest path is that the app is
  installed into *their* workspace and you hold the resulting `xoxb-` token. Two
  ways to get there — either they add you as an ordinary guest/member and you
  create the app there yourself, or you enable distribution on your existing app
  and send their admin an installation link. The link route is better: they see
  a consent screen listing exactly the scopes, and approving it is a normal,
  well-understood admin action.

If you later bridge several partners, each needs its own token, which means
either one bridge process per partner or a schema change to hold many
installations. The current design deliberately does the simple thing.

## Step 1 — create the app from the manifest

Go to <https://api.slack.com/apps> → **Create New App** → **From an app
manifest** → pick the workspace → paste the contents of
[`slack-app-manifest.yml`](slack-app-manifest.yml) → **Next** → **Create**.

**What this achieves.** The manifest is a declarative description of the whole
app: its name, its bot user, every scope, every event subscription, and the fact
that it uses Socket Mode. Doing it by hand means twelve separate screens and a
real chance of missing `groups:history`, which fails silently later. Slack shows
you a review screen listing the scopes before it creates anything — that screen
is worth reading, because it is roughly what the partner's admin will see.

After this you have an app and a bot user, but **no tokens and no access yet.**

## Step 2 — the app-level token (`xapp-…`)

**Basic Information → App-Level Tokens → Generate Token and Scopes.**
Name it `socket-mode`, add the scope **`connections:write`**, generate, and copy
the `xapp-…` value into `SLACK_APP_TOKEN`.

**What this achieves.** This token authenticates the *WebSocket connection
itself*. The bridge calls `apps.connections.open` with it, Slack returns a
short-lived `wss://` URL, and the client connects and reconnects to that.

It cannot post a message, fetch history, or look up a user — those need the bot
token. But it is **not** harmless on its own: every event Slack delivers arrives
over the connection it opens, and message events carry the full message text. So
whoever holds it can receive new messages from the app's channels as they are
posted. Slack also spreads events across up to ten open connections, so a rogue
connection would silently take a share of them away from the bridge. Treat it as
a secret exactly like the bot token.

Code: [`slack_adapter.py:286`](mmbridge/slack_adapter.py#L286) builds the
`SocketModeClient` with it; [`config.py`](mmbridge/config.py) rejects the value
at startup if it does not start with `xapp-`, because pasting the bot token into
this slot is the single most common setup mistake.

## Step 3 — install the app, and get the bot token (`xoxb-…`)

**Install App → Install to Workspace.** Read the consent screen. **Allow.**
Copy the **Bot User OAuth Token** (`xoxb-…`) into `SLACK_BOT_TOKEN`.

**What this achieves.** This is the OAuth grant. The screen you just approved is
precisely what the partner's admin will see, so it is worth looking at as them:
it lists the capabilities in plain language, and it is the moment the scopes in
the manifest become real.

The resulting token represents **the application**, not you and not an
administrator. Nobody ever sends anybody a password, and if the partner revokes
the app the token dies with it.

Every Web API call the bridge makes uses this token — posting, reading user
profiles, downloading and uploading files, adding reactions.

Code: [`slack_adapter.py:40`](mmbridge/slack_adapter.py#L40) calls `auth.test`
with it at startup to learn its own `bot_user_id` and `bot_id`, which are the
first-line loop guard.

## Step 4 — invite the app to the channel

In Slack, open the channel and type `/invite @` then pick the bridge from the
autocomplete. (Or: channel name → **Integrations** → **Add apps**.)

**What this achieves.** Everything above was workspace-level. This is the step
that grants access to one conversation, and it is the security boundary that
matters. Without it the bridge starts, connects, authenticates, reports no
errors — and receives zero message events, forever.

`mmbridge check` tests this explicitly, because the silent-failure mode is so
easy to hit.

## Step 5 — find the channel IDs

In Slack: channel name → **View channel details** → scroll to the bottom → the
**Channel ID** (`C…` for public, `C…` or `G…` for private).

Or let the bridge do it once the tokens are in place:

```sh
python -m mmbridge channels
```

That prints the channels the app can see on both platforms, with IDs and whether
the bot is a member — which is usually faster than clicking, and doubles as a
check that the token works.

## A note on the Mattermost side

Use an **ordinary user account with a personal access token**, not a Bot Account.

Mattermost stamps `from_bot: "true"` on every post a Bot Account makes and will
not let it be unset; the webapp then renders the bot's own identity and ignores
`override_username`. The practical effect is that a Bot Account cannot post under
the original author's name, so every bridged message arrives as "…Bridge Bot" and
nobody can tell who wrote what. No server setting changes this — it is a property
of the account type.

An ordinary user account with `from_webhook` + `override_username` renders the
author's name correctly, and — unlike an incoming webhook — still owns its own
posts, so edits, deletes and file uploads keep working.

## Step 6 — fill in `.env.mmbridge`

```sh
cp .env.mmbridge.example .env.mmbridge
```

The four credentials from above, plus the allowlist:

```sh
SLACK_BOT_TOKEN=xoxb-…          # step 3
SLACK_APP_TOKEN=xapp-…          # step 2
MATTERMOST_URL=https://…        # your server
MATTERMOST_TOKEN=…              # the bridge user's personal access token
MMBRIDGE_PAIRS=C0123456789:bx8ck7nd9jd4xqz1r3w5abcdef:partner-project
```

`MMBRIDGE_PAIRS` is the allowlist, and it is the only thing that decides what
gets bridged. Nothing outside it is read or written, even if the app has been
invited elsewhere. The optional third field is just a label for the logs.

## Step 7 — verify before running

```sh
python -m mmbridge check
```

This is worth running before every deploy. It checks, in order: the bot token,
that Socket Mode can actually open a connection, every required and optional
scope against what was really granted, and then per channel: that the channel is
readable, whether it is public or private, whether the app is a member, and
whether the *right* history scope for that channel type was granted. Then the
Mattermost side, including the two server settings that silently discard the
identity override. Then that the mapping database is writable.

Fix everything it marks `FAIL`. Warnings mean the bridge will run with a feature
degraded — worth understanding, not necessarily worth blocking on.

## The scopes, one at a time

| Scope | What it buys | What breaks without it |
|---|---|---|
| `channels:history` | message events in **public** channels | Connects fine, no events ever arrive. Silent. |
| `groups:history` | message events in **private** channels | Same silent failure, private channels only |
| `channels:read` / `groups:read` | channel metadata | `check` cannot resolve the channel; name/privacy unknown |
| `chat:write` | posting at all | Every mirror fails |
| `chat:write.customize` | per-message name + avatar | Caught at [`slack_adapter.py:202`](mmbridge/slack_adapter.py#L202) — falls back to posting as the app rather than dropping the message |
| `users:read` | author names via `users.info` | Authors show as raw `U0123…` IDs |
| `users:read.email` | email on the profile | @mentions from Mattermost degrade to plain text instead of real pings |
| `files:read` | downloading `url_private` | Slack files never reach Mattermost |
| `files:write` | `files_upload_v2` | Mattermost files never reach Slack |
| `reactions:read` / `reactions:write` | reaction events / mirroring them | Reactions stop crossing |
| `channels:manage` / `groups:write` | leaving channels outside the allowlist | The app still ignores those channels and says so, but stays in them. Note these also allow renaming, archiving, removing members and creating channels — the price of automatic leaving |

Only the first five are structurally required. The rest degrade one feature each,
which is why `check` reports them as warnings and the code handles their absence
rather than crashing.

## Scopes and event subscriptions are different things

They are easy to conflate and both are required:

- A **scope** says the app *may* read messages.
- An **event subscription** says Slack should *push* them to you.

The manifest subscribes to `message.channels` (public), `message.groups`
(private), `reaction_added` and `reaction_removed`. If you ever edit
subscriptions by hand, remember that a private-channel bridge needs
`message.groups` *and* `groups:history`. Having one without the other is the
classic silent bridge.

**Any change to scopes requires reinstalling the app** (Slack will show a banner).
Re-copy the bot token afterwards and restart the bridge.

## How it fits together at runtime

```
startup
  │
  ├─ auth.test          (xoxb) ──► learns its own bot_user_id + bot_id
  ├─ apps.connections.open (xapp) ──► short-lived wss:// URL
  └─ WebSocket opens OUTBOUND to Slack ──► no inbound firewall rule needed

someone types in #partner-project
  │
  ├─ Slack pushes an envelope down the socket
  ├─ bridge ACKs within 3 seconds, unconditionally
  │     (Slack redelivers anything unacknowledged; the seen-events table
  │      is what makes a redelivery harmless)
  │
  ├─ guard 1: is bot_id / user our own?          → drop
  ├─ guard 2: is this ts already in the link table? → drop  ← the decisive one
  ├─ guard 3: have we seen this event_id before?    → drop
  │
  ├─ users.info (xoxb) ──► author name + avatar
  ├─ mrkdwn ──► markdown
  ├─ thread root lookup in the link table
  ├─ files: download (xoxb) ──► upload to Mattermost
  ├─ POST to Mattermost with override_username / override_icon_url
  └─ record slack_ts ↔ mm_post_id in the link table
```

The last line is what makes the next four features possible. When that message
is later edited, deleted, replied to, or reacted to, the bridge looks up the row
and knows exactly which message on the far side to act on — and when Mattermost
immediately reports the post the bridge just created, that same row is the proof
it is a mirror and not a new message.

## Common failures, and what they actually mean

| Symptom | Cause |
|---|---|
| Connects, logs nothing, no messages cross | App not invited to the channel, or the history scope for that channel *type* is missing |
| `invalid_auth` at startup | Tokens swapped, or the app was reinstalled and the token changed |
| Socket Mode refused | `SLACK_APP_TOKEN` is not an `xapp-` token, or lacks `connections:write` |
| Everything posts as the bridge account in Mattermost | The Mattermost token belongs to a **Bot Account** (see above), or `EnablePostUsernameOverride` is off |
| Everything posts as the app in Slack | `chat:write.customize` not granted — check the log, the fallback is deliberate |
| Mattermost authors have no avatar in Slack | Expected. Mattermost serves avatars from an authenticated endpoint Slack cannot reach |
| Messages bounce back and forth | Should be impossible. If it happens, `MMBRIDGE_DB` is not on a persistent volume |

## First real test

With `check` clean, run `python -m mmbridge run` in the foreground and watch the
log. Then, in order:

1. Post a plain message in Slack → appears in Mattermost under your name with
   `(via Slack)`.
2. Post in Mattermost → appears in Slack.
3. **Confirm nothing bounces.** Neither message should reappear on its origin
   platform. This is the one that matters.
4. Reply in a thread on each side → lands in the matching thread.
5. Edit, then delete a message on each side.
6. Add a reaction on each side.
7. Upload a file each way.

Set `MMBRIDGE_DIRECTION=slack_to_mm` for the very first run if you would rather
prove one direction in isolation before opening both.
