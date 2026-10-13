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
@click.pass_context
def pr_verify_cmd(ctx, pr_number, expect_head, repo, remote):
    """Attest branch head == PR head, green CI on that SHA, and mergeable.

    Prints each fact plus the exact ``gh pr merge ... --match-head-commit <FULL sha>``
    line when all hold (it is only printed, never run). Exit 0 = all four facts
    hold; exit 1 = a fact failed; exit 2 = the facts could not be established
    (gh missing/unauthenticated, unparseable output, truncated CI listing, branch
    missing from the remote).
    """
    fmt = get_format(ctx)
    try:
        result = verify_pr(pr_number, expect_head=expect_head, repo=repo, remote=remote)
    except PrVerifyError as exc:
        output_error(exc.code, exc.message, exc.details or None, fmt=fmt)
        sys.exit(2)

    if fmt == OutputFormat.JSON:
        output_json(result)
    else:
        click.echo(f"PR #{pr_number} ({result['repo']}, {result['branch']}): {'OK' if result['ok'] else 'NOT OK'}")
        for name in ("head", "ci", "mergeable"):
            fact = result[name]
            click.echo(f"  {name}: {'ok' if fact['ok'] else 'FAIL'}")
            for reason in fact["reasons"]:
                click.echo(f"    - {reason}")
        if result["merge_command"]:
            click.echo(result["merge_command"])
    sys.exit(0 if result["ok"] else 1)
