"""LLM-judge components for clawpm.

The judges are small, dispatched LLM calls used to enforce contracts at
machine boundaries — the most important being the Stop-hook condition
evaluator that decides whether a subagent has actually satisfied its
success-criteria rubric.

See ``judges.stop_condition`` for the canonical implementation.
"""
