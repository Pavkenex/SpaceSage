# ADR-0001: Deterministic rules core with an optional OpenAI-compatible AI assist layer

**Status:** accepted · **Date:** 2026-09-12

## Context

The product must turn a WizTree export (1–20M rows) into a course of action that a user can safely execute. Advice must be explainable and reproducible; mistakes can destroy data. Users vary in privacy tolerance and willingness to configure/pay for AI.

## Decision

1. All decisions, size arithmetic, scoring, ordering, and safety gates live in a **deterministic core** (rule packs, heuristics, hashing). No model ever computes numbers or creates an executable action by itself.
2. An **optional AI layer** — speaking the OpenAI-compatible `/chat/completions` protocol so it works with cloud providers and local runtimes (Ollama, LM Studio, vLLM) alike — provides four bounded capabilities: classify ambiguous entries, narrate the report, review a plan for risks, answer questions over aggregates.
3. AI outputs are schema-validated, dataset-locked (paths must exist in the index), cached, and routed through the same approval pipeline. AI is off by default and degrades gracefully to deterministic output.

## Consequences

- Works fully offline and free; AI is pure upside when configured.
- Reproducible, auditable advice ("rule X, rationale Y").
- Two code paths to maintain for AI-assisted features, guarded by a stub-server test suite.
- Rejected alternatives: pure heuristics (fails the long tail), pure LLM (unsafe, non-reproducible, cannot see the dataset), remote-only AI (privacy), proprietary SDKs (lock-in; plain HTTP is enough).
