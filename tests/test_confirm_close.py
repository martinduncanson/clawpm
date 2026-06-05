"""Tests for the adversarial confirm-close tier (CLAWP-041).

The base judge fail-closes against *malformed* output; its remaining failure
mode is fail-OPEN via over-charitable reading (passes a criterion the
transcript claims but does not evidence). The confirm-close tier spends an
adversarial refutation pass ONLY on the ok=true->close transition.

Adversarial coverage:
  - Over-charitable base pass is overturned by a refuter that spots the
    unevidenced criterion (the headline regression).
  - The block path (base ok=false) costs exactly one judge call — the refuter
    is never invoked.
  - An impossible base verdict passes straight through (no refutation).
  - A close that genuinely survives refutation stays ok=true.
  - Majority threshold over spawned votes; malformed refuter output counts as
    a refutation (bias toward keeping the task open).
  - A refuter-invoker error abstains rather than trapping the agent forever.
"""

from __future__ import annotations

from clawpm.judges.stop_condition import (
    JudgeVerdict,
    build_judge_prompt,
    build_refutation_prompt,
    evaluate_stop_condition_confirmed,
)


# ---------------------------------------------------------------------------
# A scripted, call-counting invoker. Distinguishes the base-judge prompt from
# the refutation prompt by content so one stub can drive both phases.
# ---------------------------------------------------------------------------


class ScriptedInvoker:
    """Returns canned JSON keyed on whether the prompt is base vs refute.

    ``base_response`` is returned for the base-judge prompt; ``refute_responses``
    is a list consumed one-per-refutation-call (cycling the last entry if the
    list is exhausted). Records every prompt for assertion.
    """

    def __init__(self, base_response: str, refute_responses=None, refute_error=False):
        self.base_response = base_response
        self.refute_responses = list(refute_responses or [])
        self.refute_error = refute_error
        self.prompts: list[str] = []
        self.base_calls = 0
        self.refute_calls = 0

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        # The refutation prompt is unmistakable — it opens with the skeptical
        # verifier framing. The base prompt opens with the stop-condition hook
        # framing. Match on those stable markers.
        if "SKEPTICAL verifier" in prompt:
            self.refute_calls += 1
            if self.refute_error:
                raise RuntimeError("refuter CLI exploded")
            idx = min(self.refute_calls - 1, len(self.refute_responses) - 1)
            resp = self.refute_responses[idx]
            # Per-vote error sentinel — lets a test mix errored and live votes
            # to exercise the abstention-denominator path.
            if resp == "__ERROR__":
                raise RuntimeError("refuter CLI exploded")
            return resp
        self.base_calls += 1
        return self.base_response


OK_TRUE = '{"ok": true, "reason": "looks done"}'
OK_FALSE = '{"ok": false, "reason": "criterion 2 not evidenced"}'
IMPOSSIBLE = '{"ok": false, "impossible": true, "reason": "rubric self-contradictory"}'

RUBRIC = "1. Tests pass (gradeable_signal: pytest exit 0)\n2. No regressions"
CLAIMS_BUT_NO_EVIDENCE = "I refactored the module and the tests pass now."
HAS_EVIDENCE = "Ran `pytest -q`: 42 passed in 1.2s. Diff shows the fix."


# ---------------------------------------------------------------------------
# Criterion 1: over-charitable base pass is overturned by the refuter.
# ---------------------------------------------------------------------------


class TestOverCharitablePassOverturned:
    def test_claimed_but_unevidenced_criterion_is_refuted(self):
        # Base judge waves the unevidenced "tests pass" through; the refuter
        # catches it. Confirmed verdict must be ok=false.
        inv = ScriptedInvoker(
            base_response=OK_TRUE,
            refute_responses=['{"ok": false, "reason": "no pytest output in transcript for criterion 1"}'],
        )
        verdict = evaluate_stop_condition_confirmed(
            RUBRIC, CLAIMS_BUT_NO_EVIDENCE, invoker=inv, refute_votes=1
        )
        assert verdict.ok is False
        assert verdict.impossible is False
        assert "CONFIRM_CLOSE_REFUTED" in verdict.reason
        assert "no pytest output" in verdict.reason
        # Base ran once, refuter ran once.
        assert inv.base_calls == 1
        assert inv.refute_calls == 1


# ---------------------------------------------------------------------------
# Criterion 2: block path costs exactly one judge call (refuter never runs).
# ---------------------------------------------------------------------------


class TestBlockPathUnchanged:
    def test_base_not_ok_makes_exactly_one_call(self):
        inv = ScriptedInvoker(base_response=OK_FALSE)
        verdict = evaluate_stop_condition_confirmed(
            RUBRIC, CLAIMS_BUT_NO_EVIDENCE, invoker=inv, refute_votes=3
        )
        assert verdict.ok is False
        assert verdict.reason == "criterion 2 not evidenced"
        # The whole point: one call, no refutation despite refute_votes=3.
        assert inv.base_calls == 1
        assert inv.refute_calls == 0
        assert len(inv.prompts) == 1


# ---------------------------------------------------------------------------
# Criterion 3: refuter fires only when base ok=true (and at most once per vote).
# ---------------------------------------------------------------------------


class TestRefuterGatedOnOkTrue:
    def test_refuter_not_called_when_base_not_ok(self):
        inv = ScriptedInvoker(base_response=OK_FALSE, refute_responses=[OK_FALSE])
        evaluate_stop_condition_confirmed(RUBRIC, "anything", invoker=inv, refute_votes=1)
        assert inv.refute_calls == 0
        assert not any("SKEPTICAL verifier" in p for p in inv.prompts)

    def test_impossible_verdict_passes_through_without_refutation(self):
        inv = ScriptedInvoker(base_response=IMPOSSIBLE, refute_responses=[OK_FALSE])
        verdict = evaluate_stop_condition_confirmed(RUBRIC, "anything", invoker=inv)
        assert verdict.ok is False
        assert verdict.impossible is True
        assert inv.refute_calls == 0


# ---------------------------------------------------------------------------
# Surviving close, thresholds, and degraded-judge handling.
# ---------------------------------------------------------------------------


class TestCloseSurvives:
    def test_genuine_close_survives_refutation(self):
        inv = ScriptedInvoker(
            base_response=OK_TRUE,
            refute_responses=['{"ok": true, "reason": "every criterion evidenced; pytest output present"}'],
        )
        verdict = evaluate_stop_condition_confirmed(
            RUBRIC, HAS_EVIDENCE, invoker=inv, refute_votes=1
        )
        assert verdict.ok is True
        assert verdict.reason == "looks done"  # base reason, undecorated
        assert inv.refute_calls == 1


class TestMajorityThreshold:
    def test_two_of_three_refute_overturns(self):
        inv = ScriptedInvoker(
            base_response=OK_TRUE,
            refute_responses=[OK_FALSE, OK_TRUE, OK_FALSE],
        )
        verdict = evaluate_stop_condition_confirmed(
            RUBRIC, CLAIMS_BUT_NO_EVIDENCE, invoker=inv, refute_votes=3
        )
        assert verdict.ok is False
        assert "2/3 refuters" in verdict.reason
        assert inv.refute_calls == 3

    def test_one_of_three_refute_close_stands(self):
        inv = ScriptedInvoker(
            base_response=OK_TRUE,
            refute_responses=[OK_FALSE, OK_TRUE, OK_TRUE],
        )
        verdict = evaluate_stop_condition_confirmed(
            RUBRIC, HAS_EVIDENCE, invoker=inv, refute_votes=3
        )
        assert verdict.ok is True
        assert inv.refute_calls == 3

    def test_live_refutation_not_outvoted_by_errored_refuters(self):
        # The HIGH regression (silent-failure-hunter): with votes=3, two
        # refuters error and the ONE that ran votes to refute. Abstentions must
        # drop from the denominator (effective=1, threshold=1) so the live
        # refutation overturns — dead judges must NOT count as implicit passes.
        inv = ScriptedInvoker(
            base_response=OK_TRUE,
            refute_responses=[OK_FALSE, "__ERROR__", "__ERROR__"],
        )
        verdict = evaluate_stop_condition_confirmed(
            RUBRIC, CLAIMS_BUT_NO_EVIDENCE, invoker=inv, refute_votes=3
        )
        assert verdict.ok is False
        assert "1/1" in verdict.reason  # 1 refutation of 1 effective vote
        assert "CONFIRM_CLOSE_REFUTED" in verdict.reason

    def test_tie_overturns_by_design(self):
        # CONTRACT (Codex round-2): a 1-of-2 split overturns the close — ties
        # refute, a deliberate bias toward keeping the task open on a terminal
        # close. This is NOT a strict >50% majority; if someone "fixes" the
        # threshold to effective//2+1 this test must fail.
        inv = ScriptedInvoker(
            base_response=OK_TRUE,
            refute_responses=[OK_FALSE, OK_TRUE],
        )
        verdict = evaluate_stop_condition_confirmed(
            RUBRIC, CLAIMS_BUT_NO_EVIDENCE, invoker=inv, refute_votes=2
        )
        assert verdict.ok is False
        assert "1/2" in verdict.reason

    def test_malformed_refuter_output_counts_as_refutation(self):
        # Parse failure defaults to ok=false -> counts as a refutation. A
        # garbage refuter response biases toward keeping the task open, never
        # toward a false close.
        inv = ScriptedInvoker(
            base_response=OK_TRUE,
            refute_responses=["not json at all, the model rambled"],
        )
        verdict = evaluate_stop_condition_confirmed(
            RUBRIC, CLAIMS_BUT_NO_EVIDENCE, invoker=inv, refute_votes=1
        )
        assert verdict.ok is False
        assert "CONFIRM_CLOSE_REFUTED" in verdict.reason


class TestDegradedRefuter:
    def test_refuter_error_abstains_and_close_stands(self):
        # A systematically broken refuter must NOT trap the agent in an
        # infinite block loop — it abstains and the base verdict stands, with
        # the error surfaced in the reason.
        inv = ScriptedInvoker(base_response=OK_TRUE, refute_error=True)
        verdict = evaluate_stop_condition_confirmed(
            RUBRIC, HAS_EVIDENCE, invoker=inv, refute_votes=1
        )
        assert verdict.ok is True
        # Total outage → base stands, but surfaced with the doctor-greppable
        # token so a silently-degraded confirm-close is discoverable.
        assert "CONFIRM_CLOSE_DEGRADED" in verdict.reason
        assert "refuter CLI exploded" in verdict.reason


# ---------------------------------------------------------------------------
# Prompt construction — the refuter must be a genuinely different prompt from
# the base judge, or the votes rubber-stamp.
# ---------------------------------------------------------------------------


class TestPromptDistinct:
    def test_refute_prompt_differs_from_base_prompt(self):
        base = build_judge_prompt(RUBRIC, HAS_EVIDENCE)
        refute = build_refutation_prompt(RUBRIC, HAS_EVIDENCE, "looks done")
        assert base != refute
        assert "SKEPTICAL verifier" in refute
        assert "REFUTE" in refute

    def test_lens_varies_the_prompt(self):
        a = build_refutation_prompt(RUBRIC, HAS_EVIDENCE, "r", lens="evidence")
        b = build_refutation_prompt(RUBRIC, HAS_EVIDENCE, "r", lens="reproduction")
        assert a != b
        assert "EVIDENCE" in a
        assert "REPRODUCTION" in b


# ---------------------------------------------------------------------------
# CLAWP-043: the refuter is BLIND to the base judge's reason by default — the
# anchoring fix. Feeding the refuter the optimistic judge's framing softens
# the prosecution toward agreement, the exact mutual-softening adversarial
# review exists to prevent. The legacy anchored prompt is opt-in.
# ---------------------------------------------------------------------------


class TestRefuterAnchoring:
    def test_prior_reason_absent_from_refute_prompt_by_default(self):
        # Default (blind): the base judge's reason must NOT appear in the
        # refutation prompt — the refuter judges rubric + transcript alone.
        refute = build_refutation_prompt(RUBRIC, HAS_EVIDENCE, "looks done")
        assert "looks done" not in refute
        assert "Prior judge's stated reason" not in refute

    def test_prior_reason_included_when_opted_in(self):
        # include_prior_reason=True restores the legacy anchored prompt for A/B.
        refute = build_refutation_prompt(
            RUBRIC, HAS_EVIDENCE, "looks done", include_prior_reason=True
        )
        assert "looks done" in refute
        assert "Prior judge's stated reason" in refute

    def test_confirmed_eval_keeps_refuter_blind_by_default(self):
        # The end-to-end path must build a blind refutation prompt by default.
        inv = ScriptedInvoker(
            base_response=OK_TRUE,
            refute_responses=['{"ok": true, "reason": "all evidenced"}'],
        )
        evaluate_stop_condition_confirmed(
            RUBRIC, HAS_EVIDENCE, invoker=inv, refute_votes=1
        )
        refute_prompts = [p for p in inv.prompts if "SKEPTICAL verifier" in p]
        assert refute_prompts and all("looks done" not in p for p in refute_prompts)

    def test_env_var_restores_anchored_prompt(self, monkeypatch):
        monkeypatch.setenv("CLAWPM_REFUTER_SEES_PRIOR", "1")
        inv = ScriptedInvoker(
            base_response=OK_TRUE,
            refute_responses=['{"ok": true, "reason": "all evidenced"}'],
        )
        evaluate_stop_condition_confirmed(
            RUBRIC, HAS_EVIDENCE, invoker=inv, refute_votes=1
        )
        refute_prompts = [p for p in inv.prompts if "SKEPTICAL verifier" in p]
        assert refute_prompts and all("looks done" in p for p in refute_prompts)

    def test_explicit_arg_overrides_env(self, monkeypatch):
        # Explicit refuter_sees_prior=False wins over a truthy env var.
        monkeypatch.setenv("CLAWPM_REFUTER_SEES_PRIOR", "1")
        inv = ScriptedInvoker(
            base_response=OK_TRUE,
            refute_responses=['{"ok": true, "reason": "all evidenced"}'],
        )
        evaluate_stop_condition_confirmed(
            RUBRIC, HAS_EVIDENCE, invoker=inv, refute_votes=1,
            refuter_sees_prior=False,
        )
        refute_prompts = [p for p in inv.prompts if "SKEPTICAL verifier" in p]
        assert refute_prompts and all("looks done" not in p for p in refute_prompts)
