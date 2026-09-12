# Research: AI vs. deterministic systems for storage cleanup suggestions

**Date:** 2026-09-12 · **Question:** *"Research if adding an AI with OpenAI-compatible [API] would be good for those suggestions, or if some other system would work."*

## TL;DR — verdict

**Hybrid architecture, with the AI as an optional assist layer — not as the decision core.**

- The **deterministic core** (rule packs + heuristics + exact hashing) makes every decision, computes every number, and owns all safety gates.
- An **optional OpenAI-compatible AI layer** adds real value on the long tail: classifying ambiguous/unknown paths, narrating the plan in plain language, reviewing the plan for overlooked risks, and answering questions about the analysis.
- **OpenAI-compatible** is the right interface because one protocol spans cloud providers (OpenAI, OpenRouter, …) *and* local runtimes (Ollama, LM Studio, vLLM, llama.cpp server) — so users can keep file paths on their own machine while still "having AI".
- The "other system that works" is already the core: **rule packs**. A second non-LLM system worth building later: a **local preference model** learned from the user's own accept/reject decisions (no AI service involved).

## Why not pure-AI

1. **Scale.** A WizTree export is 1–20M rows; no context window holds it. An LLM can only ever see aggregates and samples — so a deterministic pipeline must do candidate selection first anyway. (This is the architecture insight: AI *cannot* be the first stage even if you wanted it to.)
2. **Determinism & auditability.** The same disk should produce the same advice tomorrow. "Why did you suggest deleting X?" must be answerable with a rule id and a rationale, not a sampled token path.
3. **Hallucination.** LLMs can invent paths, mis-sum sizes, or propose deleting something they don't understand. All arithmetic must be computed, never generated.
4. **Prompt injection.** Filenames are attacker-controlled input (a file named `ignore previous instructions…` is trivially constructible). AI output must be schema-constrained and cross-checked against the dataset.
5. **Privacy.** File paths reveal a person's life. Default-remote LLM = data leaves the machine; fine for some, unacceptable for many. Local runtimes solve this — but only if the app is designed for them.
6. **Cost & availability.** Analysis must work offline, free, and instantly; AI calls are optional garnish.

## Why not pure-rules either

Rule packs reliably cover ~80–90% (temp, caches, installers, known dev paths, obvious media). The remaining tail — unknown folders, unusual app layouts, ambiguous `Cache` dirs that actually hold user data, per-user questions ("do I still need this?") — can't be enumerated by hand. Narrative output ("here's what's happening and the plan in plain English") is also exactly what language models are for. That tail and the narration are where AI pays for itself.

## Options compared

| Option | Determinism | Privacy | Cost | Long tail | Verdict |
|---|---|---|---|---|---|
| Rule packs + heuristics | perfect | local | free | weak | **core** |
| Exact hashing (duplicates) | perfect | local | free | n/a — pure math | **core** |
| Local preference learning (v2) | high | local | free | medium | later |
| Cloud LLM (OpenAI-compatible) | low | data leaves machine | $$ | strong | optional |
| Local LLM (Ollama/LM Studio — same API) | low | stays local | free | strong (smaller models) | optional |

## Adopted architecture — ADR-0001 (summary)

- Decisions, estimates, ordering, and safety: **rules + engine only**.
- AI does exactly four bounded things: **classify** ambiguous entries (batch, schema-validated, dataset-locked), **narrate** the report/plan, **review** a plan for risks (annotations only), **answer** questions over aggregate stats.
- One protocol: OpenAI-compatible `/chat/completions`; presets for local runtimes; `redact_paths` option; response caching; token meter; **off by default**; graceful fallback when unreachable.
- AI output can never create an executable action by itself — it enters the same validation + approval pipeline as rule-produced items.

## Prior art surveyed

- **CH-ZHOU-0512/wiztree-disk-cleaner** (MIT, PowerShell "Agent Skill"): WizTree CSV → 3-tier Markdown report → approval-manifest-bound dry-run plan → gated execution with audit log. *Concepts adopted:* tiering (T1/T2/T3), approval manifest + plan_id binding, dry-run default, path re-validation before execution, machine-scope rule (don't act on another machine's export).
- **unfyt/ai-disk-cleanup ("DiskContext")** (Python, zero-dep): scans a disk, emits a context-rich text report designed to be pasted into an LLM, with SAFE→HIGH_RISK labels and a duplicate finder. *Validates:* LLM-as-advisor + zero-dependency packaging. *Misses:* structured plan, move/link strategy, execution, undo.
- **vudsen/ai-disk-cleaner** (Go, GUI): AI-powered cleaner with 300+ stars — demand for the category is real.

**Differentiators for SpaceSage:** WizTree-native ingest (no re-scan) at scale via SQLite; a *complete* course of action (delete **+ move + junction/symlink + native tools**), not just deletion; executable, undoable plan; pluggable/local AI with hard guardrails.

## Sources

- WizTree CSV contract — `diskanalyzer.com/guide` (export section, command-line export) + community reference (`wiztree-csv.md`, CH-ZHOU-0512 repo): columns `File Name, Size, Allocated, Modified, Attributes, Files, Folders`; folder rows include descendants (never sum them); leading-zero `Allocated` = hard link (no extra space); parse by column name; tolerate extra columns.
- Prior-art repos above (GitHub, surveyed 2026-09-12).
