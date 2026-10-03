# Getting the bridge into a partner's Slack workspace

There are two ways to do this. They differ in who owns the Slack app, and that
single choice determines the engineering work, the security conversation, and how
the partner revokes access later.

## The constraint both options live under

`SLACK_BOT_TOKEN` is one value, and a Slack bot token belongs to exactly one
workspace. **One running bridge serves one Slack workspace.** Everything below
follows from that.

---

# Option A — the app lives in *their* workspace

Their organisation creates and owns the Slack app. You receive its credentials and
run the bridge against them.

```
Partner workspace                      Your infrastructure
─────────────────                      ───────────────────
 Slack app (owned by them)
   ├─ xoxb-…  ──── handed over ─────►  SLACK_BOT_TOKEN
   └─ xapp-…  ──── handed over ─────►  SLACK_APP_TOKEN
                                              │
 #partner-project ◄──── Socket Mode ──────  mmbridge
                                              │
                                        Mattermost (yours)
```

## What you need from them

| | |
|---|---|
| A dedicated Slack channel | e.g. `#partner-collaboration`, and whether it is **public or private** — that changes which scopes are required |
| The channel ID | `C…`, from *View channel details* → bottom |
| An admin to create and install the app | ~10 minutes of clicking, following the runbook below |
| Four values | `xoxb-…`, `xapp-…`, and the app's client ID and client secret (for token rotation), sent through a password manager — never email or chat |
| The app invited to that channel | `/invite @Mattermost Bridge` |

## Two ways to run it

**A1 — they give you an account.** They create you a Slack account in their
workspace; you create the app from [`slack-app-manifest.yml`](slack-app-manifest.yml)
yourself and an admin approves the install. You can then iterate on scopes without
scheduling their time. A full member account is the safe choice — guest accounts
may be restricted from creating apps, so confirm before relying on it.

**A2 — they do it, you never touch their Slack.** Their admin follows the runbook
below and sends you the tokens. Requires no account for you at all, which is the
easiest version to get through a security review.

**A2 is the recommended default.** A1 is worth asking for only if you expect to
change scopes repeatedly during rollout.

## Pros

- **Nothing to build.** Works with the bridge exactly as it stands today.
- **No public endpoint.** No OAuth callback, no TLS certificate, no inbound
  firewall rule — the Socket Mode advantage stays intact.
- **The best governance story.** The app is *theirs*. Their admin can inspect its
  scopes, see what it has access to, and revoke it instantly without asking you.
  An app created inside the workspace also tends to clear internal security review
  faster than an external app from another company.

## Cons

- **The tokens move out of band.** They must be shared through a password manager
  and treated as secrets in your infrastructure.
- **Scope changes need them.** Adding a scope means their admin edits the app and
  reinstalls, then re-sends the bot token.
- **It does not scale.** A second partner means a second workspace, a second set
  of tokens, and a second bridge process.

---

# Option B — you distribute *your* app to them

You enable distribution on the app you already own and send their admin an
installation link. They click it, approve a consent screen, and Slack hands your
server a token for their workspace.

```
Your Slack app (owned by you)
      │
      │  shareable install URL
      ▼
Partner admin clicks → Slack consent screen → Allow
      │
      │  OAuth redirect with a code
      ▼
https://bridge.example.org/slack/oauth/callback   ← you must run this, publicly
      │
      │  exchange code for token
      ▼
 slack_installations table  (team_id → bot token)
```

## What you need from them

The same channel, channel ID, and `/invite` as Option A — but instead of creating
anything, their admin clicks one link and approves one consent screen. **No token
handoff at all.** From their side this is the lightest possible ask.

## What you need to build

This is the real cost, and it is not small:

1. **A public HTTPS endpoint.** Slack's distribution checklist requires a redirect
   URL over SSL. You would be standing up exactly the public surface Socket Mode
   was chosen to avoid — though only for installation, not for events.
2. **An OAuth callback service.** A small web app handling `/slack/install` and
   `/slack/oauth/callback`, exchanging the code via `oauth.v2.access`, and storing
   the result encrypted.
3. **An installations store.** `slack_installations`: `team_id`, `team_name`,
   `bot_token_encrypted`, `installed_by`, `installed_at`, `revoked_at`.
4. **Multi-workspace token routing.** The bridge currently builds one `WebClient`
   from one token. It would need a client per `team_id`, and channel pairs keyed
   by `(team_id, channel_id)` rather than channel alone. Every inbound event
   carries `team_id`; that becomes the routing key.
5. **Distribution activation.** *Manage Distribution* → complete the checklist →
   *Activate Public Distribution*.

Budget a few days, most of it in 2–4.

## Pros

- **The cleanest possible ask.** One link, one consent screen. Nothing to
  configure, no secrets to transmit.
- **You own the app.** Scope changes, manifest updates and fixes happen on your
  side; installed workspaces pick them up on reinstall.
- **It scales.** Partners B, C and D are rows in a table, not new deployments.

## Cons

- **Days of work to solve a problem you have once.**
- **You become custodian of their token.** That is a materially heavier data
  protection position than holding a token they minted and can pull at will.
- **Possibly a harder security review.** Some organisations scrutinise an external
  third-party app far more than one built inside their own workspace.
- **No Marketplace listing.** Slack does not allow Socket Mode apps in the public
  Marketplace. A shareable install URL still works, so this only matters if you
  ever wanted to be publicly listed — at which point Socket Mode would have to go.

---

# Recommendation

**With one partner, use Option A2.**

Option B is the right architecture for a product with many customers. With one
partner it would cost several days of work plus a permanently exposed HTTPS
endpoint, to replace a one-time handover of two secrets — and it would make your
data protection position worse, not better, by putting you in custody of their
credentials.

Revisit Option B when a third partner appears. The adapter boundary already
isolates the change: `SlackAdapter` is the only place that knows about tokens.

---

# What to ask them for

A summary you can lift directly into an email:

> We do **not** need administrative access to your Slack workspace, and we do not
> need anyone's password.
>
> What we need is:
>
> 1. A dedicated Slack channel for the collaboration, and confirmation of whether
>    it is public or private.
> 2. An administrator to create a Slack app in your workspace from a manifest we
>    provide — this takes about ten minutes and the manifest lists every
>    permission explicitly, so there is nothing hidden to review.
> 3. Four resulting values — two tokens plus the app's client ID and secret —
>    shared through a password manager. The bot token is exchanged for one that
>    expires every 12 hours the moment our service first starts, so the value
>    you send stops working almost immediately.
> 4. That app invited to the one channel.
>
> The app can only read and write in channels it has been invited to. Slack
> permissions and channel membership are separate mechanisms, so even with the
> permissions granted it is structurally unable to see any other channel. If it
> is invited anywhere else by mistake, it forwards nothing and leaves on its own —
> which is the one reason it asks for the channel-management permissions. You can
> revoke it at any time from your own app settings, without involving us.
>
> Messages, files, threads, edits and reactions in that one channel will be
> mirrored to our Mattermost channel and vice versa. Every mirrored message is
> labelled with the platform it came from, so nobody is misled about who they are
> talking to.

## Worth agreeing in writing

The bridge is an automated export path between two organisations: anything posted
in the bridged channel, files included, leaves for the other side. That is the
point of it, but under GDPR both organisations are controllers for what crosses,
and it should be a documented decision rather than an assumed one. Agree, at
minimum: which channel, what categories of data may be posted there, retention on
both sides, and who to contact to switch it off.

---

# The runbook to send their admin

Everything below can be forwarded as-is.

> ## Setting up the Mattermost bridge app
>
> **1. Create the app.** Go to <https://api.slack.com/apps> → **Create New App**
> → **From an app manifest** → choose your workspace → paste the manifest we
> supplied → **Next** → **Create**. The review screen lists every permission the
> app requests; nothing is granted that is not on it. Optionally, under
> *Basic Information* → *Display Information*, upload the icon we supplied as
> the app icon.
>
> **2. Create an app-level token.** *Basic Information* → *App-Level Tokens* →
> **Generate Token and Scopes**. Name it `socket-mode`, add the **`connections:write`**
> scope, generate it, and copy the value. It begins `xapp-`. This token opens the
> connection over which Slack delivers new messages from the app's channels. It
> cannot post or fetch history, but it is just as confidential as the bot token.
>
> **3. Install the app.** *Install App* → **Install to Workspace** → review →
> **Allow**. Copy the **Bot User OAuth Token**. It begins `xoxb-`. This step must
> come before step 4.
>
> **4. Switch on token rotation.** *OAuth & Permissions* → *Redirect URLs* → add
> `https://<our-Mattermost-domain>/slack/oauth/unused` → **Save URLs**. Slack
> requires an address on file before rotation can be enabled; in this setup
> nothing is ever sent to it. Then switch on token rotation on the same page.
> This setting is permanent.
>
> **5. Copy the app credentials.** *Basic Information* → *App Credentials*: the
> **Client ID** and the **Client Secret**.
>
> **6. Send us the four values** — app-level token, bot token, Client ID and
> Client Secret — through a password manager, not email or chat. When our service
> first starts, it exchanges the bot token for one that expires every 12 hours
> and renews automatically. The token you sent stops working at that moment.
>
> **7. Invite the app to the channel.** In the channel we agreed, type `/invite @`
> and select the app from the list. Until this step the app cannot see the channel
> at all, regardless of its permissions.
>
> That is everything. To revoke access at any time, remove the app from the channel,
> or delete it entirely from the same app settings page.
