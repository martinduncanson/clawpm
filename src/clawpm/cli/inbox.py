from __future__ import annotations

import sys

import click

from clawpm.models import Task
from clawpm.output import output_error, output_json
from clawpm.inbox import TaskRequestError, validate_agent_id, build_task_request_payload, materialize_task_requests, ack_messages as _inbox_ack, get_thread as _inbox_thread, read_inbox as _inbox_read, send_message as _inbox_send
from clawpm.cli.base import main, get_format, require_portfolio, require_project

# ============================================================================
# Inbox commands
# ============================================================================


def _require_agent_ids(fmt, *agent_ids: str) -> None:
    """Exit 1 with a clean error if any agent id is not a safe inbox filename."""
    for agent_id in agent_ids:
        try:
            validate_agent_id(agent_id)
        except ValueError as exc:
            output_error("bad_agent_id", str(exc), fmt=fmt)
            sys.exit(1)


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
@click.option("--task-request", "task_request", is_flag=True, default=False,
              help="Attach a structured task_request payload (CLAWP-101); the recipient turns it into a task with `inbox materialize`. Requires --title.")
@click.option("--title", "req_title", default=None, help="task_request: task title")
@click.option("--priority", "req_priority", type=int, default=None, help="task_request: priority 1-10")
@click.option("--complexity", "req_complexity", type=click.Choice(["s", "m", "l", "xl"]), default=None, help="task_request: complexity")
@click.option("--body", "req_body", default=None, help="task_request: task body/description")
@click.option("--depends", "req_depends", multiple=True, help="task_request: dependency task id (repeatable)")
@click.option("--scope", "req_scope", multiple=True, help="task_request: file glob claimed by the task (repeatable)")
@click.option("--tag", "req_tags", multiple=True, help="task_request: workstream tag (repeatable)")
@click.option("--success-criteria", "req_success_criteria", multiple=True, help="task_request: measurable success criterion (repeatable)")
@click.option("--predict-duration", "req_predict_duration", default=None, help="task_request: predicted duration (e.g. 2h)")
@click.option("--predict-complexity", "req_predict_complexity", type=click.Choice(["s", "m", "l", "xl"]), default=None, help="task_request: predicted complexity")
@click.option("--predict-approach", "req_predict_approach", default=None, help="task_request: predicted approach")
@click.option("--confidence", "req_confidence", type=int, default=None, help="task_request: confidence 1-5")
@click.option("--pre-mortem", "req_pre_mortem", default=None, help="task_request: pre-mortem")
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
    task_request: bool,
    req_title: str | None,
    req_priority: int | None,
    req_complexity: str | None,
    req_body: str | None,
    req_depends: tuple[str, ...],
    req_scope: tuple[str, ...],
    req_tags: tuple[str, ...],
    req_success_criteria: tuple[str, ...],
    req_predict_duration: str | None,
    req_predict_complexity: str | None,
    req_predict_approach: str | None,
    req_confidence: int | None,
    req_pre_mortem: str | None,
) -> None:
    """Send a message to an agent's inbox.

    With --task-request, also attaches a structured payload the recipient can
    turn into a task via `clawpm inbox materialize`. The sender never writes
    into any project's .project/ tree.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    _require_agent_ids(fmt, to_agent, from_agent)

    payload = None
    if task_request:
        try:
            payload = build_task_request_payload(
                title=req_title, priority=req_priority, complexity=req_complexity,
                description=req_body, depends=req_depends, scope=req_scope,
                tags=req_tags, success_criteria=req_success_criteria,
                predict_duration=req_predict_duration,
                predict_complexity=req_predict_complexity,
                predict_approach=req_predict_approach, confidence=req_confidence,
                pre_mortem=req_pre_mortem,
            )
        except TaskRequestError as exc:
            output_error("bad_task_request", str(exc), fmt=fmt)
            sys.exit(1)

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
        payload=payload,
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
    _require_agent_ids(fmt, agent_id)

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
    _require_agent_ids(fmt, acked_by)

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


@inbox_group.command("materialize")
@click.option("--agent", "--agent-id", "agent_id", default="main", help="Whose inbox to drain (default: main)")
@click.option("--project", "-p", "project_id", default=None, help="Fallback project for requests that name none (default: auto-detected)")
@click.option("--dry-run", is_flag=True, default=False, help="Preview the tasks that would be created; writes nothing")
@click.pass_context
def inbox_materialize(ctx: click.Context, agent_id: str, project_id: str | None, dry_run: bool) -> None:
    """Turn pending task_request messages into real tasks (recipient-side).

    Creates each task via the normal add path in THIS agent's context, replies to
    the sender (in_reply_to) with the new task id, and acks the request.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    _require_agent_ids(fmt, agent_id)
    default_project, _ = require_project(ctx, project_id, required=False, auto_init=False)
    result = materialize_task_requests(config, agent_id, default_project=default_project, dry_run=dry_run)
    output_json(result)
