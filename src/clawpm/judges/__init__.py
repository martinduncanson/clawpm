"""LLM-judge components for clawpm.

The judges are small, dispatched LLM calls used to enforce contracts at
machine boundaries — the most important being the Stop-hook condition
evaluator that decides whether a subagent has actually satisfied its
success-criteria rubric.

See ``judges.stop_condition`` for the canonical (absolute, pass/fail)
implementation, and ``judges.tournament`` for the comparative-selection
primitive that picks the strongest of N candidate deliverables before the
close gate certifies it (CLAWP-044).
"""

from .stop_condition import (
    JudgeVerdict,
    evaluate_stop_condition,
    evaluate_stop_condition_confirmed,
)
from .tournament import (
    Candidate,
    TournamentResult,
    evaluate_tournament,
)

__all__ = [
    "JudgeVerdict",
    "evaluate_stop_condition",
    "evaluate_stop_condition_confirmed",
    "Candidate",
    "TournamentResult",
    "evaluate_tournament",
]
