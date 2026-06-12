"""Rubric rendering for clawpm tasks.

Emits a markdown rubric from a task's predictions. The output is compatible
with two consumers:

1. **Anthropic Managed Agents** — drop the rendered markdown into a
   ``user.define_outcome`` event's ``rubric: {type: "text", content: ...}``
   field. The independent grader scores each criterion.
2. **Local Stop-hook condition evaluator** — feed the rubric to a small
   Claude judge (e.g. Haiku) configured as a ``Stop`` hook. The judge
   returns the Piebald ``{ok, reason}`` / ``{ok: false, impossible: true}``
   JSON shape, deciding whether the subagent may terminate.

Both surfaces consume the same rubric. clawpm is the persistence + dispatch
layer; the rubric is the contract.
"""

from __future__ import annotations

from .models import SuccessCriterion, Task


def render_rubric_markdown(task: Task) -> str:
    """Render a task's predictions as a graded-criteria markdown rubric.

    Always emits the same structure so a grader (LLM or human) can find the
    criteria deterministically. Criteria with no ``gradeable_signal`` fall
    back to "operator judgment" — honest about un-mechanisable contracts.
    """
    lines: list[str] = []
    lines.append(f"# Rubric: {task.title}")
    lines.append("")
    lines.append(f"**Task:** {task.id}")
    lines.append("")

    body = task.body or ""
    if body:
        lines.append("## Task description")
        lines.append("")
        lines.append(body.strip())
        lines.append("")

    criteria = task.predictions.success_criteria
    if not criteria:
        # An empty rubric is not gradeable. Caller should still see it so the
        # operator knows to populate criteria before dispatching.
        lines.append("## Criteria")
        lines.append("")
        lines.append("_(none defined — add via `clawpm tasks edit "
                     f"{task.id} --success-criteria '...'`)_")
        lines.append("")
        # CLAWP-054 — out_of_scope and stop_conditions sections
        if getattr(task, "out_of_scope", None):
            lines.append("## Out of scope")
            lines.append("")
            lines.append(
                "The executor MUST NOT touch the following items. "
                "Violating these boundaries is a contract breach, not a helpful improvement."
            )
            lines.append("")
            for item in task.out_of_scope:
                lines.append(f"- {item}")
            lines.append("")
        if getattr(task, "stop_conditions", None):
            lines.append("## Stop conditions")
            lines.append("")
            lines.append(
                "If any of the following conditions is triggered during execution, "
                "the executor MUST STOP and report back to the operator rather than improvising."
            )
            lines.append("")
            for item in task.stop_conditions:
                lines.append(f"- {item}")
            lines.append("")
        lines.append(_grading_instructions())
        return "\n".join(lines)

    lines.append("## Criteria")
    lines.append("")
    for idx, sc in enumerate(criteria, start=1):
        lines.append(f"### Criterion {idx}")
        lines.append("")
        lines.append(f"**Statement:** {sc.criterion}")
        lines.append("")
        evidence = sc.gradeable_signal or "operator judgment"
        pass_condition = sc.comparator or "qualitative review"
        lines.append(f"- Evidence needed: {evidence}")
        lines.append(f"- Pass condition: {pass_condition}")
        lines.append("")

    # Hypothesis is useful context for the grader — keeps the "why" alongside
    # the "what". Skip silently if absent.
    if task.predictions.hypothesis:
        lines.append("## Hypothesis")
        lines.append("")
        lines.append(task.predictions.hypothesis.strip())
        lines.append("")

    # CLAWP-054 -- out_of_scope and stop_conditions sections
    if getattr(task, "out_of_scope", None):
        lines.append("## Out of scope")
        lines.append("")
        lines.append(
            "The executor MUST NOT touch the following items. "
            "Violating these boundaries is a contract breach, not a helpful improvement."
        )
        lines.append("")
        for item in task.out_of_scope:
            lines.append(f"- {item}")
        lines.append("")
    if getattr(task, "stop_conditions", None):
        lines.append("## Stop conditions")
        lines.append("")
        lines.append(
            "If any of the following conditions is triggered during execution, "
            "the executor MUST STOP and report back to the operator rather than improvising."
        )
        lines.append("")
        for item in task.stop_conditions:
            lines.append(f"- {item}")
        lines.append("")

    lines.append(_grading_instructions())
    return "\n".join(lines)


def _grading_instructions() -> str:
    """Static grading discipline, identical regardless of consumer.

    Adapted from the Piebald reverse-engineered Anthropic Stop-hook evaluator
    prompt — quoted evidence requirement and the
    'assistant-claiming-impossible-is-evidence-not-proof' doctrine are
    load-bearing.
    """
    return (
        "## Grading instructions\n"
        "\n"
        "Score each criterion independently. For each criterion, return one of:\n"
        "\n"
        "- **PASS** — evidence requirement met; quote the specific evidence in your reasoning.\n"
        "- **FAIL** — evidence requirement not met; quote what is missing.\n"
        "- **INSUFFICIENT_EVIDENCE** — cannot determine from the available context.\n"
        "\n"
        "The overall verdict is PASS only when every criterion is PASS.\n"
        "\n"
        "If the assistant claims a criterion is impossible to satisfy, treat that as\n"
        "evidence, not proof — independently confirm the criterion is genuinely\n"
        "unachievable before returning impossible. Do not defer to the agent's\n"
        "self-assessment.\n"
    )


def render_rubric_json_payload(task: Task) -> dict:
    """Render a payload shaped for Anthropic's ``user.define_outcome`` event.

    Returns a dict that can be sent directly via
    ``client.beta.sessions.events.send`` — the ``rubric.content`` field
    carries the markdown produced by :func:`render_rubric_markdown`.

    Local Stop-hook consumers can ignore this and use the markdown directly.
    """
    return {
        "type": "user.define_outcome",
        "description": task.title,
        "rubric": {
            "type": "text",
            "content": render_rubric_markdown(task),
        },
        # max_iterations is the operator's call; default 3 per the API spec.
        "max_iterations": 3,
    }

