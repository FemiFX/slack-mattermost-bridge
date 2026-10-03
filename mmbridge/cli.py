"""Command line entry point for mmbridge."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from . import __version__
from .config import DEFAULT_ENV_FILE, Config, ConfigError, load_env_file
from .store import StoreError


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # These libraries log every frame at DEBUG, which buries the bridge's own output.
    for noisy in ("websocket", "slack_sdk", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.INFO)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mmbridge",
        description="Bidirectional message bridge between Slack and Mattermost.",
    )
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                        help=f"KEY=VALUE file to seed the environment from (default: {DEFAULT_ENV_FILE})")
    parser.add_argument("--version", action="version", version=f"mmbridge {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Connect to both platforms and bridge messages")
    sub.add_parser("check", help="Verify credentials, channel access and server settings, then exit")
    sub.add_parser("channels", help="List channels the bridge can see on both platforms")
    sub.add_parser("genkey", help="Print a new MMBRIDGE_TOKEN_KEY for encrypting the stored Slack token")
    sub.add_parser("refresh-token", help="Refresh the rotating Slack token now (for testing rotation)")
    return parser


def cmd_run(config: Config) -> int:
    from .bridge import Bridge

    bridge = Bridge(config)
    stop = threading.Event()

    def shutdown(signum, _frame):
        logging.getLogger("mmbridge").info("Received signal %s, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    bridge.start()
    try:
        stop.wait()
    finally:
        bridge.stop()
    return 0


def cmd_check(config: Config) -> int:
    from .preflight import run_preflight

    return run_preflight(config)


def cmd_channels(config: Config) -> int:
    from .preflight import list_channels

    return list_channels(config)


def cmd_refresh_token(config: Config) -> int:
    from .slack_tokens import SlackTokenManager, describe
    from .store import Store

    if not config.slack_token_rotation:
        print("Token rotation is off (SLACK_CLIENT_ID / SLACK_CLIENT_SECRET are not set).",
              file=sys.stderr)
        return 2
    store = Store(config.db_path)
    try:
        manager = SlackTokenManager(config, store)
        manager.ensure()
        manager.refresh(force=True)
        print(f"Refreshed. The new Slack token expires in {describe(manager.seconds_left)}.")
        print("A running bridge picks up the new token from the database within 5 minutes.")
    finally:
        store.close()
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "genkey":
        # Needs no configuration -- it is how part of the configuration is made.
        from .slack_tokens import generate_key
        print(generate_key())
        return 0

    load_env_file(args.env_file)
    try:
        config = Config.from_env()
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    _configure_logging(config.log_level)

    handlers = {"run": cmd_run, "check": cmd_check, "channels": cmd_channels,
                "refresh-token": cmd_refresh_token}
    from .slack_tokens import TokenError
    try:
        return handlers[args.command](config)
    except StoreError as error:
        # A setup problem, not a crash -- print the guidance, not a traceback.
        print(f"Storage error: {error}", file=sys.stderr)
        return 2
    except TokenError as error:
        print(f"Slack token error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
