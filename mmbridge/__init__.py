"""A bidirectional message bridge between one Slack channel and one Mattermost channel.

The bridge is deliberately a single process with a single replica: both Slack
Socket Mode and the Mattermost WebSocket deliver each event once per
connection, so a second replica would mirror every message twice.
"""

__version__ = "0.1.0"
