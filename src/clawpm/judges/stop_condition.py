"""Stop-condition evaluator — the killer feature.

Adapts the Anthropic Claude Code 2.1.143 ``/goal`` Stop-hook condition
evaluator to clawpm. A small LLM judge reads a subagent's transcript +
the task's rubric, then returns the exact JSON shape used by the official
evaluator (reverse-engineered by Piebald-AI):

- ``{"ok": true,  "reason": "<quoted evidence>"}``
- ``{"ok": false, "reason": "<what is missing>"}``
- ``{"ok": false, "impossible": true, "reason": "<why unachievable>"}``

When wired as a Claude Code ``Stop`` hook, the subagent literally cannot
terminate until the rubric is satisfied or impossibility is independently
confirmed. Adopts the Piebald doctrine verbatim:

  > The assistant claiming the goal is impossible is evidence, not proof;
  > independently confirm the condition is genuinely unachievable rather
  > than deferring to the assistant's self-assessment.

This module is also the local alternative to Anthropic Managed Agents'
paid ``user.define_outcome`` grader. The same rubric content drives both;
clawpm callers stay subscription-only by default.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Default judge — subprocess invocation of the user's installed `claude` CLI in
# print mode. Override via env CLAWPM_JUDGE_CMD when you want a different
# model (e.g. set to `claude --model claude-haiku-4-5 -p`) or a stub for tests.
DEFAULT_JUDGE_CMD = ["claude", "--print", "--model", "claude-haiku-4-5"]


@dataclass
class JudgeVerdict:
    """Parsed judge output, in the official Anthropic Stop-hook shape."""

    ok: bool
    reason: str
    impossible: bool = False

    def to_dict(self) -> dict:
        d: dict = {"ok": self.ok, "reason": self.reason}
        if self.impossible:
            d["impossible"] = True
        return d

    @classmethod
    def parse(cls, raw: str) -> "JudgeVerdict":
        """Parse judge output. Be defensive — LLMs return malformed JSON sometimes.

        Strategy: find the first ``{`` and parse from there. On any parse
        failure, return ``{ok: False, reason: "judge output unparseable: ..."}``
        — which keeps the subagent running rather than letting a parse glitch
        be treated as success.
        """
        stripped = raw.strip()
        # Strip common LLM wrappings: ```json ... ```
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            # drop fence open + fence close
            inner = []
            in_fence = False
            for ln in lines:
                if ln.startswith("```"):
                    in_fence = not in_fence
                    continue
                inner.append(ln)
            stripped = "\n".join(inner).strip()

        # Find the first { and last } — judge sometimes prepends commentary.
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return cls(
                ok=False,
                reason=f"judge output unparseable (no JSON object found): {raw[:200]}",
            )
        candidate = stripped[start : end + 1]
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            return cls(
                ok=False,
                reason=f"judge output JSON parse error: {exc}; raw: {raw[:200]}",
            )
        if not isinstance(data, dict):
            return cls(
                ok=False, reason=f"judge returned non-object: {data!r}"
            )
        ok = bool(data.get("ok"))
        reason = data.get("reason", "")
        if not isinstance(reason, str):
            reason = str(reason)
        impossible = bool(data.get("impossible", False))
        if impossible and ok:
            # Self-contradictory — treat as not-ok to be safe.
            return cls(
                ok=False,
                impossible=True,
                reason=f"judge returned ok=true AND impossible=true (contradiction); raw reason: {reason}",
            )
        return cls(ok=ok, reason=reason, impossible=impossible)


JudgeInvoker = Callable[[str], str]
"""A judge invoker takes the prompt string and returns the raw text response."""


def _default_judge_invoker(prompt: str) -> str:
    """Default judge: subprocess to the user's `claude --print` CLI.

    Honors ``CLAWPM_JUDGE_CMD`` when set — split via shlex so users can pass
    flags like ``"claude -p --model claude-haiku-4-5"``. The prompt is sent on
    stdin so even long rubrics + transcripts don't hit shell argument limits.
    """
    cmd_str = os.environ.get("CLAWPM_JUDGE_CMD")
    if cmd_str:
        cmd = shlex.split(cmd_str)
    else:
        cmd = list(DEFAULT_JUDGE_CMD)

    # Some judge CLIs read the prompt from a positional argument when stdin is
    # empty, others from stdin. claude --print accepts stdin, so this is the
    # path of least surprise. Timeout matches a generous human-perceptible
    # ceiling — Haiku usually returns in <5s; 30s is a wide safety margin.
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    except FileNotFoundError as exc:
        # `claude` CLI not on PATH — be loud about it so the operator knows
        # to install Claude Code or override CLAWPM_JUDGE_CMD.
        raise RuntimeError(
            f"Judge command not found: {cmd[0]!r}. Install Claude Code or "
            f"set CLAWPM_JUDGE_CMD to an alternative judge. Error: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Judge timed out after {exc.timeout}s; rubric or transcript may "
            "be too large for a single call"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"Judge exited {result.returncode}: {result.stderr[:500]}"
        )
    return result.stdout


JUDGE_PROMPT_TEMPLATE = """You are evaluating a stop-condition hook in Claude Code. Read the transcript carefully, then judge whether the rubric below is satisfied.

Your response must be a JSON object with one of these shapes:
- {{"ok": true, "reason": "<quote evidence from the transcript that satisfies the rubric>"}}
- {{"ok": false, "reason": "<quote what is missing or what blocks the condition>"}}
- {{"ok": false, "impossible": true, "reason": "<explain why the condition can never be satisfied>"}}

Always include a "reason" field, quoting specific text from the transcript whenever possible. If the transcript does not contain clear evidence that the rubric is satisfied, return {{"ok": false, "reason": "insufficient evidence in transcript"}}.

Only use {{"ok": false, "impossible": true}} when the rubric is genuinely unachievable in this session — for example: the rubric is self-contradictory, it depends on a resource or capability that is unavailable, or the assistant has explicitly tried, exhausted reasonable approaches, and stated it cannot be done. Apply your own judgment when deciding this — the assistant claiming the goal is impossible is evidence, not proof; independently confirm the condition is genuinely unachievable rather than deferring to the assistant's self-assessment. Do not use it just because the goal has not been reached yet or because progress is slow. When in doubt, return {{"ok": false}} without "impossible".

=== RUBRIC ===
{rubric}

=== TRANSCRIPT ===
{transcript}

=== END ===

Return ONLY the JSON object. No prose before or after."""


def build_judge_prompt(rubric: str, transcript: str) -> str:
    """Compose the judge prompt from rubric + transcript."""
    return JUDGE_PROMPT_TEMPLATE.format(
        rubric=rubric.strip(),
        transcript=transcript.strip(),
    )


def evaluate_stop_condition(
    rubric: str,
    transcript: str,
    invoker: JudgeInvoker | None = None,
) -> JudgeVerdict:
    """Evaluate whether the transcript satisfies the rubric.

    ``invoker`` defaults to subprocess ``claude --print``; tests pass a
    callable that returns canned output to avoid the subprocess dependency.
    """
    invoker = invoker or _default_judge_invoker
    prompt = build_judge_prompt(rubric, transcript)
    raw = invoker(prompt)
    return JudgeVerdict.parse(raw)


def load_transcript_from_hook_input(hook_input: dict) -> str:
    """Best-effort transcript extraction from a Claude Code hook stdin payload.

    Stop hooks receive the session_id (and sometimes a transcript path).
    Claude Code persists transcripts to ``~/.claude/projects/<encoded>/<session>.jsonl``
    in current versions; we look for a ``transcript_path`` field first, fall
    back to a path-by-session lookup, and finally raise if nothing is found.

    The fallback path uses a heuristic; the operator can always wire the
    Stop hook to call eval-stop with an explicit ``--transcript-file`` flag
    if the default lookup misses.
    """
    if path := hook_input.get("transcript_path"):
        return Path(path).read_text(encoding="utf-8", errors="replace")

    session_id = hook_input.get("session_id")
    if not session_id:
        raise ValueError(
            "Stop hook input missing both 'transcript_path' and 'session_id'"
        )

    # Walk ~/.claude/projects/ for a matching session file. This is a fallback;
    # the operator should wire transcript_path explicitly when possible.
    claude_root = Path.home() / ".claude" / "projects"
    if not claude_root.exists():
        raise FileNotFoundError(
            f"~/.claude/projects/ not found; cannot resolve session {session_id}"
        )
    candidates = list(claude_root.rglob(f"{session_id}.jsonl"))
    if not candidates:
        raise FileNotFoundError(
            f"No transcript file found for session {session_id} under {claude_root}"
        )
    return candidates[0].read_text(encoding="utf-8", errors="replace")


def map_verdict_to_hook_output(verdict: JudgeVerdict) -> dict:
    """Translate a JudgeVerdict into the Claude Code hook output schema.

    Stop-hook output contract (from system-prompt-hooks-configuration.md):
      - ``continue: false`` keeps Claude working (blocks the stop)
      - ``stopReason`` is shown when blocking
      - ``decision: "block"`` is the Stop-hook block signal
      - ``systemMessage`` displays to the user

    Mapping:
      - ok=true              → continue=true,  systemMessage="rubric ✓ ..."
      - ok=false impossible  → continue=true,  systemMessage="rubric impossible ..."
                               (let the agent stop; operator must triage)
      - ok=false             → continue=false, decision="block",
                               stopReason="rubric not yet satisfied: ..."
    """
    if verdict.ok:
        return {
            "continue": True,
            "systemMessage": f"clawpm rubric satisfied: {verdict.reason}",
        }
    if verdict.impossible:
        # Surface impossibility but DO let the agent stop — otherwise the
        # session loops forever on an unachievable goal. The systemMessage
        # is the operator's signal to triage.
        return {
            "continue": True,
            "systemMessage": (
                f"clawpm rubric IMPOSSIBLE — operator should triage: "
                f"{verdict.reason}"
            ),
        }
    return {
        "continue": False,
        "decision": "block",
        "stopReason": f"clawpm rubric not satisfied: {verdict.reason}",
    }
