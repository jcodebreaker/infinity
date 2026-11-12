"""Eval target support (ADR 0043, docs/evals.md): what an external eval service needs from the
engine to play as a fresh person per run.

``identity`` (who an eval person is, and the context-record env their hosts get) and ``people``
(the registry of eval persons, their release, and the 7-day sweep). The evals themselves live in
evalution.
"""
