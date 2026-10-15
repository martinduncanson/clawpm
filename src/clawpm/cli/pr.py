from __future__ import annotations

import sys

import click

from clawpm.output import OutputFormat, output_error, output_json
from clawpm.pr_verify import PrVerifyError, verify_pr
from clawpm.cli.base import main, get_format

# ============================================================================
# PR commands (CLAWP-147-001) -- read-only handoff checks
# ============================================================================


@main.group("pr")
def pr_group() -> None:
    """Read-only pull-request checks. Nothing here merges or writes to the remote."""
    pass


@pr_group.command("verify")
@click.argument("pr_number", type=int)
@click.option("--expect-head", "expect_head", default=None, help="Full 40-char SHA the worker reported; must equal the PR head")
@click.option("--repo", "repo", default=None, help="owner/name (default: derived from --remote)")
@click.option("--remote", "remote", default="fork", show_default=True, help="Git remote holding the PR branch")
@click.option("--task", "task_id", default=None, help="Record a merged-PR attestation on this task (requires the PR to be MERGED)")
@click.option("--project", "-p", "project_id", default=None, help="Project ID for --task (auto-detected if not specified)")
@click.pass_context
def pr_verify_cmd(ctx, pr_number, expect_head, repo, remote, task_id, project_id):
    """Attest branch head == PR head, green CI on that SHA, and mergeable.

    Prints each fact plus the exact ``gh pr merge ... --match-head-commit <FULL sha>``
    line when all hold (it is only printed, never run). Exit 0 = all four facts
    hold; exit 1 = a fact failed; exit 2 = the facts could not be established
    (gh missing/unauthenticated, unparseable output, truncated CI listing, branch
    missing from the remote).

    With ``--task ID`` the PR must be MERGED (the ``mergeable`` fact is replaced
    by a ``merged`` fact carrying the merge commit) and, only when every fact
    holds, a ``pr_verification`` record is written into the task's frontmatter
    (one line, replaced on re-run). Any refusal leaves the task file untouched.
    The PR branch must still exist on the remote. ``tasks land`` does not read
    the record.
    """
    fmt = get_format(ctx)
    config = None
    if task_id is not None:
        from clawpm.cli.base import require_portfolio, require_project
        from clawpm.context import expand_task_id

        from clawpm.discovery import _read_project_id_from_settings, get_project_dir
        from clawpm.pr_record import PrRecordError, validate_task_ref
        from clawpm.pr_verify import _resolve_repo
        from clawpm.sessions import Scope

        config = require_portfolio(ctx)
        project_id, _ = require_project(ctx, project_id, auto_init=False)  # a refusal must write nothing
        task_id = expand_task_id(task_id, project_id)
        try:
            validate_task_ref(task_id)
        except PrRecordError as exc:
            output_error(exc.code, exc.message, {"task": task_id}, fmt=fmt)
            sys.exit(1)
        # The store we are about to write must itself declare the requested project id.
        project_dir = get_project_dir(config, project_id)
        declared = _read_project_id_from_settings(project_dir / "settings.toml") if project_dir is not None else None
        if declared != project_id:
            output_error(
                "project_mismatch",
                f"resolved store {str(project_dir)!r} declares project {declared!r}, not {project_id!r}; "
                "refusing to write another project's task",
                {"project": project_id, "declared": declared},
                fmt=fmt,
            )
            sys.exit(1)
        scope = Scope(pinned_project_dir=project_dir)
        # An explicit --repo must be the remote's own repo: forks share branch names and shas.
        if repo:
            try:
                actual = _resolve_repo(None, remote)
            except PrVerifyError as exc:
                output_error(exc.code, exc.message, exc.details or None, fmt=fmt)
                sys.exit(2)
            if actual.lower() != repo.lower():
                output_error(
                    "repo_remote_mismatch",
                    f"--repo {repo} does not match remote '{remote}' ({actual}); "
                    "refusing to attest a PR from a different repository",
                    {"repo": repo, "remote_repo": actual},
                    fmt=fmt,
                )
                sys.exit(1)
    try:
        result = verify_pr(
            pr_number, expect_head=expect_head, repo=repo, remote=remote,
            require_merged=task_id is not None,
        )
    except PrVerifyError as exc:
        output_error(exc.code, exc.message, exc.details or None, fmt=fmt)
        sys.exit(2)

    if task_id is not None and result["ok"]:
        from datetime import datetime, timezone

        from clawpm.pr_record import record_pr_verification

        record = {
            "pr": pr_number,
            "head": result["head"]["pr_head"],
            "merge_commit": result["merge_commit"],
            "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "remote": remote,
            "repo": result["repo"],
        }
        try:
            path = record_pr_verification(config, project_id, task_id, record, scope=scope)
        except PrRecordError as exc:
            output_error(exc.code, exc.message, {"task": task_id, "project": project_id}, fmt=fmt)
            sys.exit(1)
        result = {**result, "task": task_id, "pr_verification": record, "task_file": str(path)}

    if fmt == OutputFormat.JSON:
        output_json(result)
    else:
        click.echo(f"PR #{pr_number} ({result['repo']}, {result['branch']}): {'OK' if result['ok'] else 'NOT OK'}")
        for name in ("head", "ci", "merged" if "merged" in result else "mergeable"):
            fact = result[name]
            click.echo(f"  {name}: {'ok' if fact['ok'] else 'FAIL'}")
            for reason in fact["reasons"]:
                click.echo(f"    - {reason}")
        if result["merge_command"]:
            click.echo(result["merge_command"])
        if "pr_verification" in result:
            click.echo(f"  recorded pr_verification on {result['task']} ({result['task_file']})")
    sys.exit(0 if result["ok"] else 1)
