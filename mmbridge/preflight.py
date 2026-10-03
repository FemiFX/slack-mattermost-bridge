"""`mmbridge check` and `mmbridge channels`.

Almost every failure in a bridge like this is a setup problem rather than a code
problem: a scope that was never granted, an app that was never invited to the
channel, a server setting that silently discards the identity override. These
checks surface all of them at once, before a single message is mirrored.
"""
from __future__ import annotations

import sys
import time
from typing import List

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .config import Config
from .slack_adapter import SlackAdapter

# Scopes the bridge needs with every feature on. Reactions and files drop out
# if those are switched off, which `check` accounts for.
REQUIRED_SLACK_SCOPES = {
    "chat:write": "post messages into Slack",
    "chat:write.customize": "post under each author's own name and avatar",
    "users:read": "resolve Slack user IDs to names",
}
OPTIONAL_SLACK_SCOPES = {
    "users:read.email": "match Slack and Mattermost accounts so @mentions ping the right person",
    "files:read": "download files shared in Slack",
    "files:write": "upload files that came from Mattermost",
    "reactions:read": "notice reactions added in Slack",
    "reactions:write": "mirror Mattermost reactions into Slack",
}

OK = "  ok    "
WARN = "  warn  "
FAIL = "  FAIL  "


class Report:
    def __init__(self) -> None:
        self.failures: List[str] = []
        self.warnings: List[str] = []

    def ok(self, message: str) -> None:
        print(f"{OK}{message}")

    def warn(self, message: str, hint: str = "") -> None:
        print(f"{WARN}{message}")
        if hint:
            print(f"          -> {hint}")
        self.warnings.append(message)

    def fail(self, message: str, hint: str = "") -> None:
        print(f"{FAIL}{message}")
        if hint:
            print(f"          -> {hint}")
        self.failures.append(message)


def _slack_scopes(client: WebClient) -> set:
    response = client.auth_test()
    granted = response.headers.get("x-oauth-scopes", "")
    if isinstance(granted, list):
        granted = granted[0] if granted else ""
    return {scope.strip() for scope in granted.split(",") if scope.strip()}


def run_preflight(config: Config) -> int:
    report = Report()
    print("\nSlack")
    print("-----")

    token = _slack_token_for_check(config, report)
    if token:
        _check_slack(config, report, token)

    print("\nMattermost")
    print("----------")
    from .mattermost_adapter import MattermostAdapter, MattermostError

    try:
        mattermost = MattermostAdapter(config)
        report.ok(f"token valid, connected to {config.mattermost_url}")
    except Exception as error:
        report.fail(
            f"could not authenticate: {error}",
            "Check MATTERMOST_URL and MATTERMOST_TOKEN (a bot access token).",
        )
        return _finish(report)

    if config.identity_override or mattermost.is_bot_account:
        if mattermost.is_bot_account:
            report.warn(
                "the Mattermost token belongs to a Bot Account, which cannot post "
                "under another author's name"
                + ("" if config.identity_override
                   else " (MMBRIDGE_IDENTITY_OVERRIDE is off, so this is not biting yet -- "
                        "but turning it on will not help until the token changes)"),
                "Mattermost stamps from_bot on every Bot Account post and then ignores "
                "override_username, so every bridged message would show the bot's name. "
                "The bridge detects this and prefixes the author's name into the message "
                "text instead. For real names in the Mattermost header, authenticate as "
                "an ordinary user account with a personal access token (System Console -> "
                "Integrations -> enable Personal Access Tokens) and add it to the channel.",
            )
        else:
            report.ok("Mattermost account is an ordinary user (author names will show)")

        try:
            client_config = mattermost._request("GET", "/config/client?format=old")
            for setting, label in (
                ("EnablePostUsernameOverride", "username"),
                ("EnablePostIconOverride", "icon"),
            ):
                if str(client_config.get(setting, "false")).lower() == "true":
                    report.ok(f"{setting} is on ({label} override will work)")
                else:
                    report.warn(
                        f"{setting} is off",
                        f"Mattermost will silently ignore the per-message {label}, so every "
                        "bridged message shows the bot's identity. Turn it on in System Console "
                        "-> Integrations -> Integration Management, or set "
                        "MMBRIDGE_IDENTITY_OVERRIDE=false to prefix the author's name instead.",
                    )
        except MattermostError as error:
            report.warn(f"could not read the server configuration: {error}")

    for pair in config.pairs:
        try:
            channel = mattermost._request("GET", f"/channels/{pair.mattermost_channel_id}")
        except MattermostError as error:
            report.fail(
                f"cannot read Mattermost channel {pair.mattermost_channel_id}: {error}",
                "Check the channel ID (mmbridge channels lists them).",
            )
            continue

        kind = {"O": "public", "P": "private", "D": "direct", "G": "group"}.get(
            channel.get("type"), channel.get("type")
        )
        try:
            mattermost._request(
                "GET", f"/channels/{pair.mattermost_channel_id}/members/{mattermost.bot_user_id}"
            )
            report.ok(f"~{channel.get('name')} ({kind}) — bot is a member")
        except MattermostError:
            report.fail(
                f"~{channel.get('name')} ({kind}) — bot is NOT a member",
                "Add the bot account to that channel; a token alone does not grant "
                "access to a private channel.",
            )

    print("\nStorage")
    print("-------")
    try:
        from .store import Store

        store = Store(config.db_path)
        store.mark_seen("preflight-probe")
        store.close()
        report.ok(f"mapping database writable at {config.db_path}")
    except Exception as error:
        report.fail(
            f"cannot open the mapping database at {config.db_path}",
            f"{error}",
        )

    return _finish(report)


def _check_slack(config: Config, report: Report, token: str) -> None:
    slack = WebClient(token=token)
    try:
        auth = slack.auth_test()
        report.ok(f"bot token valid: {auth.get('user')} in workspace {auth.get('team')}")
    except SlackApiError as error:
        report.fail(
            f"bot token rejected: {error.response.get('error')}",
            "Check SLACK_BOT_TOKEN. It must be the xoxb- token from OAuth & Permissions.",
        )
        return

    try:
        WebClient(token=config.slack_app_token).apps_connections_open(
            app_token=config.slack_app_token
        )
        report.ok("app-level token valid, Socket Mode can connect")
    except SlackApiError as error:
        report.fail(
            f"Socket Mode connection refused: {error.response.get('error')}",
            "Enable Socket Mode in the app settings and create an app-level token "
            "with the connections:write scope.",
        )

    granted = _slack_scopes(slack)
    if granted:
        for scope, why in REQUIRED_SLACK_SCOPES.items():
            if scope in granted:
                report.ok(f"scope {scope}")
            else:
                report.fail(f"missing scope {scope}", f"Needed to {why}. Add it and reinstall the app.")

        wanted = dict(OPTIONAL_SLACK_SCOPES)
        if not config.sync_files:
            wanted.pop("files:read", None)
            wanted.pop("files:write", None)
        if not config.sync_reactions:
            wanted.pop("reactions:read", None)
            wanted.pop("reactions:write", None)
        if config.slack_leave_unauthorised:
            wanted["channels:manage"] = "leave public channels it was invited to without authorisation"
            wanted["groups:write"] = "leave private channels it was invited to without authorisation"
        for scope, why in wanted.items():
            if scope in granted:
                report.ok(f"scope {scope}")
            else:
                report.warn(f"missing scope {scope}", f"Without it the bridge cannot {why}.")
    else:
        report.warn("could not read the granted scope list from Slack's response headers")

    for pair in config.pairs:
        try:
            channel = slack.conversations_info(channel=pair.slack_channel_id)["channel"]
        except SlackApiError as error:
            report.fail(
                f"cannot read Slack channel {pair.slack_channel_id}: {error.response.get('error')}",
                "Check the channel ID, and that the app has been invited to it.",
            )
            continue

        kind = "private" if channel.get("is_private") else "public"
        history_scope = "groups:history" if channel.get("is_private") else "channels:history"
        read_scope = "groups:read" if channel.get("is_private") else "channels:read"

        if channel.get("is_member"):
            report.ok(f"#{channel.get('name')} ({kind}) — app is a member")
        else:
            report.fail(
                f"#{channel.get('name')} ({kind}) — app is NOT a member",
                f"Run /invite @{auth.get('user')} in that Slack channel. "
                "Scopes alone do not grant access to a conversation.",
            )

        if granted:
            for scope in (history_scope, read_scope):
                if scope in granted:
                    report.ok(f"scope {scope} (needed for this {kind} channel)")
                else:
                    report.fail(
                        f"missing scope {scope}",
                        f"#{channel.get('name')} is {kind}, so this scope is required.",
                    )

    try:
        stray = [
            channel for channel in SlackAdapter.member_channels_with(slack)
            if not config.pair_for_slack(channel["id"])
        ]
        if stray:
            names = ", ".join(f"#{c.get('name')} ({c['id']})" for c in stray)
            report.warn(
                f"app is a member of channel(s) not in MMBRIDGE_PAIRS: {names}",
                "Nothing from them is forwarded. With MMBRIDGE_SLACK_LEAVE_UNAUTHORISED on, "
                "the bridge leaves them at startup.",
            )
        else:
            report.ok("app is a member of no channel outside the allowlist")
    except SlackApiError as error:
        report.warn(f"could not list the app's channel memberships: {error.response.get('error')}")



def _slack_token_for_check(config: Config, report: Report) -> str:
    """The Slack token to run the checks with, reporting rotation status on the way.

    Never refreshes: that would spend the refresh token a running bridge holds.
    """
    if not config.slack_token_rotation:
        return config.slack_bot_token

    from .slack_tokens import TokenError, describe, read_stored
    from .store import Store, StoreError

    try:
        store = Store(config.db_path)
    except StoreError as error:
        report.fail("cannot open the database holding the rotating token", str(error))
        return ""
    try:
        stored = read_stored(config, store)
    except TokenError as error:
        report.fail(
            f"rotating token: {error}",
            "Restore the original MMBRIDGE_TOKEN_KEY, or bootstrap again by supplying a "
            "fresh SLACK_BOT_TOKEN or SLACK_REFRESH_TOKEN.",
        )
        return ""
    finally:
        store.close()

    if stored is None:
        report.warn(
            "token rotation is on, but no rotating token is stored yet",
            "The first `run` bootstraps it: SLACK_BOT_TOKEN is exchanged, or "
            "SLACK_REFRESH_TOKEN is spent. Checking with SLACK_BOT_TOKEN meanwhile.",
        )
        return config.slack_bot_token

    access, expires_at = stored
    left = expires_at - time.time()
    if left <= 0:
        report.warn(
            "the stored rotating token has expired, so the Slack checks are skipped",
            "A running bridge refreshes ahead of expiry, so if it is running its refreshes "
            "are failing -- check its log. If it is stopped, `run` or `refresh-token` "
            "refreshes it.",
        )
        return ""
    report.ok(f"token rotation on; the stored token expires in {describe(left)}")

    legacy = config.slack_bot_token
    if legacy and not legacy.startswith("xoxe."):
        try:
            WebClient(token=legacy).auth_test()
            report.warn(
                "the original long-lived SLACK_BOT_TOKEN still works",
                "Rotation protects nothing while it does. Revoke it and remove it from "
                "the configuration.",
            )
        except SlackApiError:
            report.ok("the original long-lived SLACK_BOT_TOKEN no longer works")
    return access


def _finish(report: Report) -> int:
    print()
    if report.failures:
        print(f"{len(report.failures)} problem(s) must be fixed before the bridge will work:")
        for failure in report.failures:
            print(f"  - {failure}")
        return 1
    if report.warnings:
        print(f"Ready, with {len(report.warnings)} warning(s) — the bridge will run "
              "but some features are degraded.")
        return 0
    print("All checks passed.")
    return 0


def list_channels(config: Config) -> int:
    print("\nSlack channels the app can see")
    print("------------------------------")
    token = config.slack_bot_token
    if config.slack_token_rotation:
        from .slack_tokens import read_stored
        from .store import Store
        store = Store(config.db_path)
        try:
            stored = read_stored(config, store)
        finally:
            store.close()
        if stored and stored[1] > time.time():
            token = stored[0]
    slack = WebClient(token=token)
    try:
        cursor = None
        while True:
            page = slack.conversations_list(
                types="public_channel,private_channel", limit=200, cursor=cursor,
            )
            for channel in page["channels"]:
                member = "member" if channel.get("is_member") else "not a member"
                kind = "private" if channel.get("is_private") else "public"
                print(f"  {channel['id']:<12} #{channel['name']:<32} {kind:<8} {member}")
            cursor = page.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except SlackApiError as error:
        print(f"  could not list channels: {error.response.get('error')}", file=sys.stderr)

    print("\nMattermost channels the bot is in")
    print("---------------------------------")
    from .mattermost_adapter import MattermostAdapter, MattermostError

    try:
        mattermost = MattermostAdapter(config)
        teams = mattermost._request("GET", f"/users/{mattermost.bot_user_id}/teams") or []
        for team in teams:
            print(f"  team {team.get('display_name')} ({team.get('name')})")
            channels = mattermost._request(
                "GET", f"/users/{mattermost.bot_user_id}/teams/{team['id']}/channels"
            ) or []
            for channel in channels:
                kind = {"O": "public", "P": "private", "D": "direct", "G": "group"}.get(
                    channel.get("type"), channel.get("type")
                )
                if kind in {"direct", "group"}:
                    continue
                print(f"    {channel['id']:<28} ~{channel.get('name'):<32} {kind}")
    except MattermostError as error:
        print(f"  could not list channels: {error}", file=sys.stderr)

    print("\nPair them with MMBRIDGE_PAIRS=SLACK_CHANNEL_ID:MATTERMOST_CHANNEL_ID")
    return 0
