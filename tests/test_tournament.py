"""Tests for the tournament judge — pairwise comparative selection (CLAWP-044).

The tournament SELECTS the strongest of N candidate deliverables; it does not
certify the winner (that stays the stop-condition close gate's job). Coverage:
  - A clear content winner is chosen and both orders agree (position-bias
    cancelled -> agreed=True).
  - A pure position-bias judge collapses to seed order: the two orders disagree,
    the pair is ambiguous, and the higher seed is kept (agreed=False).
  - A 3+ candidate gauntlet decides exactly N-1 pairs and surfaces the strongest.
  - A single candidate short-circuits with ZERO model calls.
  - Empty / duplicate-label inputs raise ValueError.
  - parse_winner is defensive (fences, case, garbage, out-of-range -> None).
  - An invoker that errors abstains -> ambiguity -> keep higher seed, no crash.
"""

from __future__ import annotations

import pytest

from clawpm.judges.tournament import (
    Candidate,
    Comparison,
    TournamentResult,
    build_comparison_prompt,
    evaluate_tournament,
    parse_winner,
)


# ---------------------------------------------------------------------------
# Scripted invokers. They parse the A/B blocks out of the comparison prompt so
# one stub can answer correctly regardless of which order the bracket asks in.
# ---------------------------------------------------------------------------


def _extract_ab(prompt: str) -> tuple[str, str]:
    a = prompt.split("=== CANDIDATE A ===", 1)[1].split("=== CANDIDATE B ===", 1)[0].strip()
    b = prompt.split("=== CANDIDATE B ===", 1)[1].split("=== END ===", 1)[0].strip()
    return a, b


class ContentJudge:
    """Position-bias-FREE: picks whichever position holds the higher-scoring
    transcript, so both orders agree on the same candidate."""

    def __init__(self, score):
        self.score = score
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        a, b = _extract_ab(prompt)
        winner = "A" if self.score(a) >= self.score(b) else "B"
        return f'{{"winner": "{winner}", "reason": "content"}}'


class AlwaysAJudge:
    """Pure position bias: always names position A, whatever the content."""

    def __init__(self):
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        return '{"winner": "A", "reason": "position bias"}'


class ExplodingJudge:
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        raise RuntimeError("judge CLI exploded")


RUBRIC = "1. Tests pass\n2. No regressions"
RANK = {"alpha": 1, "bravo": 2, "charlie": 3}
_rank_score = lambda t: RANK[t.strip()]


# ---------------------------------------------------------------------------
# Clear winner — both orders agree.
# ---------------------------------------------------------------------------


class TestClearWinner:
    def test_stronger_candidate_wins_with_agreement(self):
        inv = ContentJudge(_rank_score)
        cands = [Candidate("a", "alpha"), Candidate("b", "bravo")]
        result = evaluate_tournament(RUBRIC, cands, invoker=inv)
        assert isinstance(result, TournamentResult)
        assert result.winner.label == "b"
        assert len(result.comparisons) == 1
        assert result.comparisons[0].agreed is True
        # Both orders judged -> two model calls for one pair.
        assert inv.calls == 2

    def test_winner_independent_of_seed_order(self):
        # Same two candidates, reversed seed order -> same content winner.
        inv = ContentJudge(_rank_score)
        cands = [Candidate("b", "bravo"), Candidate("a", "alpha")]
        result = evaluate_tournament(RUBRIC, cands, invoker=inv)
        assert result.winner.label == "b"
        assert result.comparisons[0].agreed is True


# ---------------------------------------------------------------------------
# Position bias collapses to seed order (the documented pre-mortem).
# ---------------------------------------------------------------------------


class TestPositionBiasDebiasing:
    def test_pure_position_bias_keeps_higher_seed(self):
        inv = AlwaysAJudge()
        cands = [Candidate("first", "alpha"), Candidate("second", "charlie")]
        result = evaluate_tournament(RUBRIC, cands, invoker=inv)
        # Both orders say "A": order1 -> first, order2 -> second. Disagreement
        # -> ambiguous -> the higher seed (first) is kept, deterministically.
        assert result.winner.label == "first"
        assert result.comparisons[0].agreed is False
        assert "kept higher seed" in result.comparisons[0].reason


# ---------------------------------------------------------------------------
# Multi-candidate gauntlet — N-1 decided pairs, strongest surfaces.
# ---------------------------------------------------------------------------


class TestGauntlet:
    def test_three_candidates_decide_two_pairs_and_surface_strongest(self):
        inv = ContentJudge(_rank_score)
        cands = [
            Candidate("a", "alpha"),    # weakest
            Candidate("b", "bravo"),
            Candidate("c", "charlie"),  # strongest
        ]
        result = evaluate_tournament(RUBRIC, cands, invoker=inv)
        assert result.winner.label == "c"
        # Single-elimination gauntlet: exactly N-1 = 2 decided pairs.
        assert len(result.comparisons) == 2
        assert all(c.agreed for c in result.comparisons)
        # Two model calls (both orders) per decided pair.
        assert inv.calls == 4

    def test_strongest_seeded_first_still_wins(self):
        inv = ContentJudge(_rank_score)
        cands = [
            Candidate("c", "charlie"),  # strongest, top seed
            Candidate("b", "bravo"),
            Candidate("a", "alpha"),
        ]
        result = evaluate_tournament(RUBRIC, cands, invoker=inv)
        assert result.winner.label == "c"
        assert len(result.comparisons) == 2


# ---------------------------------------------------------------------------
# Short-circuit and input validation.
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_single_candidate_short_circuits_without_calling_judge(self):
        inv = ExplodingJudge()
        cands = [Candidate("solo", "alpha")]
        result = evaluate_tournament(RUBRIC, cands, invoker=inv)
        assert result.winner.label == "solo"
        assert result.comparisons == []
        assert inv.calls == 0  # never invoked

    def test_empty_candidates_raises(self):
        with pytest.raises(ValueError, match="at least one candidate"):
            evaluate_tournament(RUBRIC, [], invoker=ContentJudge(_rank_score))

    def test_duplicate_labels_raise(self):
        cands = [Candidate("dup", "alpha"), Candidate("dup", "bravo")]
        with pytest.raises(ValueError, match="unique"):
            evaluate_tournament(RUBRIC, cands, invoker=ContentJudge(_rank_score))


# ---------------------------------------------------------------------------
# Invoker error abstains rather than crashing the tournament.
# ---------------------------------------------------------------------------


class TestDegradedJudge:
    def test_invoker_error_abstains_and_keeps_higher_seed(self):
        inv = ExplodingJudge()
        cands = [Candidate("first", "alpha"), Candidate("second", "charlie")]
        result = evaluate_tournament(RUBRIC, cands, invoker=inv)
        # Both orders error -> both None -> ambiguous -> higher seed kept.
        assert result.winner.label == "first"
        assert result.comparisons[0].agreed is False


# ---------------------------------------------------------------------------
# parse_winner robustness.
# ---------------------------------------------------------------------------


class TestParseWinner:
    def test_plain_a(self):
        assert parse_winner('{"winner": "A", "reason": "x"}') == "A"

    def test_lowercase_normalised(self):
        assert parse_winner('{"winner": "b"}') == "B"

    def test_code_fenced(self):
        assert parse_winner('```json\n{"winner": "A"}\n```') == "A"

    def test_prose_around_object(self):
        assert parse_winner('Sure! {"winner": "B", "reason": "y"} done') == "B"

    def test_garbage_is_none(self):
        assert parse_winner("the model rambled with no json") is None

    def test_out_of_range_winner_is_none(self):
        assert parse_winner('{"winner": "C"}') is None

    def test_missing_winner_is_none(self):
        assert parse_winner('{"reason": "no winner field"}') is None

    def test_non_object_is_none(self):
        assert parse_winner('["A", "B"]') is None


# ---------------------------------------------------------------------------
# Prompt construction.
# ---------------------------------------------------------------------------


class TestComparisonPrompt:
    def test_prompt_contains_rubric_and_both_candidates(self):
        prompt = build_comparison_prompt(RUBRIC, "alpha-work", "bravo-work")
        assert "CANDIDATE A" in prompt and "CANDIDATE B" in prompt
        assert "alpha-work" in prompt and "bravo-work" in prompt
        assert "No regressions" in prompt
        a, b = _extract_ab(prompt)
        assert a == "alpha-work" and b == "bravo-work"


class TestComparisonRecord:
    def test_to_dict_shape(self):
        c = Comparison("hi", "lo", "hi", False, "kept higher seed")
        d = c.to_dict()
        assert d == {
            "higher_seed": "hi",
            "lower_seed": "lo",
            "winner": "hi",
            "agreed": False,
            "reason": "kept higher seed",
        }

    def test_result_to_dict_omits_transcript(self):
        result = TournamentResult(
            winner=Candidate("w", "big transcript body"),
            comparisons=[Comparison("w", "l", "w", True, "both orders favoured w")],
        )
        d = result.to_dict()
        assert d["winner"] == "w"
        assert d["decided_pairs"] == 1
        assert "big transcript body" not in str(d)
