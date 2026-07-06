"""Domain service layer (CLAWP-077).

Orchestration that composes the low-level domain primitives (``clawpm.tasks``,
``clawpm.worklog``, ``clawpm.reflect`` …) into the higher-level operations the
CLI performs — and that the MCP server (CLAWP-068) will perform directly,
without the click command layer or a subprocess. Services are click-free: they
take a portfolio ``config`` and plain arguments, return structured results, and
raise only domain exceptions. Presentation (output formatting, exit codes, the
``_mutation_errors`` mapping) stays in ``clawpm.cli``.
"""
