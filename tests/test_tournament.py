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

import json

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.judges.tournament import (
    TOURNAMENT_DEGRADED_MARKER,
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


class SequenceJudge:
    """Returns canned responses one per call, in order (cycles the last). A
    response of ``__ERROR__`` raises RuntimeError for that call. Lets a test
    drive each position-order of a pair independently."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        resp = self.responses[idx]
        if resp == "__ERROR__":
            raise RuntimeError("judge exploded")
        return resp


class FallbackJudge:
    """A working stub that reports it graded via the local fallback (mirrors
    make_judge_invoker setting fallback_used=True)."""

    def __init__(self, response: str):
        self.response = response
        self.fallback_used = True
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        return self.response


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
        # Both orders returned usable (conflicting) verdicts — a genuine
        # disagreement, NOT a degraded vote.
        assert result.comparisons[0].degraded is False
        assert result.is_degraded is False
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
        # This is a DEGRADED collapse (judge failed), not a near-tie — and the
        # whole bracket degraded, so the result is loudly flagged.
        assert result.comparisons[0].degraded is True
        assert result.fully_degraded is True
        assert result.is_degraded is True


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
            "degraded": False,
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
        assert d["agreed_pairs"] == 1
        assert d["degraded_pairs"] == 0
        assert d["fully_degraded"] is False
        assert "warning" not in d  # clean run carries no degraded marker
        assert "big transcript body" not in str(d)


# ---------------------------------------------------------------------------
# Comparison structural invariants (JudgeVerdict-style __post_init__ guards).
# ---------------------------------------------------------------------------


class TestComparisonInvariants:
    def test_winner_must_be_one_of_the_seeds(self):
        with pytest.raises(ValueError, match="must be the higher_seed"):
            Comparison("hi", "lo", "stranger", True, "bogus")

    def test_unagreed_must_keep_higher_seed(self):
        # agreed=False but winner is the LOWER seed — the deterministic tiebreak
        # contract says the higher seed must be kept. Must crash at construction.
        with pytest.raises(ValueError, match="must keep the higher seed"):
            Comparison("hi", "lo", "lo", False, "violates tiebreak")

    def test_agreed_lower_seed_is_allowed(self):
        # agreed=True (a real model decision) MAY pick the lower seed.
        c = Comparison("hi", "lo", "lo", True, "lower won on merit")
        assert c.winner == "lo"


# ---------------------------------------------------------------------------
# Partial abstention: one order usable, the other not -> degraded ambiguity.
# ---------------------------------------------------------------------------


class TestPartialAbstention:
    def test_one_order_valid_one_unparseable_is_degraded_keep_higher(self):
        # order1 returns a clean vote; order2 is garbage (None). A single valid
        # vote must NOT decide the pair — both-orders agreement is required.
        inv = SequenceJudge(['{"winner": "A"}', "the model rambled, no json"])
        cands = [Candidate("hi", "alpha"), Candidate("lo", "bravo")]
        result = evaluate_tournament(RUBRIC, cands, invoker=inv)
        assert result.winner.label == "hi"  # higher seed kept
        c = result.comparisons[0]
        assert c.agreed is False
        assert c.degraded is True  # the unusable order makes it a degraded collapse
        assert result.is_degraded is True

    def test_one_order_valid_for_challenger_one_errors_still_keeps_higher(self):
        # order1 favours the CHALLENGER (lower seed); order2 errors. The lone
        # valid vote for the challenger does not win — degraded -> keep higher.
        inv = SequenceJudge(['{"winner": "B"}', "__ERROR__"])
        cands = [Candidate("hi", "alpha"), Candidate("lo", "bravo")]
        result = evaluate_tournament(RUBRIC, cands, invoker=inv)
        assert result.winner.label == "hi"
        assert result.comparisons[0].degraded is True


# ---------------------------------------------------------------------------
# Swap-then-defend: a non-seed-0 incumbent must carry forward and defend.
# ---------------------------------------------------------------------------


class TestSwapThenDefend:
    def test_incumbent_changes_then_defends_then_loses(self):
        # Seed order [mid, strong, weak, strongest]:
        #   p1: mid    vs strong    -> strong  (incumbent changes, seed!=0)
        #   p2: strong vs weak      -> strong  (new incumbent DEFENDS)
        #   p3: strong vs strongest -> strongest (incumbent loses)
        RANK4 = {"mid": 2, "strong": 3, "weak": 1, "strongest": 4}
        inv = ContentJudge(lambda t: RANK4[t.strip()])
        cands = [
            Candidate("mid", "mid"),
            Candidate("strong", "strong"),
            Candidate("weak", "weak"),
            Candidate("strongest", "strongest"),
        ]
        result = evaluate_tournament(RUBRIC, cands, invoker=inv)
        assert result.winner.label == "strongest"
        assert len(result.comparisons) == 3  # N-1
        # The middle comparison is strong (incumbent) defending against weak.
        defend = result.comparisons[1]
        assert defend.higher_seed == "strong" and defend.winner == "strong"
        assert all(c.agreed for c in result.comparisons)


# ---------------------------------------------------------------------------
# Mixed gauntlet + degraded aggregates + fallback surfacing.
# ---------------------------------------------------------------------------


class TestDegradedAggregates:
    def test_mixed_gauntlet_counts_agreed_and_degraded(self):
        # 3 candidates: p1 decisive (agreed), p2 degraded (one order errors).
        # ContentJudge would decide both; instead drive raw responses so p2
        # degrades. Order of calls: p1.o1, p1.o2, p2.o1, p2.o2.
        inv = SequenceJudge([
            '{"winner": "B"}',   # p1.o1: a(A) vs b(B) -> b
            '{"winner": "A"}',   # p1.o2: b(A) vs a(B) -> b  (agree -> b wins)
            '{"winner": "A"}',   # p2.o1: b(A) vs c(B) -> b
            "__ERROR__",         # p2.o2: errors -> degraded -> keep higher (b)
        ])
        cands = [Candidate("a", "alpha"), Candidate("b", "bravo"), Candidate("c", "charlie")]
        result = evaluate_tournament(RUBRIC, cands, invoker=inv)
        assert result.winner.label == "b"
        assert result.agreed_pairs == 1
        assert result.degraded_pairs == 1
        assert result.fully_degraded is False  # not EVERY pair degraded
        assert result.is_degraded is True
        d = result.to_dict()
        assert TOURNAMENT_DEGRADED_MARKER in d["warning"]
        assert "1 pair(s) collapsed" in d["warning"]

    def test_fully_degraded_marker_says_seed_order(self):
        inv = ExplodingJudge()
        cands = [Candidate("first", "alpha"), Candidate("second", "bravo")]
        result = evaluate_tournament(RUBRIC, cands, invoker=inv)
        d = result.to_dict()
        assert d["fully_degraded"] is True
        assert TOURNAMENT_DEGRADED_MARKER in d["warning"]
        assert "SEED ORDER" in d["warning"]

    def test_fallback_grading_surfaced(self):
        inv = FallbackJudge('{"winner": "A"}')
        cands = [Candidate("hi", "alpha"), Candidate("lo", "bravo")]
        result = evaluate_tournament(RUBRIC, cands, invoker=inv)
        assert result.fallback_used is True
        assert result.is_degraded is True  # fallback alone flags degradation
        d = result.to_dict()
        assert d["fallback_used"] is True
        assert "local fallback" in d["warning"]


# ---------------------------------------------------------------------------
# CLI: `clawpm judge tournament` validation + wiring (the glue with its own
# validation logic that lives nowhere else).
# ---------------------------------------------------------------------------


def _cli_text(res) -> str:
    """Combined stdout+stderr, robust across Click versions (errors print to
    stderr; older Click mixes it into output, newer Click separates it)."""
    out = res.output or ""
    try:
        out += res.stderr or ""
    except ValueError:
        pass  # stderr was mixed into output already
    return out


class TestCli:
    def _write(self, tmp_path, name, body):
        p = tmp_path / name
        p.write_text(body, encoding="utf-8")
        return str(p)

    def test_no_candidates_errors(self, tmp_path):
        rubric = self._write(tmp_path, "rubric.md", "1. tests pass")
        res = CliRunner().invoke(main, ["judge", "tournament", "--rubric-file", rubric])
        assert res.exit_code == 1
        assert "no_candidates" in _cli_text(res)

    def test_label_mismatch_errors(self, tmp_path):
        rubric = self._write(tmp_path, "rubric.md", "1. tests pass")
        a = self._write(tmp_path, "a.txt", "alpha")
        b = self._write(tmp_path, "b.txt", "bravo")
        res = CliRunner().invoke(main, [
            "judge", "tournament", "--rubric-file", rubric,
            "--candidate", a, "--candidate", b, "--label", "only-one",
        ])
        assert res.exit_code == 1
        assert "label_mismatch" in _cli_text(res)

    def test_empty_rubric_errors(self, tmp_path):
        rubric = self._write(tmp_path, "rubric.md", "   \n  ")
        a = self._write(tmp_path, "a.txt", "alpha")
        res = CliRunner().invoke(main, [
            "judge", "tournament", "--rubric-file", rubric, "--candidate", a,
        ])
        assert res.exit_code == 1
        assert "empty_rubric" in _cli_text(res)

    def test_duplicate_labels_surface_as_clean_error(self, tmp_path):
        # Two files with the same stem -> duplicate default labels -> the
        # evaluate_tournament ValueError must surface as a structured CLI error,
        # not a traceback.
        rubric = self._write(tmp_path, "rubric.md", "1. tests pass")
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        a = d1 / "cand.txt"
        b = d2 / "cand.txt"
        a.write_text("alpha", encoding="utf-8")
        b.write_text("bravo", encoding="utf-8")
        res = CliRunner().invoke(main, [
            "judge", "tournament", "--rubric-file", rubric,
            "--candidate", str(a), "--candidate", str(b),
        ])
        assert res.exit_code == 1
        text = _cli_text(res)
        assert "tournament_failed" in text
        assert "unique" in text

    def test_single_candidate_short_circuit_emits_winner_json(self, tmp_path):
        # One candidate needs no judge at all -> clean JSON winner.
        rubric = self._write(tmp_path, "rubric.md", "1. tests pass")
        a = self._write(tmp_path, "solo.txt", "alpha")
        res = CliRunner().invoke(main, [
            "judge", "tournament", "--rubric-file", rubric, "--candidate", a,
        ])
        assert res.exit_code == 0
        payload = json.loads(res.output)
        assert payload["status"] == "ok"
        assert payload["data"]["winner"] == "solo"
        assert payload["data"]["decided_pairs"] == 0
