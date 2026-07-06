from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click

from clawpm.output import OutputFormat, output_error, output_success
from clawpm.cli.base import main, get_format

# ============================================================================
# Judge subcommands — standalone judge primitives (CLAWP-044)
# ============================================================================


@main.group()
def judge() -> None:
    """Standalone judge primitives.

    Rubric pass/fail grading is wired through ``hook eval-stop``; this group
    exposes the judge primitives that are useful to call directly — currently
    the comparative-selection ``tournament``.
    """
    pass


@judge.command("tournament")
@click.option(
    "--rubric-file", "rubric_file",
    type=click.Path(exists=True, dir_okay=False), required=True,
    help="Path to the rubric / success-criteria file the candidates are judged against.",
)
@click.option(
    "--candidate", "candidate_files",
    type=click.Path(exists=True, dir_okay=False), multiple=True,
    help="Path to a candidate deliverable/transcript file. Repeat for each candidate. "
         "ORDER IS SEED ORDER — pass the strongest-prior candidate first; it wins ambiguous pairs.",
)
@click.option(
    "--label", "labels", multiple=True,
    help="Optional label per --candidate, in the same order. Defaults to each file's stem.",
)
@click.option(
    "--judge-cmd-override", "judge_cmd_override", default=None,
    help="Override the judge subprocess command (beats CLAWPM_JUDGE_CMD). Use a stub for offline testing.",
)
@click.pass_context
def judge_tournament(
    ctx: click.Context,
    rubric_file: str,
    candidate_files: tuple[str, ...],
    labels: tuple[str, ...],
    judge_cmd_override: str | None,
) -> None:
    """Pick the candidate that best satisfies the rubric via pairwise comparison.

    Comparative selection is more reliable than absolute scoring for choosing
    among attempts. The winner is SELECTED, not certified — feed it through
    ``hook eval-stop`` (optionally ``--confirm-close``) to verify it actually
    clears the rubric. Each pair is judged in both position orders to cancel
    position bias; ambiguous pairs keep the higher seed.
    """
    from clawpm.judges.tournament import Candidate, evaluate_tournament
    from clawpm.judges.stop_condition import make_judge_invoker

    fmt = get_format(ctx)
    if not candidate_files:
        output_error("no_candidates", "Provide at least one --candidate.", fmt=fmt)
        sys.exit(1)
    if labels and len(labels) != len(candidate_files):
        output_error(
            "label_mismatch",
            f"{len(labels)} --label(s) for {len(candidate_files)} --candidate(s); counts must match.",
            fmt=fmt,
        )
        sys.exit(1)

    # Robust reads (mirror `hook eval-stop`'s errors="replace"): a non-UTF-8 or
    # race-deleted file must surface as a structured error, not a raw traceback
    # that breaks the JSON contract callers rely on.
    try:
        rubric = Path(rubric_file).read_text(encoding="utf-8", errors="replace")
        candidates = []
        for i, cf in enumerate(candidate_files):
            path = Path(cf)
            label = labels[i] if labels else path.stem
            candidates.append(
                Candidate(
                    label=label,
                    transcript=path.read_text(encoding="utf-8", errors="replace"),
                )
            )
    except OSError as exc:
        output_error("read_failed", f"Failed to read an input file: {exc}", fmt=fmt)
        sys.exit(1)

    # An empty rubric means there is nothing to judge against — the model would
    # confidently pick a winner from noise. Refuse rather than emit a meaningless
    # selection that then seeds the close gate.
    if not rubric.strip():
        output_error(
            "empty_rubric",
            f"Rubric file {rubric_file!r} is empty; nothing to judge candidates against.",
            fmt=fmt,
        )
        sys.exit(1)

    invoker = make_judge_invoker(judge_cmd_override) if judge_cmd_override else None
    try:
        result = evaluate_tournament(rubric, candidates, invoker=invoker)
    except ValueError as exc:
        output_error("tournament_failed", str(exc), fmt=fmt)
        sys.exit(1)

    if fmt == OutputFormat.JSON:
        output_success("tournament complete", data=result.to_dict(), fmt=fmt)
    else:
        click.echo(f"Winner: {result.winner.label}")
        for c in result.comparisons:
            mark = "x" if c.degraded else ("=" if c.agreed else "~")
            click.echo(f"  {mark} {c.higher_seed} vs {c.lower_seed} -> {c.winner}")
        if result.is_degraded:
            click.echo(
                f"WARNING: {result.to_dict()['warning']}", err=True
            )
