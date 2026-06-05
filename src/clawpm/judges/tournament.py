"""Tournament judge — pairwise comparative selection among N candidates (CLAWP-044).

The stop-condition judge (``judges.stop_condition``) answers an ABSOLUTE
question: does this one deliverable satisfy the rubric? That is the right shape
for a pass/fail close gate. It is the wrong shape for *choosing the best of
several attempts* — you cannot score a single deliverable against nothing, and
absolute LLM scores are noisier than relative ones (the same reason pairwise
preference beats absolute scoring in preference modelling).

This module adds the COMPARATIVE primitive. Given a rubric and N candidate
deliverables (each a transcript), it runs pairwise "which better satisfies the
rubric" comparisons through a single-elimination gauntlet and returns the
winner. The intended use is UPSTREAM of the close gate: a low-confidence /
high-blast-radius dispatch spawns N attempts, the tournament selects the
strongest, and that winner THEN passes through
``evaluate_stop_condition[_confirmed]``. The tournament picks the best
candidate; it does not certify that the best candidate is good enough — that
remains the close gate's job. The two are orthogonal: selection then
verification.

Determinism discipline (model for judgment, code for facts): the bracket
structure, seeding, position-bias debiasing, and tie resolution are all plain
code. The ONLY model call is the single pairwise "A or B" judgment. Everything
a rule can decide, a rule decides.

Position bias: LLM judges have a documented, content-independent preference for
one position (often the first). A naive single comparison would let seed order
leak into the result. Each pair is therefore judged in BOTH orders; the pair is
only decided by the model when both orders agree on the same candidate. When
they disagree — the signature of pure position bias, or a genuine near-tie —
the comparison is ambiguous and the deterministic tiebreak keeps the higher
seed. A judge with total position bias thus degrades to seed order
(deterministic and surfaced in the comparison log), never to a coin flip. This
is the tournament analogue of the confirm-close tier's bias-to-refute: when the
model can't give an unambiguous signal, fall back to a deterministic rule, not
to chance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .stop_condition import JudgeInvoker, make_judge_invoker


@dataclass(frozen=True)
class Candidate:
    """One deliverable in the tournament.

    ``label`` is a stable identifier (e.g. the dispatch/subtask id) and must be
    unique within a tournament; ``transcript`` is the work to be judged.
    """

    label: str
    transcript: str


@dataclass
class Comparison:
    """The record of one decided pair: who faced whom and how it resolved.

    ``winner`` is the label of the surviving candidate. ``agreed`` is True when
    both position orders independently picked the same candidate (a genuine,
    position-bias-cancelled model decision); False means the orders disagreed
    (or a vote was unusable) and the higher seed was kept by the deterministic
    tiebreak.
    """

    higher_seed: str
    lower_seed: str
    winner: str
    agreed: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "higher_seed": self.higher_seed,
            "lower_seed": self.lower_seed,
            "winner": self.winner,
            "agreed": self.agreed,
            "reason": self.reason,
        }


@dataclass
class TournamentResult:
    """The selected winner plus the full comparison log for transparency."""

    winner: Candidate
    comparisons: list[Comparison] = field(default_factory=list)

    def to_dict(self) -> dict:
        # Deliberately omits the winner's full transcript — the caller already
        # holds the candidate files; the label is the join key. Keeps the
        # machine-readable result small even for large transcripts.
        return {
            "winner": self.winner.label,
            "decided_pairs": len(self.comparisons),
            "comparisons": [c.to_dict() for c in self.comparisons],
        }


COMPARE_PROMPT_TEMPLATE = """You are comparing two candidate deliverables, A and B, that each attempt the SAME task. Judge which candidate BETTER satisfies the rubric below — more criteria genuinely evidenced, fewer gaps, stronger proof of completion. Judge only against the rubric and what each transcript actually SHOWS; do not reward length, confident tone, or assertions unbacked by evidence.

Your response must be a JSON object with one of these shapes:
- {{"winner": "A", "reason": "<rubric criteria A evidences that B does not, quoting the evidence>"}}
- {{"winner": "B", "reason": "<rubric criteria B evidences that A does not, quoting the evidence>"}}

Pick the single stronger candidate. If they are genuinely indistinguishable on the rubric, pick the one whose transcript shows more concrete evidence.

=== RUBRIC ===
{rubric}

=== CANDIDATE A ===
{a}

=== CANDIDATE B ===
{b}

=== END ===

Return ONLY the JSON object. No prose before or after."""


def build_comparison_prompt(
    rubric: str, a_transcript: str, b_transcript: str
) -> str:
    """Compose the pairwise comparison prompt for one A-vs-B judgment."""
    return COMPARE_PROMPT_TEMPLATE.format(
        rubric=rubric.strip(),
        a=a_transcript.strip(),
        b=b_transcript.strip(),
    )


def parse_winner(raw: str) -> str | None:
    """Return ``"A"``, ``"B"``, or ``None`` from a comparison response.

    Defensive in the same spirit as ``JudgeVerdict.parse``: strip code fences,
    find the first JSON object, read ``winner`` and normalise to upper-case.
    ANY deviation (no JSON, non-object, missing/invalid ``winner``, an error
    string) returns ``None`` — which the caller treats as an abstention for
    that order, collapsing the pair to ambiguity (keep higher seed). A garbage
    comparison must never silently pick a winner.
    """
    stripped = raw.strip()
    if stripped.startswith("```"):
        inner: list[str] = []
        in_fence = False
        for ln in stripped.splitlines():
            if ln.startswith("```"):
                in_fence = not in_fence
                continue
            inner.append(ln)
        stripped = "\n".join(inner).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    winner = data.get("winner")
    if not isinstance(winner, str):
        return None
    winner = winner.strip().upper()
    return winner if winner in {"A", "B"} else None


def _safe_winner(
    rubric: str, a_transcript: str, b_transcript: str, invoker: JudgeInvoker
) -> str | None:
    """Run one comparison order and parse it; swallow invoker errors as None.

    A failed judge call (CLI missing / auth / quota — all ``RuntimeError`` from
    ``make_judge_invoker``) is an abstention for this order, not a crash of the
    whole tournament. ``None`` propagates to ambiguity → keep higher seed.
    """
    try:
        raw = invoker(build_comparison_prompt(rubric, a_transcript, b_transcript))
    except RuntimeError:
        return None
    return parse_winner(raw)


def _compare_pair(
    rubric: str, higher: Candidate, lower: Candidate, invoker: JudgeInvoker
) -> Comparison:
    """Decide one pair with both-orders position-bias debiasing.

    ``higher`` is the higher-seeded candidate, kept on ambiguity. The pair is
    judged twice — higher-as-A then higher-as-B (positions swapped) — and is
    only awarded to the model's pick when BOTH orders name the same candidate.
    """
    # Order 1: higher is position A, lower is position B.
    o1 = _safe_winner(rubric, higher.transcript, lower.transcript, invoker)
    # Order 2: positions swapped — lower is A, higher is B.
    o2 = _safe_winner(rubric, lower.transcript, higher.transcript, invoker)

    # Map each order's positional verdict back to a candidate label.
    pick1 = {"A": higher.label, "B": lower.label}.get(o1)
    pick2 = {"A": lower.label, "B": higher.label}.get(o2)

    if pick1 is not None and pick1 == pick2:
        return Comparison(
            higher_seed=higher.label,
            lower_seed=lower.label,
            winner=pick1,
            agreed=True,
            reason=f"both orders favoured {pick1}",
        )
    return Comparison(
        higher_seed=higher.label,
        lower_seed=lower.label,
        winner=higher.label,
        agreed=False,
        reason=(
            "orders disagreed or a vote was unusable "
            f"(order1={o1!r}, order2={o2!r}); kept higher seed"
        ),
    )


def evaluate_tournament(
    rubric: str,
    candidates: list[Candidate],
    invoker: JudgeInvoker | None = None,
) -> TournamentResult:
    """Select the candidate that best satisfies ``rubric`` via pairwise comparison.

    Single-elimination gauntlet: ``candidates[0]`` is the initial incumbent
    (top seed); each subsequent candidate challenges the incumbent and the
    winner carries forward. Exactly ``len(candidates) - 1`` pairs are decided.
    Seed order is the input order, so the caller controls the tiebreak by
    ordering candidates strongest-prior-first; the reigning incumbent is always
    the higher seed in its next comparison (a challenger must clearly beat the
    sitting winner, not merely tie it).

    Raises ``ValueError`` on an empty candidate list or duplicate labels (the
    label is the result join key and the winner-detection key, so it must be
    unique). A single candidate short-circuits with zero model calls.
    """
    if not candidates:
        raise ValueError("evaluate_tournament requires at least one candidate")
    labels = [c.label for c in candidates]
    if len(set(labels)) != len(labels):
        raise ValueError(f"candidate labels must be unique; got {labels}")
    if len(candidates) == 1:
        return TournamentResult(winner=candidates[0], comparisons=[])

    invoker = invoker or make_judge_invoker()
    incumbent = candidates[0]
    comparisons: list[Comparison] = []
    for challenger in candidates[1:]:
        result = _compare_pair(rubric, incumbent, challenger, invoker)
        comparisons.append(result)
        if result.winner == challenger.label:
            incumbent = challenger
    return TournamentResult(winner=incumbent, comparisons=comparisons)
