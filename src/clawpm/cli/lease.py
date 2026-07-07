from __future__ import annotations

import sys
from pathlib import Path

import click

from clawpm.models import Task
from clawpm.output import output_error, output_success
from clawpm.context import expand_task_id
from clawpm.cli.base import main, get_format, require_portfolio, require_project, _FALLBACK_POLICIES

# ============================================================================
# Lease commands (CLAWP-039) — crash-safe dispatch
# ============================================================================


@main.group("lease")
def lease_group() -> None:
    """Crash-safe dispatch leases: TTL + heartbeat + expiry → fallback.

    A dispatched subtask carries a lease; the holder heartbeats while alive
    (wired to the dispatch PostToolUse hook). If the holder goes silent past
    the TTL, a sweep (run by ``clawpm doctor`` and on the next ``tasks
    dispatch``) transitions the task per its fallback policy. No daemon —
    expiry is detected lazily on sweep.
    """
    pass


@lease_group.command("grant")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--task", "task_id", required=True, help="Task the lease is granted on")
@click.option("--ttl", "ttl", type=int, required=True, help="Lease TTL in seconds (no heartbeat within → expired)")
@click.option("--fallback-policy", "fallback_policy", type=click.Choice(_FALLBACK_POLICIES), default="requeue", show_default=True)
@click.option("--holder", "holder_id", default=None, help="Optional holder identifier (e.g. worktree path / session id)")
@click.option("--target-dir", "target_dir", default=None, help="Dispatch target dir (torn down on requeue fallback)")
@click.pass_context
def lease_grant(ctx, project_id, task_id, ttl, fallback_policy, holder_id, target_dir):
    """Grant a lease on a dispatched task."""
    from clawpm.leases import FallbackPolicy, grant_lease

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)
    # Store an ABSOLUTE target dir (Codex P2) so a later sweep from a different
    # CWD tears down the right path — matching what `tasks dispatch` does.
    if target_dir:
        target_dir = Path(target_dir).resolve().as_posix()
    try:
        grant_lease(
            config.portfolio_root, task_id, project_id, ttl_seconds=ttl,
            fallback_policy=FallbackPolicy(fallback_policy),
            holder_id=holder_id, target_dir=target_dir,
        )
    except ValueError as exc:
        output_error("lease_grant_failed", str(exc), fmt=fmt)
        sys.exit(1)
    output_success(
        f"Lease granted on {task_id} (ttl {ttl}s, fallback {fallback_policy})",
        data={"task_id": task_id, "ttl_seconds": ttl, "fallback_policy": fallback_policy},
        fmt=fmt,
    )


@lease_group.command("heartbeat")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--task", "task_id", required=True, help="Task whose lease to heartbeat")
@click.option("--holder", "holder_id", default=None)
@click.pass_context
def lease_heartbeat(ctx, project_id, task_id, holder_id):
    """Record a heartbeat — the holder is alive. (Called by the dispatch hook.)"""
    from clawpm.leases import heartbeat

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)
    heartbeat(config.portfolio_root, task_id, project_id, holder_id=holder_id)
    output_success(f"Heartbeat recorded for {task_id}", data={"task_id": task_id}, fmt=fmt)


@lease_group.command("release")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--task", "task_id", required=True, help="Task whose lease to release (clean completion)")
@click.pass_context
def lease_release(ctx, project_id, task_id):
    """Release a lease — clean completion, no fallback on later sweeps."""
    from clawpm.leases import release_lease

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)
    release_lease(config.portfolio_root, task_id, project_id)
    output_success(f"Lease released for {task_id}", data={"task_id": task_id}, fmt=fmt)


@lease_group.command("list")
@click.option("--project", "-p", "project_id", help="Filter to a project (default: all)")
@click.pass_context
def lease_list(ctx, project_id):
    """List active leases with their expiry + fallback policy."""
    from datetime import datetime, timezone
    from clawpm.leases import active_leases

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    now = datetime.now(timezone.utc)
    rows = []
    for l in active_leases(config.portfolio_root):
        if project_id and l.project_id != project_id:
            continue
        rows.append({
            "task_id": l.task_id,
            "project_id": l.project_id,
            "holder_id": l.holder_id,
            "ttl_seconds": l.ttl_seconds,
            "last_heartbeat_at": l.last_heartbeat_at.isoformat().replace("+00:00", "Z"),
            "expires_at": l.expires_at().isoformat().replace("+00:00", "Z"),
            "expired": l.is_expired(now),
            "fallback_policy": l.fallback_policy.value,
        })
    output_success(f"{len(rows)} active lease(s)", data={"leases": rows}, fmt=fmt)


@lease_group.command("sweep")
@click.option("--dry-run", "dry_run", is_flag=True, default=False, help="Report expired leases without applying fallback.")
@click.pass_context
def lease_sweep(ctx, dry_run):
    """Detect expired leases and apply their fallback (the no-daemon expiry check)."""
    from datetime import datetime, timezone
    from clawpm.leases import expired_leases, sweep

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    now = datetime.now(timezone.utc)
    if dry_run:
        rows = [
            {"task_id": l.task_id, "project_id": l.project_id,
             "fallback_policy": l.fallback_policy.value}
            for l in expired_leases(config.portfolio_root, now)
        ]
        output_success(f"{len(rows)} expired lease(s) (dry-run)", data={"expired": rows}, fmt=fmt)
        return
    actions = sweep(config, config.portfolio_root, now=now)
    output_success(f"Swept {len(actions)} expired lease(s)", data={"actions": actions}, fmt=fmt)
