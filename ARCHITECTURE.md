# How the bridge works

A detailed walk through the design: the moving parts, what happens to a message
end to end, and the platform constraints that forced particular choices.

## 1. The shape of it

The bridge is a single long-lived process holding two live connections. It owns
no UI and stores no message content — only a table of ID pairs.

```
  Mattermost                    mmbridge                     Slack
 ────────────                 ────────────                 ─────────

  WebSocket  ───── events ────►  MattermostAdapter
                                        │
                                   BridgeMessage          ← the neutral model
                                        │
                                   Bridge (routing)
                                   ├─ loop guards
                                   ├─ thread lookup ──► SQLite link table
                                   ├─ markdown xlate
                                   ├─ identity
                                   └─ file proxy
                                        │
                                   BridgeMessage
                                        │
                                  SlackAdapter  ──── Web API ────►
                                        ◄──── Socket Mode ────────
```

Both directions run the same pipeline with the adapters swapped. Nothing in the
routing layer knows what a Slack payload looks like.

## 2. The four parts

**Adapters** ([`slack_adapter.py`](mmbridge/slack_adapter.py),
[`mattermost_adapter.py`](mmbridge/mattermost_adapter.py)) each own one
platform: its connection, its API calls, its user cache, and translation to and
from the neutral model. They are the only code that touches a platform payload.

**The model** ([`models.py`](mmbridge/models.py)) is `BridgeMessage` —
source, channel, message ID, author, text, thread root, files. Plus `BridgeUser`,
`BridgeFile`, `BridgeReaction`, and `MessageLink`. Files are carried as metadata
with a `source_ref`, not bytes: downloads happen only once a message has survived
the loop guards, so a message dropped as an echo never costs a file transfer.

**The store** ([`store.py`](mmbridge/store.py)) is the ID mapping table plus a
seen-events table. It is the only SQL in the project.

**The routing** ([`bridge.py`](mmbridge/bridge.py)) decides what crosses, how it
is attributed, where in a thread it lands, and — most importantly — what does
*not* cross.

## 3. The life of a message

Take someone typing in Slack.

**1. Delivery.** Slack pushes an envelope down the Socket Mode WebSocket. The
handler acknowledges it *immediately and unconditionally*, before any work —
Slack redelivers anything unacknowledged within three seconds, and an ack that
waits on a file download would guarantee duplicates.

**2. Is it a message at all?** Subtypes like `channel_join` and topic changes are
Slack narrating the channel rather than someone speaking; only the subtypes in
`MESSAGE_SUBTYPES` continue.

**3. Is the channel bridged?** `config.pair_for_slack(channel)` consults the
allowlist. Anything not explicitly paired is dropped here, regardless of where
the app happens to have been invited.

**4. Take the pair's lock.** One lock per bridged pair. Socket Mode dispatches on
several threads, and two messages mirrored concurrently could land out of order
or race each other's thread lookup. Per-pair rather than global, so a slow file
transfer in one channel cannot stall another.

**5. The three loop guards** (detail in §4).

**6. Normalise.** The adapter builds a `BridgeMessage`: author resolved through
the cached `users.info`, text as sent, thread root, file metadata.

**7. Translate.** Slack mrkdwn becomes Mattermost markdown; mentions are resolved
(§7); files are downloaded from Slack and uploaded to Mattermost, yielding
`file_ids`.

**8. Place it.** If the message is a thread reply, the link table is consulted for
where its root landed on the far side (§5).

**9. Post**, with the author's identity attached (§6).

**10. Record the link.** `slack_ts ↔ mattermost_post_id` goes into the table. This
last step is what makes every later feature possible — and it is what will
identify the echo that arrives a moment later.

The reverse direction is the same pipeline mirrored, with one asymmetry in how
files are attached (§8).

## 4. Loop prevention

The failure this system must never have: the bridge posts into Mattermost,
Mattermost reports a new post, the bridge mirrors it back to Slack, forever.

Three guards, in order of cost:

**Authorship.** `_is_own_slack_message` checks the event's `bot_id` against the
app's own, and the `user` against the bot user. On the Mattermost side,
`post.user_id == bot_user_id`. Cheap, and catches the common case — but not
sufficient on its own, because Slack reports bot posts inconsistently across
subtypes, and a Mattermost post made with webhook-style props looks much like an
ordinary one.

**The link table.** `store.link_by_slack(channel, ts)` — if this timestamp is
already one half of a recorded pair, the bridge created it. This is the decisive
guard: it is exact, it does not depend on how a platform chose to label
authorship, and it holds across restarts because the table is on disk.

**Event de-duplication.** `store.mark_seen(event_id)` — Socket Mode redelivers on
a slow ack and can replay on reconnect. This makes a redelivery harmless, which
is what lets step 1 acknowledge before doing any work.

The first two are why the mapping database must be on a persistent volume.

### Allowlist enforcement on the Slack side

The allowlist already guarantees nothing from an unlisted channel is forwarded:
`pair_for_slack` drops it at step 3. But if someone invites the app to another
channel, Slack still *streams* that channel's messages to the bridge, which then
discards them. For a bridge between two organisations, receiving data you have no
business receiving is itself worth avoiding.

So the bridge leaves. Three triggers, because Slack's documentation is ambiguous
about whether an app is always told of its own invitation:

- a `member_joined_channel` event naming the app itself;
- any message event from an unlisted channel — receiving one proves membership;
- a sweep of `users.conversations` at startup and every ten minutes, which
  catches invitations made while the bridge was down.

Before leaving, it posts a short notice once, so whoever invited it knows why it
disappeared. Enforcement runs *before* the direction check: it is a security
control, and must hold for a one-way deployment too.

Two safeguards. The sweep refuses to act if any configured channel cannot be
resolved — a typo in `MMBRIDGE_PAIRS` would otherwise make the bridge treat its
real channel as unauthorised and walk out of it on the next start. And leaving
needs `channels:manage` / `groups:write`, which also permit renaming, archiving
and removing members; if they are not granted, the bridge degrades to ignoring the
channel and announcing itself once, rather than failing.

## 5. Threading

A Slack thread is identified by the root message's `thread_ts`; a Mattermost
thread by the root post's `root_id`. Neither means anything to the other.

When a reply arrives, `_target_thread_root` looks up the *root* message in the
link table and takes the counterpart ID:

```
Slack reply, thread_ts = 1726482123.123456
        │
        └─► link table: slack_ts = 1726482123.123456
                        ↳ mattermost_post_id = xyz789
        │
        └─► post to Mattermost with root_id = xyz789
```

If the root was never bridged — it predates the bridge, or was posted while it
was down — the reply is posted as a **new root** rather than dropped. Losing the
thread shape beats losing the words.

Slack always collapses a thread behind "1 reply", with no setting to show replies
inline, while Mattermost users often read replies in the channel itself. So
replies travelling *into* Slack are posted with Slack's "Also send to #channel"
(`MMBRIDGE_SLACK_BROADCAST_REPLIES`, on by default): one message, shown both in
the thread and in the channel. Editing or deleting it acts on both. A broadcast
reply keeps the bot's `bot_id` and has one timestamp, so the existing loop guards
cover it unchanged.

## 6. Identity

This is the part with the most platform-specific scar tissue.

The bridge posts every message under the original author's name plus a
provenance marker: `Anna (via Slack)`. The marker is deliberate — between two
organisations, people should be able to tell that someone is on the other side.

**Into Mattermost**, identity is carried in post props: `from_webhook: "true"`,
`override_username`, `override_icon_url`. Three things must hold:

1. `EnablePostUsernameOverride` and `EnablePostIconOverride` on the server
2. `from_webhook` set — it is the switch that makes Mattermost read the override
3. **the account must not be a Bot Account**

That third one is the trap, and it cost real debugging time. Mattermost stamps
`from_bot: "true"` on every post a Bot Account makes and will not let it be
unset — passing `from_bot: "false"` explicitly comes back as `"true"`. The webapp
then renders the bot's own identity and ignores `override_username` entirely. So
a Bot Account *cannot* post under another person's name, no matter how the server
is configured. The bridge detects this at startup, warns, and falls back to
prefixing the author's name into the message text so attribution is never simply
lost.

**Into Slack**, identity is the `username` and `icon_url` arguments to
`chat.postMessage`, which require the `chat:write.customize` scope. If the scope
is missing, `post()` catches `missing_scope` and retries as the app rather than
dropping the message.

**The BOT badge cannot be removed.** In Mattermost, `from_webhook` is what enables
the name override *and* what triggers the badge — the same switch does both. Drop
it and the header reverts to the bridge account's own name. The only way to a
clean human header would be provisioning a real Mattermost account per Slack
person, which is impersonation and a user-management problem.

**Avatars are asymmetric.** Slack avatars are public CDN URLs, so Mattermost can
fetch them. Mattermost serves avatars only to logged-in clients — an anonymous
request gets 401 however public the server is — and Slack fetches icon URLs
anonymously. So Mattermost authors appear in Slack with `MMBRIDGE_SLACK_ICON_URL`
if one is set, and with the app's own icon otherwise.

## 7. Mentions

A mention is a link to an account, and in a two-organisation channel the people
on the far side have no account on this platform. Neither Slack nor Mattermost
can autocomplete them, or turn "@anna" into a notification by itself. So the
bridge resolves typed names against **the members of the bridged channel on the
other side** ([`mentions.py`](mmbridge/mentions.py)).

**Mattermost → Slack.** For each `@name`:

1. If it is a Mattermost username, it names a Mattermost person. It becomes a
   Slack mention only if that person also has a Slack account with the same
   email; otherwise it stays as typed.
2. Otherwise it is matched against the Slack channel's members, by handle,
   display name, full name, first name or last name, and becomes `<@U…>`.

**Slack → Mattermost.** A real Slack mention (`<@U…>`) becomes a Mattermost
mention when the person has a Mattermost account with the same email. A plain
`@Name`, typed for someone Slack does not know, is matched against the
Mattermost channel's members.

Two rules keep this safe:

- **Only members of the bridged channel are candidates.** Nobody else in either
  workspace can be notified through the bridge.
- **Only an unambiguous match counts.** An exact username wins outright;
  otherwise a name must fit exactly one member. With two Annas, "@anna" stays
  plain text: a missed notification is recoverable, a wrong one is not.

A name the bridge cannot resolve is rendered so that it **cannot** notify anyone:
an invisible word joiner goes between `@` and the name. Without it, a Slack
mention of "Anna" would arrive in Mattermost as `@Anna` and notify whoever there
holds the username `anna`.

Matching is case-insensitive but otherwise exact (`@Mueller` does not find
Müller). E-mail addresses and URLs such as `example.org/@anna` are never read as
mentions. Member lists are cached for five minutes and re-read on a miss after
one, so someone who just joined can be mentioned. `@here`, `@channel` and `@all`
map directly.

## 8. Files

Files are never proxied through a URL — they are downloaded from the source and
re-uploaded to the target, so neither side needs credentials for the other.

```
Slack file event
   └─► download url_private with the bot token   (files:read)
          └─► POST /api/v4/files                 → file_id
                 └─► create post with file_ids   → one message
```

**Slack→Mattermost is clean**: Mattermost accepts `file_ids` on the post itself,
so text and attachments arrive as a single message.

**Mattermost→Slack cannot be.** Slack strips the `username` override from any
message a file is attached to — verified directly: an identical post carrying a
file comes back with no `subtype: bot_message` and no `username`. Attaching by
editing a file permalink into an existing message doesn't work either; the file
simply isn't attached. So Slack offers a genuine either/or, exposed as
`MMBRIDGE_SLACK_FILE_STYLE`:

- **`thread`** (default) — the author's message carries the name and the icon;
  files upload as a threaded reply beneath it. Two messages, attribution intact.
- **`inline`** — one `files_upload_v2` call carrying both the file and the text,
  with the author named in the text. One message, header shows the app.

In `inline` mode the upload endpoints don't return the enclosing message, so the
adapter recovers its timestamp from the file's `shares` (falling back to
`files.info`). If it can't be found the file is still delivered; only the link
record is skipped, and that message won't support later edits or replies.

`files.upload` is not used anywhere — Slack retired it in 2024 in favour of the
asynchronous upload flow that `files_upload_v2` wraps.

## 9. Edits, deletes, reactions

All three work the same way: find the link, act on the counterpart.

```
Mattermost post_edited (post xyz789)
   └─► link table → slack_ts 1726482123.123456
          └─► chat.update
```

Edits and deletes propagate **only in the direction the message originally
travelled** — the handlers check `link.origin`. Without that, editing a mirrored
copy would push a change back onto the original, and two platforms could fight
over one message's content.

Reactions are applied by the bridge account, not the original reactor — neither
platform lets you react on someone else's behalf. Skin-tone modifiers are
stripped, since the far side may not know that emoji.

A delete removes the link row as well as the message; nothing should map to a
message that no longer exists.

## 10. Markdown

Slack mrkdwn and standard markdown are similar enough to be mistaken for each
other, which is the trap. Slack writes `*bold*`, `~strike~` and `<url|label>`;
Mattermost writes `**bold**`, `~~strike~~` and `[label](url)`. Passing text
through unconverted silently mangles emphasis and turns links into angle-bracket
noise.

[`mdconv.py`](mmbridge/mdconv.py) splits the text into code and non-code runs
first — a `*` inside a snippet is a character, not emphasis — and converts only
outside them. Replacements are written to sentinels rather than final characters,
so `**bold**` reduced toward Slack's `*bold*` cannot then be re-read as italic on
a later pass. Slack's HTML escaping (`&amp;` `&lt;` `&gt;`) is undone inbound and
reapplied outbound.

## 11. Connections

**Slack — Socket Mode.** The bridge dials *out*. No public hostname, no inbound
firewall rule, no certificate. Two tokens with separate jobs: `xapp-` opens the
WebSocket, and everything Slack delivers over it — including the text of new
messages in the app's channels — arrives on that connection; `xoxb-` is the Web
API credential for posting, history, users and files. Neither is harmless alone:
treat both as secrets.
Reconnection is handled by the SDK.

**Mattermost — WebSocket.** `wss://…/api/v4/websocket` with a bearer header, in a
daemon thread, reconnecting with exponential backoff capped at 60s. `ping_interval`
is set because a proxy silently dropping an idle socket is exactly how a bridge
stops receiving events without appearing to fail.

The REST client retries 429s using `Retry-After`.

### Token rotation

Optional, and on when `SLACK_CLIENT_ID` and `SLACK_CLIENT_SECRET` are both set.
Slack then issues bot tokens that expire after 12 hours, each paired with a
single-use refresh token. [`slack_tokens.py`](mmbridge/slack_tokens.py) runs the
cycle:

- **Bootstrap once.** The long-lived `SLACK_BOT_TOKEN` is exchanged via
  `oauth.v2.exchange`, or, for an app installed after opting in, a
  `SLACK_REFRESH_TOKEN` is spent. After that neither is needed.
- **Save before use.** Each new pair is written to the database in one
  transaction *before* the Slack client switches to it. A refresh token is
  single-use, so losing a freshly issued pair means reinstalling the app; saving
  first keeps that risk to a single network round trip.
- **Refresh ahead of expiry**, an hour early by default, retrying with backoff
  while the current token still works.
- **Adopt, don't repeat.** Before refreshing, the database is checked for a newer
  pair, for instance one written by `mmbridge refresh-token`. That process has
  already spent the refresh token this one holds, so reusing it would fail.

The token is swapped on the one shared `WebClient`, which Web API calls, file
downloads and the Socket Mode handler all use. Nothing else keeps a copy.

Tokens are encrypted at rest with `MMBRIDGE_TOKEN_KEY`, so a copied database or
backup is useless on its own. The client secret and refresh token go to Slack in
the POST body; the SDK's own wrappers would put them in the URL, and URLs reach
logs far more often than request bodies do. `check` reports rotation status but
never refreshes, since that would spend the refresh token of a running bridge.

The limit is worth stating plainly. The key, the client secret and the refresh
token all live on the host running the bridge. Rotation protects against a token
leaking *on its own* — in a log, a backup, a pasted message — by making it
worthless within 12 hours. It does not protect against someone who has taken
over that host.

## 12. Deliberately not bridged

Slash commands, workflows, Block Kit interactivity, custom emoji, presence,
channel membership, pins, and bookmarks. They have no faithful equivalent on the
other side, and a half-working button is worse than an absent one.

## 13. Why one replica

Each platform delivers an event once per connection. A second replica would
receive the same events on its own connections and mirror everything twice. The
bridge is therefore single-instance by design — which is also why SQLite is
sufficient, and why `store.py` is the only file that would need to change if that
ever stopped being true.
