from __future__ import annotations

import sys

import click

from clawpm.models import Task
from clawpm.output import output_error, output_json
from clawpm.inbox import ack_messages as _inbox_ack, get_thread as _inbox_thread, read_inbox as _inbox_read, send_message as _inbox_send
from clawpm.cli.base import main, get_format, require_portfolio

# ============================================================================
# Inbox commands
# ============================================================================


@main.group("inbox")
def inbox_group() -> None:
    """Inter-agent messaging. Filesystem-first, append-only, no daemons."""
    pass


@inbox_group.command("send")
@click.option("--to", "to_agent", required=True, help="Recipient agent ID")
@click.option("--message", "message", default=None, help="Message text (or '-' to read from stdin)")
@click.option("--stdin", "read_stdin", is_flag=True, default=False, help="Read message from stdin")
@click.option("--from", "from_agent", default="main", help="Sender agent ID (default: main)")
@click.option("--in-reply-to", "in_reply_to", default=None, help="msg_id this message replies to")
@click.option("--project", "project_id", default=None, help="Project context for the message")
@click.option("--task", "task_id", default=None, help="Task context for the message")
@click.pass_context
def inbox_send(
    ctx: click.Context,
    to_agent: str,
    message: str | None,
    read_stdin: bool,
    from_agent: str,
    in_reply_to: str | None,
    project_id: str | None,
    task_id: str | None,
) -> None:
    """Send a message to an agent's inbox."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    if message == "-" or read_stdin:
        message = sys.stdin.read()
    if not message:
        output_error("missing_message", "Provide --message text or pass --stdin / --message -", fmt=fmt)
        sys.exit(1)

    event = _inbox_send(
        portfolio_root=config.portfolio_root,
        to=to_agent,
        message=message,
        from_agent=from_agent,
        in_reply_to=in_reply_to,
        project=project_id,
        task=task_id,
    )
    output_json({"msg_id": event["msg_id"], "to": event["to"], "ts": event["ts"]})


@inbox_group.command("read")
@click.option("--agent", "agent_id", required=True, help="Whose inbox to read")
@click.option("--unacked", "filter_mode", flag_value="unacked", default=True, help="Show only unacked messages (default)")
@click.option("--all", "filter_mode", flag_value="all", help="Show all messages including acked")
@click.option("--since", "since", default=None, help="Filter messages at or after this date/timestamp (YYYY-MM-DD or ISO)")
@click.option("--from", "from_filter", default=None, help="Filter messages from this sender")
@click.pass_context
def inbox_read(
    ctx: click.Context,
    agent_id: str,
    filter_mode: str,
    since: str | None,
    from_filter: str | None,
) -> None:
    """Read messages from an agent's inbox."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    unacked_only = filter_mode == "unacked"
    messages = _inbox_read(
        portfolio_root=config.portfolio_root,
        agent_id=agent_id,
        unacked_only=unacked_only,
        since=since,
        from_filter=from_filter,
    )
    output_json(messages)


@inbox_group.command("ack")
@click.argument("msg_ids", nargs=-1, required=True)
@click.option("--agent", "acked_by", default="main", help="Agent performing the ack (default: main)")
@click.pass_context
def inbox_ack(ctx: click.Context, msg_ids: tuple[str, ...], acked_by: str) -> None:
    """Acknowledge one or more messages (marks them as read)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    result = _inbox_ack(
        portfolio_root=config.portfolio_root,
        msg_ids=list(msg_ids),
        acked_by=acked_by,
    )
    output_json(result)


@inbox_group.command("thread")
@click.argument("msg_id")
@click.pass_context
def inbox_thread(ctx: click.Context, msg_id: str) -> None:
    """Show the full thread containing a message, sorted by timestamp."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    thread = _inbox_thread(portfolio_root=config.portfolio_root, msg_id=msg_id)
    output_json(thread)
