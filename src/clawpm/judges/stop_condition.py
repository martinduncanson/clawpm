"""Stop-condition evaluator — the killer feature.

Adapts the Anthropic Claude Code ``/goal`` Stop-hook condition evaluator
to clawpm. A small LLM judge reads a subagent's transcript + the task's
rubric, then returns the JSON shape used by the official evaluator
(reverse-engineered by Piebald-AI, vintage 2026-Q1; verify against
current docs at ``docs.anthropic.com`` if the contract shape changes):

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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# cp1252-safe stdio (CLAWP-011): this module writes the fallback announcement to
# sys.stderr; reconfigure to UTF-8 (errors="replace") so a non-ASCII fallback
# command repr can't raise UnicodeEncodeError on a Windows cp1252 console.
# Guarded for redirected / wrapped streams that lack reconfigure().
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError):
    pass

# Default judge — subprocess invocation of the user's installed `claude` CLI in
# print mode. Override via env CLAWPM_JUDGE_CMD when you want a different
# model (e.g. set to `claude --model claude-haiku-4-5 -p`) or a stub for tests.
DEFAULT_JUDGE_CMD = ["claude", "--print", "--model", "claude-haiku-4-5"]

# Fallback judge — a LOCAL model, tried when the primary judge is unavailable
# (not installed, or exits non-zero, e.g. auth/quota failure). Keeps grading
# working — and subscription-cost-free — when `claude -p` can't be reached.
# Override the model via env CLAWPM_JUDGE_FALLBACK_CMD (e.g.
# "ollama run qwen2.5"); set it to the empty string to DISABLE the fallback.
DEFAULT_JUDGE_FALLBACK_CMD = ["ollama", "run", "llama3.1"]

# Per-call judge subprocess budget. Shared with dispatch.py so the Stop-hook
# timeout can be sized against the confirm-close vote budget (base + N
# refuters run sequentially, each bounded by this).
JUDGE_CALL_TIMEOUT_SECONDS = 60


class JudgeUnavailable(RuntimeError):
    """Primary judge could not produce a verdict for a reason that warrants
    trying the fallback — command not found, or non-zero exit (broken / auth /
    quota). Distinct from a timeout, which does NOT trigger fallback."""


class JudgeTimeout(RuntimeError):
    """Judge subprocess exceeded its time budget. NOT a fallback trigger: a
    timeout usually means the prompt/transcript is too large (a local model
    would struggle too), and falling back would double the latency past the
    Stop-hook timeout budget. Surfaced so the caller's fail-open path handles
    it."""


@dataclass
class JudgeVerdict:
    """Parsed judge output, in the Anthropic Stop-hook shape (reverse-
    engineered by Piebald-AI; verify against current docs if the hook
    contract changes).

    Invariant: ``ok=True`` and ``impossible=True`` is contradictory and
    rejected at construction. This is structural — every code path that
    creates a JudgeVerdict (parser, tests, future callers) is protected,
    not just the parser.
    """

    ok: bool
    reason: str
    impossible: bool = False

    def __post_init__(self) -> None:
        if self.ok and self.impossible:
            raise ValueError(
                "JudgeVerdict cannot be both ok=True and impossible=True; "
                "use parse() if you need to coerce contradictory judge "
                "output into a safe not-ok verdict."
            )

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
        # Strict bool validation: an LLM that returns `{"ok": "false"}`
        # (string instead of bool) would coerce to True via bool(...) and
        # silently bypass the Stop hook. Treat any non-bool as malformed
        # and return a blocking not-ok verdict.
        raw_ok = data.get("ok")
        raw_impossible = data.get("impossible", False)
        if not isinstance(raw_ok, bool):
            return cls(
                ok=False,
                reason=(
                    f"judge output has non-boolean 'ok' field "
                    f"({type(raw_ok).__name__}={raw_ok!r}); refusing to "
                    f"coerce — schema drift could fail-open the Stop hook"
                ),
            )
        if not isinstance(raw_impossible, bool):
            return cls(
                ok=False,
                reason=(
                    f"judge output has non-boolean 'impossible' field "
                    f"({type(raw_impossible).__name__}={raw_impossible!r}); "
                    f"refusing to coerce"
                ),
            )
        ok = raw_ok
        impossible = raw_impossible
        reason = data.get("reason", "")
        if not isinstance(reason, str):
            reason = str(reason)
        if impossible and ok:
            # Self-contradictory output is a JUDGE QUALITY BUG, not an
            # impossibility signal. We must NOT route this to the same
            # "let agent stop" path as a genuine impossibility — that
            # gives the agent a free escape via inducing contradiction.
            # Return ok=False, impossible=False, reason flagging the
            # contradiction so map_verdict_to_hook_output blocks the stop.
            return cls(
                ok=False,
                impossible=False,
                reason=(
                    f"JUDGE_CONTRADICTION: judge returned ok=true AND "
                    f"impossible=true. Original reason: {reason}"
                ),
            )
        return cls(ok=ok, reason=reason, impossible=impossible)


JudgeInvoker = Callable[[str], str]
"""A judge invoker takes the prompt string and returns the raw text response."""


def _run_judge_cmd(
    cmd: list[str], prompt: str, cwd: Path | None = None
) -> str:
    """Run ONE judge command with the prompt on stdin; return stdout.

    The prompt is sent on stdin so long rubrics + transcripts don't hit shell
    argument limits. Raises:
      - ``JudgeUnavailable`` if the binary isn't found or exits non-zero
        (fallback-eligible: the judge is broken / missing / auth-failed).
      - ``JudgeTimeout`` if it exceeds the per-call budget (NOT fallback-
        eligible — see the class docstring).
    """
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=JUDGE_CALL_TIMEOUT_SECONDS,
            cwd=str(cwd) if cwd is not None else None,
        )
    except FileNotFoundError as exc:
        raise JudgeUnavailable(
            f"Judge command not found: {cmd[0]!r}. Error: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise JudgeTimeout(
            f"Judge {cmd[0]!r} timed out after {exc.timeout}s; rubric or "
            "transcript may be too large for a single call"
        ) from exc
    if result.returncode != 0:
        raise JudgeUnavailable(
            f"Judge {cmd[0]!r} exited {result.returncode}: "
            f"{result.stderr[:500]}"
        )
    return result.stdout


def _resolve_judge_cmds(
    judge_cmd_override: str | None = None,
) -> tuple[list[str], list[str] | None]:
    """Resolve ``(primary_cmd, fallback_cmd_or_None)``.

    Primary resolution (highest priority first): ``judge_cmd_override`` →
    ``CLAWPM_JUDGE_CMD`` env → ``DEFAULT_JUDGE_CMD`` (claude --print).

    Fallback: ``CLAWPM_JUDGE_FALLBACK_CMD`` env → ``DEFAULT_JUDGE_FALLBACK_CMD``
    (local model). Setting the env var to the empty string DISABLES the
    fallback (returns ``None``).
    """
    if judge_cmd_override:
        primary = shlex.split(judge_cmd_override)
    else:
        env_cmd = os.environ.get("CLAWPM_JUDGE_CMD")
        primary = shlex.split(env_cmd) if env_cmd else list(DEFAULT_JUDGE_CMD)

    fb_env = os.environ.get("CLAWPM_JUDGE_FALLBACK_CMD")
    if fb_env is not None:
        # Explicit empty string => fallback disabled.
        fallback = shlex.split(fb_env) if fb_env.strip() else None
    else:
        fallback = list(DEFAULT_JUDGE_FALLBACK_CMD)
    return primary, fallback


def make_judge_invoker(
    judge_cmd_override: str | None = None,
    cwd: Path | None = None,
    enable_fallback: bool = True,
) -> JudgeInvoker:
    """Build a judge invoker: primary judge (``claude -p`` by default), with a
    local-model fallback when the primary is UNAVAILABLE (not installed, or
    non-zero exit — broken / auth / quota).

    A primary *timeout* does NOT fall back — it re-raises so the caller's
    existing fail-open path handles it and the Stop-hook timeout budget stays
    accurate (the fallback would otherwise double the latency). When the
    fallback fires it is announced on stderr AND the returned invoker's
    ``fallback_used`` attribute is set True, so callers can persist a durable
    degradation marker (see ``_annotate_fallback``). If both fail, a combined
    error is raised. All errors subclass ``RuntimeError`` so existing
    ``except RuntimeError`` handlers keep working.

    ``enable_fallback=False`` forces primary-only — used for the agent-dispatch
    SUBAGENT runner, where falling back would mean a local text model
    *performs the work* (its output becomes the transcript) instead of a real
    Claude Code agent honoring the worktree `.claude` hooks. Fallback is a
    JUDGE concept (grade with whatever's available), never an execution one.
    """
    primary, fallback = _resolve_judge_cmds(judge_cmd_override)
    if not enable_fallback:
        fallback = None

    def _invoke(prompt: str) -> str:
        try:
            return _run_judge_cmd(primary, prompt, cwd=cwd)
        except JudgeTimeout:
            # Slow primary — surface, do not double the budget via fallback.
            raise
        except JudgeUnavailable as primary_exc:
            if not fallback:
                raise RuntimeError(
                    f"Primary judge unavailable and fallback "
                    f"disabled/unconfigured. {primary_exc}"
                ) from primary_exc
            sys.stderr.write(
                f"clawpm judge: primary judge unavailable ({primary_exc}); "
                f"falling back to local judge {fallback[0]!r}\n"
            )
            try:
                out = _run_judge_cmd(fallback, prompt, cwd=cwd)
            except RuntimeError as fb_exc:
                raise RuntimeError(
                    f"Both primary and fallback judges failed. "
                    f"Primary: {primary_exc}  Fallback: {fb_exc}. "
                    f"Install Claude Code or a local judge (e.g. ollama), or "
                    f"set CLAWPM_JUDGE_CMD / CLAWPM_JUDGE_FALLBACK_CMD."
                ) from fb_exc
            _invoke.fallback_used = True
            return out

    _invoke.fallback_used = False
    return _invoke


# Durable marker folded into a verdict's reason when the local fallback graded
# it — so the reflection JSONL (and `doctor`) can tell a healthy primary from
# "every close is now being graded by the local fallback" (Codex P2). Greppable.
FALLBACK_MARKER = "[JUDGE_FALLBACK_USED]"


def _annotate_fallback(verdict: "JudgeVerdict", invoker: JudgeInvoker) -> "JudgeVerdict":
    """Prefix ``FALLBACK_MARKER`` into ``verdict.reason`` if the invoker fell
    back to the local judge for this grade. No-op for stub/primary invokers."""
    if getattr(invoker, "fallback_used", False) and FALLBACK_MARKER not in verdict.reason:
        return JudgeVerdict(
            ok=verdict.ok,
            reason=f"{FALLBACK_MARKER} {verdict.reason}",
            impossible=verdict.impossible,
        )
    return verdict


def _default_judge_invoker(prompt: str) -> str:
    """Default judge: ``claude -p`` primary with the local-model fallback.

    Thin back-compat wrapper over ``make_judge_invoker`` (which is what the
    evaluate_* functions use directly so the resolution happens once per call
    rather than per vote)."""
    return make_judge_invoker()(prompt)


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

    ``invoker`` defaults to ``claude --print`` (with local-model fallback);
    tests pass a callable that returns canned output to avoid the subprocess
    dependency. When the default invoker falls back to the local judge, the
    verdict reason is tagged with ``FALLBACK_MARKER`` for durable visibility.
    """
    invoker = invoker or make_judge_invoker()
    prompt = build_judge_prompt(rubric, transcript)
    raw = invoker(prompt)
    return _annotate_fallback(JudgeVerdict.parse(raw), invoker)


# ===========================================================================
# Adversarial confirm-close tier (CLAWP-041)
# ===========================================================================
#
# The base judge above runs ONCE per Stop event and is deliberately fail-
# closed against *malformed* output. Its remaining failure mode is fail-OPEN
# via over-charitable reading: a single judge reads "I refactored the auth
# module" and decides the criterion "auth tests pass" is satisfied, when no
# test run actually appears in the transcript. One judge is a single point of
# optimistic failure.
#
# The confirm-close tier hardens *only the terminal decision*. A false
# ``block`` (ok=false) is self-correcting — the agent keeps working and re-
# grades; cost ≈ one wasted loop. A false ``ok=true`` is terminal — the task
# closes and unverified work ships. So the adversarial cost is spent ONLY at
# the ok=true→close transition, never on the (common, many-per-task) block
# path. The block path stays a single judge call.
#
# The refuter uses the SAME ``{ok, reason, impossible}`` contract and parser,
# but an adversarial prompt: "a prior judge passed this — try to refute it;
# default to refuted when uncertain." ``ok=false`` from the refuter means
# "refuted → keep blocking"; ``ok=true`` means "tried and genuinely could not
# refute → the close stands".

_REFUTE_LENSES = {
    "evidence": (
        "Lens: EVIDENCE — focus on whether the transcript contains concrete, "
        "quotable evidence (command output, diffs, test results, file "
        "contents) for each claim, not merely an assertion that it was done."
    ),
    "correctness": (
        "Lens: CORRECTNESS — focus on whether the claimed work is actually "
        "correct and complete, not merely attempted or partially done."
    ),
    "reproduction": (
        "Lens: REPRODUCTION — focus on whether an independent reviewer could "
        "reproduce or verify each claimed outcome from what the transcript "
        "actually shows."
    ),
}

# Default single-vote lens. EVIDENCE is the sharpest against the over-
# charitable-reading failure mode the tier exists to catch.
_DEFAULT_REFUTE_LENS_ORDER = ["evidence", "correctness", "reproduction"]


REFUTE_PROMPT_TEMPLATE = """You are a SKEPTICAL verifier performing an independent confirm-close check. A prior judge has claimed the rubric below is SATISFIED by the transcript. Your job is the OPPOSITE: try to REFUTE that claim.

{prior_reason_block}{lens_instruction}

Examine each rubric criterion against the transcript. You are hunting for ANY criterion the transcript CLAIMS to satisfy but does not actually EVIDENCE — e.g. the work says "tests pass" but no test run appears in the transcript; it says "implemented X" but no corresponding change is shown; a gradeable signal whose actual value is absent or contradicts the claim.

Your response must be a JSON object with one of these shapes:
- {{"ok": false, "reason": "<name the specific criterion that is claimed but not evidenced, and quote what is missing>"}}  — you refuted it; the close should stay BLOCKED.
- {{"ok": true, "reason": "<confirm every criterion is genuinely evidenced, quoting the evidence>"}}  — ONLY if you genuinely cannot refute after trying.

Bias: when in doubt, REFUTE (return {{"ok": false}}). A false pass closes the task and ships unverified work; a false refute merely costs one more iteration. Default to ok=false unless the evidence is unambiguous.

=== RUBRIC ===
{rubric}

=== TRANSCRIPT ===
{transcript}

=== END ===

Return ONLY the JSON object. No prose before or after."""


# CLAWP-043 — env override to restore the legacy ANCHORED refuter prompt (the
# refuter sees the base judge's passing rationale). Default is blind: feeding
# the refuter the optimistic judge's framing anchors it toward agreement — the
# mutual-softening effect adversarial review exists to avoid. Kept as a toggle
# so the two arms can be A/B'd on a transcript corpus.
REFUTER_SEES_PRIOR_ENV = "CLAWPM_REFUTER_SEES_PRIOR"

_TRUTHY = {"1", "true", "yes", "on"}


def _resolve_refuter_sees_prior(explicit: bool | None) -> bool:
    """Resolve whether the refuter sees the base judge's reason.

    Precedence: explicit arg → ``CLAWPM_REFUTER_SEES_PRIOR`` env → ``False``
    (blind, the anchoring-resistant default).
    """
    if explicit is not None:
        return explicit
    return os.environ.get(REFUTER_SEES_PRIOR_ENV, "").strip().lower() in _TRUTHY


def build_refutation_prompt(
    rubric: str,
    transcript: str,
    prior_reason: str,
    lens: str = "evidence",
    include_prior_reason: bool = False,
) -> str:
    """Compose the adversarial refutation prompt for one confirm-close vote.

    ``lens`` selects the angle of attack so multi-vote refutation stays
    independent rather than collapsing to one repeated check.

    ``include_prior_reason`` controls whether the base judge's passing
    rationale is shown to the refuter. It defaults to ``False`` (blind):
    feeding the refuter the optimistic judge's framing anchors it toward
    agreement (the mutual-softening effect adversarial review exists to
    avoid). The refuter already has the rubric and the transcript — it does
    not need the prior reason to independently hunt for claimed-but-
    unevidenced criteria, and withholding it keeps the prosecution genuinely
    independent of the defence. Set ``True`` (or
    ``CLAWPM_REFUTER_SEES_PRIOR=1`` via ``evaluate_stop_condition_confirmed``)
    to restore the legacy anchored prompt for A/B comparison.
    """
    lens_instruction = _REFUTE_LENSES.get(lens, _REFUTE_LENSES["evidence"])
    if include_prior_reason:
        prior_reason_block = (
            "Prior judge's stated reason for passing: "
            f"{(prior_reason or '(none given)').strip()}\n\n"
        )
    else:
        prior_reason_block = ""
    return REFUTE_PROMPT_TEMPLATE.format(
        rubric=rubric.strip(),
        transcript=transcript.strip(),
        prior_reason_block=prior_reason_block,
        lens_instruction=lens_instruction,
    )


def evaluate_stop_condition_confirmed(
    rubric: str,
    transcript: str,
    invoker: JudgeInvoker | None = None,
    refute_votes: int = 1,
    refuter_sees_prior: bool | None = None,
) -> JudgeVerdict:
    """Base grade + adversarial confirm-close on the ok=true→close transition.

    Cheap path (the common case): runs the base judge once. If the base
    verdict is NOT ``ok`` — including an ``impossible`` verdict — it is
    returned verbatim and **the refuter is never invoked**. The block path
    therefore costs exactly one judge call, unchanged from
    ``evaluate_stop_condition``.

    Confirm-close path (once per task, at close): when the base verdict is
    ``ok=true``, spawn ``refute_votes`` adversarial refutation calls (lens-
    varied so they are independent). The close is overturned to ``ok=false``
    if AT LEAST HALF of the refuters that actually ran refute it
    (``ceil(effective/2)``); with the default ``refute_votes=1`` a single
    refutation overturns. **Ties overturn by design** — a 1-of-2 split is
    doubt, and for a terminal close (a false ok=true ships unverified work)
    doubt should keep the task open. This is a deliberate bias toward refuting,
    NOT a strict >50% majority. A surviving close returns the base verdict.

    Refuter-invoker errors are caught and treated as ABSTENTIONS — they are
    dropped from BOTH the refutation count AND the threshold denominator, so
    the threshold is computed over the refuters that actually returned a
    verdict. This matters under multi-vote: with ``refute_votes=3`` and two
    refuters erroring, a single surviving ``ok=false`` vote still overturns the
    close (1 of 1 effective vote), rather than being outvoted by two dead
    judges (the fail-open this avoids). Only a TOTAL refuter outage (every vote
    errored) lets the base verdict stand — the right call versus trapping the
    agent in an infinite block loop on a systematically broken refuter.

    Any refuter error decorates the returned reason with a greppable
    ``CONFIRM_CLOSE_DEGRADED`` token so a confirm-close that silently lost its
    refutation pass is discoverable in the persisted iteration-event stream
    (the base-judge error path writes its own doctor signal in the CLI; this is
    the equivalent marker for refuter degradation).
    """
    invoker = invoker or make_judge_invoker()
    sees_prior = _resolve_refuter_sees_prior(refuter_sees_prior)
    base = evaluate_stop_condition(rubric, transcript, invoker=invoker)

    # Block path (and impossible path): single call, refuter never runs.
    if not base.ok:
        return base  # already fallback-annotated by evaluate_stop_condition

    votes = max(1, refute_votes)
    refutations: list[str] = []
    errors: list[str] = []
    for i in range(votes):
        lens = _DEFAULT_REFUTE_LENS_ORDER[i % len(_DEFAULT_REFUTE_LENS_ORDER)]
        prompt = build_refutation_prompt(
            rubric, transcript, base.reason, lens=lens,
            include_prior_reason=sees_prior,
        )
        try:
            raw = invoker(prompt)
        except RuntimeError as exc:
            # Abstention — dropped from refutations AND denominator below.
            errors.append(f"{lens}:{exc}")
            continue
        vote = JudgeVerdict.parse(raw)
        # Refuter ok=false == "refuted". Parse failures default to ok=false
        # (see JudgeVerdict.parse), which correctly counts as a refutation —
        # a malformed refuter response biases toward keeping the task open.
        if not vote.ok:
            refutations.append(vote.reason)

    # Threshold over the refuters that ACTUALLY ran, not the configured count.
    effective = votes - len(errors)
    if effective <= 0:
        # Total refuter outage → base verdict stands (fail-open vs infinite
        # block loop), but surfaced for doctor via CONFIRM_CLOSE_DEGRADED.
        return _annotate_fallback(
            JudgeVerdict(
                ok=True,
                impossible=False,
                reason=(
                    f"{base.reason} [CONFIRM_CLOSE_DEGRADED: all {votes} "
                    f"refuter(s) errored, none cast a vote: {errors[0]}]"
                ),
            ),
            invoker,
        )

    # ceil(effective / 2): ties overturn, by design (bias toward refuting —
    # NOT a strict >50% majority; see the function docstring).
    threshold = -(-effective // 2)
    if len(refutations) >= threshold:
        first = refutations[0] if refutations else "(no reason captured)"
        return _annotate_fallback(
            JudgeVerdict(
                ok=False,
                impossible=False,
                reason=(
                    f"CONFIRM_CLOSE_REFUTED ({len(refutations)}/{effective} "
                    f"refuters that ran overturned the close): {first}"
                ),
            ),
            invoker,
        )

    # Close stands. If some (but not all) refuters errored, decorate so the
    # partial degradation is doctor-discoverable.
    if errors:
        return _annotate_fallback(
            JudgeVerdict(
                ok=True,
                impossible=False,
                reason=(
                    f"{base.reason} [CONFIRM_CLOSE_DEGRADED: "
                    f"{len(refutations)}/{effective} refuted, {len(errors)} "
                    f"refuter error(s) abstained: {errors[0]}]"
                ),
            ),
            invoker,
        )
    return _annotate_fallback(base, invoker)


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
    """Translate a JudgeVerdict into the Claude Code Stop-hook output.

    Stop-hook output contract (Codex round-2 P1 correction):
      - ``decision: "block"`` + ``reason`` → blocks the *stop event*, the
        agent must continue working. This is what we want for an
        unsatisfied rubric — force another iterate→grade→revise cycle.
      - ``continue: false`` → halts the entire processing pipeline
        (terminates the agent). **Different semantics** — must NOT be
        used to mean "the rubric is not yet satisfied", or a failed
        rubric ends the session instead of forcing another loop, which
        defeats the entire Stop-gate contract.
      - ``continue: true`` + ``systemMessage`` → let the stop go through,
        with a visible note to the operator.

    Mapping:
      - ok=true              → continue=true,  systemMessage="rubric ✓ ..."
      - ok=false impossible  → continue=true,  systemMessage="rubric impossible ..."
                               (let the agent stop; operator must triage)
      - ok=false             → decision="block", reason="rubric not yet
                               satisfied: ..." (force another loop)
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
    # Unsatisfied rubric: block the Stop event so the agent keeps
    # working. NOT `continue: false` — that would terminate the agent
    # outright. `decision: "block"` + `reason` is the documented Stop-
    # hook block signal.
    return {
        "decision": "block",
        "reason": f"clawpm rubric not satisfied: {verdict.reason}",
    }
