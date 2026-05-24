"""Session resume briefing (CLAWP-025).

When an operator returns to a project after a break, ``clawpm context``
emits a JSON wall they then have to read. ``clawpm resume`` instead
gathers the same signals, hands them to the same subprocess judge used
by the Stop-hook (`claude --print`), and asks for a 2-paragraph human
briefing.

Output contract:
  Paragraph 1 — "where you are": branch, in-progress task, last commit's
                intent.
  Paragraph 2 — "what's next":   next likely step, recent surprises or
                blockers to be aware of.

The briefing is cached at ``<portfolio_root>/resume_cache_<project>.txt``
with a 60-second TTL — re-running within the TTL returns the cached text
(fast). ``--no-cache`` bypasses the cache.

When the judge subprocess is unavailable (``claude`` not on PATH or any
other ``FileNotFoundError`` / ``RuntimeError`` from the invoker) we
gracefully degrade to a structured signals summary plus a warning.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .discovery import get_project
from .models import PortfolioConfig, TaskState
from .tasks import list_tasks
from .worklog import tail_entries


CACHE_TTL_SECONDS = 60


# Resume uses its own env var so operators can point the briefing model
# at a different (cheaper, faster) endpoint than the stop-condition judge.
RESUME_CMD_ENV = "CLAWPM_RESUME_CMD"


@dataclass
class ResumeSignals:
    """Signals gathered for the briefing prompt."""

    project_id: str
    project_name: str
    branch: str | None = None
    uncommitted_count: int = 0
    uncommitted_sample: list[str] = field(default_factory=list)
    recent_commits: list[str] = field(default_factory=list)
    in_progress_task: dict[str, Any] | None = None
    next_task: dict[str, Any] | None = None
    recent_worklog: list[dict[str, Any]] = field(default_factory=list)
    recent_reflections: list[dict[str, Any]] = field(default_factory=list)
    # CLAWP-028: codegraph-rendered code orientation for the in-progress
    # task's scope or title. Empty when codegraph isn't installed or the
    # project isn't indexed — graceful degrade.
    codegraph_context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "branch": self.branch,
            "uncommitted_count": self.uncommitted_count,
            "uncommitted_sample": self.uncommitted_sample,
            "recent_commits": self.recent_commits,
            "in_progress_task": self.in_progress_task,
            "next_task": self.next_task,
            "recent_worklog": self.recent_worklog,
            "recent_reflections": self.recent_reflections,
            "codegraph_context": self.codegraph_context,
        }


def _git(cmd: list[str], cwd: Path) -> str | None:
    """Run a git command, return stdout stripped, or None on any failure."""
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=5,
            encoding="utf-8",
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def gather_signals(
    config: PortfolioConfig,
    project_id: str,
) -> ResumeSignals:
    """Collect git + task + worklog + reflection signals for one project."""
    proj = get_project(config, project_id)
    if proj is None:
        raise ValueError(f"Project not found: {project_id}")

    sig = ResumeSignals(
        project_id=project_id,
        project_name=proj.name,
    )

    # Git status
    repo = proj.repo_path
    if repo and repo.exists():
        sig.branch = _git(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
        porcelain = _git(["git", "status", "--porcelain"], repo)
        if porcelain:
            changes = [ln for ln in porcelain.splitlines() if ln]
            sig.uncommitted_count = len(changes)
            sig.uncommitted_sample = changes[:5]
        log_out = _git(["git", "log", "--oneline", "-5"], repo)
        if log_out:
            sig.recent_commits = [
                ln for ln in log_out.splitlines() if ln
            ]

    # In-progress task (first one if multiple — resume is per-project)
    in_progress = list_tasks(config, project_id, state_filter=TaskState.PROGRESS)
    if in_progress:
        t = in_progress[0]
        sig.in_progress_task = {
            "id": t.id,
            "title": t.title,
            "complexity": t.complexity.value if t.complexity else None,
            "priority": t.priority,
        }

        # Recent reflection events for this task (if any)
        ref_file = config.portfolio_root / "reflections" / f"{t.id}.jsonl"
        if ref_file.exists():
            try:
                lines = ref_file.read_text(encoding="utf-8").splitlines()
                # Tail last 5 reflection events
                tail = [ln for ln in lines if ln.strip()][-5:]
                for ln in tail:
                    try:
                        sig.recent_reflections.append(json.loads(ln))
                    except json.JSONDecodeError:
                        continue
            except OSError:
                pass

        # CLAWP-028: prepend codegraph orientation for the in-progress
        # task's scope/title. Best-effort — graceful degrade when
        # codegraph isn't installed or the project isn't indexed.
        if repo is not None:
            try:
                from .codegraph import context_brief
                # Use scope when available, fall back to title — codegraph
                # context takes free-text input.
                query = " ".join(t.scope) if t.scope else t.title
                sig.codegraph_context = context_brief(
                    query, repo, max_chars=1500
                )
            except Exception:
                sig.codegraph_context = ""
    else:
        # No active task — surface the next one so the briefing has somewhere to go
        from .tasks import get_next_task
        nxt = get_next_task(config, project_id)
        if nxt:
            sig.next_task = {
                "id": nxt.id,
                "title": nxt.title,
                "complexity": nxt.complexity.value if nxt.complexity else None,
                "priority": nxt.priority,
            }

    # Last 5 worklog entries
    recent = tail_entries(config, project=project_id, limit=5)
    sig.recent_worklog = [e.to_dict() for e in recent]

    return sig


PROMPT_TEMPLATE = """You are writing a brief two-paragraph session briefing for a developer who is returning to a project after a break. They want to reorient FAST — no preamble, no bullet lists, just two tight paragraphs.

Paragraph 1 — "where you are":
  - Name the git branch.
  - Name the in-progress task (if any) and what it's about.
  - Summarise the intent of the most recent commit (one sentence; quote the commit subject if useful).

Paragraph 2 — "what's next":
  - Name the next likely step (either the next subtask of the in-progress task, or the next pending task).
  - Flag any recent surprises, blockers, or reflection events the developer should know about before they touch code.

Hard rules:
  - Exactly TWO paragraphs separated by a blank line. No headings, no bullets, no markdown.
  - Address the developer in the second person ("you").
  - Keep it under 180 words total. Cut filler.
  - If a signal is missing (no in-progress task, no recent commits, etc.) say so plainly — don't invent.
  - When ``codegraph_context`` is present, weave the code-level orientation (key symbols, file paths) into paragraph 1 — but don't quote it verbatim, summarise.

=== SIGNALS (JSON) ===
{signals_json}
=== END SIGNALS ===

Return ONLY the two paragraphs. No JSON, no commentary."""


def build_resume_prompt(signals: ResumeSignals) -> str:
    """Compose the resume prompt from the gathered signals."""
    return PROMPT_TEMPLATE.format(
        signals_json=json.dumps(signals.to_dict(), indent=2, default=str),
    )


def _default_resume_invoker(prompt: str) -> str:
    """Default resume invoker: subprocess to `claude --print`.

    Honors ``CLAWPM_RESUME_CMD`` for override (parsed via shlex). Falls
    back to the same command shape as the Stop-hook judge but with no
    structured JSON requirement. Raises ``FileNotFoundError`` if the
    binary is missing — callers handle the graceful-degrade path.
    """
    # Mirror the stop_condition default so the same `claude` install is reused.
    from .judges.stop_condition import DEFAULT_JUDGE_CMD

    cmd_str = os.environ.get(RESUME_CMD_ENV)
    if cmd_str:
        cmd = shlex.split(cmd_str)
    else:
        cmd = list(DEFAULT_JUDGE_CMD)

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    except FileNotFoundError:
        # Re-raise so the caller can decide whether to degrade or fail.
        raise
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Resume judge timed out after {exc.timeout}s"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"Resume judge exited {result.returncode}: {result.stderr[:500]}"
        )
    return result.stdout.strip()


def _cache_path(config: PortfolioConfig, project_id: str) -> Path:
    return config.portfolio_root / f"resume_cache_{project_id}.txt"


def read_cache(
    config: PortfolioConfig,
    project_id: str,
    ttl_seconds: int = CACHE_TTL_SECONDS,
) -> str | None:
    """Return cached briefing if fresh (within TTL), else None."""
    p = _cache_path(config, project_id)
    if not p.exists():
        return None
    try:
        age = time.time() - p.stat().st_mtime
    except OSError:
        return None
    if age > ttl_seconds:
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def write_cache(
    config: PortfolioConfig,
    project_id: str,
    briefing: str,
) -> None:
    p = _cache_path(config, project_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(briefing, encoding="utf-8")
    except OSError:
        # Cache write is best-effort — never fail the resume call over it.
        pass


def format_degraded_summary(signals: ResumeSignals) -> str:
    """Build a structured plaintext summary when the judge is unavailable.

    This is the graceful-degrade fallback — every signal the prompt would
    have seen, laid out for a human to read. Better than a stack trace.
    """
    lines: list[str] = []
    lines.append(f"Project: {signals.project_name} ({signals.project_id})")
    if signals.branch:
        lines.append(f"Branch:  {signals.branch}")
    if signals.uncommitted_count:
        lines.append(
            f"Uncommitted: {signals.uncommitted_count} file(s) "
            f"({', '.join(signals.uncommitted_sample[:3])}"
            f"{'...' if signals.uncommitted_count > 3 else ''})"
        )
    if signals.in_progress_task:
        t = signals.in_progress_task
        lines.append(f"In progress: {t['id']} — {t['title']}")
    elif signals.next_task:
        t = signals.next_task
        lines.append(f"Next up: {t['id']} — {t['title']}")
    else:
        lines.append("No in-progress or next task surfaced.")

    if signals.recent_commits:
        lines.append("")
        lines.append("Recent commits:")
        for c in signals.recent_commits:
            lines.append(f"  - {c}")

    if signals.recent_worklog:
        lines.append("")
        lines.append("Recent work_log:")
        for e in signals.recent_worklog[:5]:
            ts = e.get("ts", "")
            action = e.get("action", "")
            summary = (e.get("summary") or "")[:80]
            lines.append(f"  - [{ts}] {action}: {summary}")

    if signals.recent_reflections:
        lines.append("")
        lines.append("Recent reflection events:")
        for r in signals.recent_reflections[:5]:
            ev = r.get("event", "?")
            reason = (
                r.get("verdict", {}).get("reason")
                or r.get("reason")
                or ""
            )[:80]
            lines.append(f"  - {ev}: {reason}")

    return "\n".join(lines)


def render_briefing(
    config: PortfolioConfig,
    project_id: str,
    *,
    use_cache: bool = True,
    invoker: Callable[[str], str] | None = None,
    ttl_seconds: int = CACHE_TTL_SECONDS,
) -> tuple[str, str]:
    """Render a 2-paragraph briefing for ``project_id``.

    Returns ``(briefing, status)`` where status is one of:
      - ``"ok"``         — fresh briefing from the judge
      - ``"cached"``     — returned from cache
      - ``"degraded"``   — judge unavailable; returned a signals summary

    ``invoker`` is injected for tests — defaults to :func:`_default_resume_invoker`.
    """
    if use_cache:
        cached = read_cache(config, project_id, ttl_seconds=ttl_seconds)
        if cached is not None:
            return cached, "cached"

    signals = gather_signals(config, project_id)

    invoker = invoker or _default_resume_invoker
    prompt = build_resume_prompt(signals)
    try:
        briefing = invoker(prompt).strip()
    except FileNotFoundError:
        return format_degraded_summary(signals), "degraded"
    except RuntimeError as exc:
        # Surface the runtime reason inline so the operator sees WHY it degraded
        msg = str(exc)
        if "not found" in msg.lower() or "no such file" in msg.lower():
            return format_degraded_summary(signals), "degraded"
        # Other runtime errors (timeout, non-zero exit) also degrade — better
        # than blowing up a reorientation command. Append a one-line cause.
        fallback = format_degraded_summary(signals)
        return f"{fallback}\n\n[resume judge failed: {msg[:200]}]", "degraded"

    if not briefing:
        # Empty judge output → degrade. Don't cache an empty string.
        return format_degraded_summary(signals), "degraded"

    write_cache(config, project_id, briefing)
    return briefing, "ok"
