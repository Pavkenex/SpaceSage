"""The AI layer: suggestions for the rows the rules cannot decide.

Optional by design.  Nothing in this package runs unless the user configured a
provider in ``ai.toml`` (or pointed the app at a local server), and everything it
produces is a *suggestion*: text that has passed a validator, that the plan
generator may turn into an action, and that the user still has to approve.

The modules, in the order the pipeline uses them:

``config``       providers (ollama / lmstudio / openai / openrouter / custom),
                 the ``ai.toml`` file, local-only mode, per-provider pricing.
``client``       one OpenAI-compatible transport over stdlib ``urllib``:
                 ``POST /chat/completions`` (SSE streaming) and ``GET /models``,
                 with retries, timeouts and the error taxonomy.
``prompts``      the bounded use cases - suggest, classify, explain, review,
                 summarize - each with its own prompt, JSON schema, parser and
                 injection-resistant data wrapping.
``guardrails``   the distrust: JSON extraction, schema validation with one repair
                 retry, dataset locking, the answer cache, the cost meter and
                 path redaction.
``runner``       bounded batches for long lists, with progress, cache counts and
                 a pre-flight cost estimate.
``engine``       the object the CLI and the GUI talk to; every method returns an
                 outcome (never an exception for the failures a user can fix).
``promote``      writing an accepted verdict into the user's rule pack.

Quick start::

    from spacesage.ai import AIEngine

    engine = AIEngine()                     # a disabled engine is a valid engine
    if engine.status().ready:
        outcome = engine.suggest(items)     # bounded batches, cached, metered
        for suggestion in outcome.suggestions:
            ...
"""

from __future__ import annotations

from spacesage.ai.client import (
    AIClient,
    ChatResult,
    Message,
    ModelInfo,
    Usage,
    http_opener,
    messages_from_dicts,
    parse_models,
)
from spacesage.ai.config import (
    PRESETS,
    AIConfig,
    ProviderConfig,
    ProviderPreset,
    default_cache_dir,
    default_config_path,
    enforce_local_only,
    is_loopback,
    validate_base_url,
)
from spacesage.ai.engine import DEFAULT_BATCH_SIZE, DEFAULT_MAX_ITEMS, AIEngine, default_engine
from spacesage.ai.errors import (
    AUTH,
    BAD_RESPONSE,
    BLOCKED_PATH,
    CANCELLED,
    DISABLED,
    DISABLED_CODES,
    INVALID_CONFIG,
    LOCAL_ONLY,
    MODEL_MISSING,
    RATE_LIMITED,
    REPAIR_FAILED,
    RETRYABLE_CODES,
    SCHEMA,
    SERVER_ERROR,
    TIMEOUT,
    UNREACHABLE,
    AIError,
)
from spacesage.ai.guardrails import (
    CACHE_SCHEMA,
    TOKENS_PER_MILLION,
    CacheEntry,
    CacheStats,
    CostEstimate,
    IndexDataset,
    Meter,
    MeterSnapshot,
    ResponseCache,
    ScopedDataset,
    assert_redacted,
    estimate_batch,
    leaks,
    locked,
    read_answer,
    redactor,
    validate,
)
from spacesage.ai.promote import (
    DEFAULT_CONFIDENCE_FLOOR,
    PACK_ID,
    PromotionError,
    PromotionResult,
    RuleDraft,
    draft_from_classification,
    draft_from_suggestion,
    write_rules,
)
from spacesage.ai.prompts import (
    DATA_CLOSE,
    DATA_OPEN,
    Annotation,
    Classification,
    Explanation,
    ItemFacts,
    PathRedactor,
    PlanSummary,
    ProseStreamer,
    Suggestion,
    UseCase,
    build_messages,
    build_payload,
    facts_from_opportunity,
    parse_annotations,
    parse_classifications,
    parse_explanation,
    parse_plan_summary,
    parse_suggestions,
    use_case,
)
from spacesage.ai.results import (
    AIStatus,
    BatchFailure,
    BatchPlan,
    BatchProgress,
    BatchRun,
    CheckResult,
    ClassifyOutcome,
    ExplainOutcome,
    Outcome,
    ReviewOutcome,
    SuggestOutcome,
    SummarizeOutcome,
)
from spacesage.ai.runner import STOP_CODES, STOP_LABELS, BatchExecution, BatchRunner

__all__ = [
    "AUTH",
    "BAD_RESPONSE",
    "BLOCKED_PATH",
    # guardrails
    "CACHE_SCHEMA",
    "CANCELLED",
    # prompts
    "DATA_CLOSE",
    "DATA_OPEN",
    "DEFAULT_BATCH_SIZE",
    # promotion
    "DEFAULT_CONFIDENCE_FLOOR",
    "DEFAULT_MAX_ITEMS",
    "DISABLED",
    "DISABLED_CODES",
    "INVALID_CONFIG",
    "LOCAL_ONLY",
    "MODEL_MISSING",
    "PACK_ID",
    # config
    "PRESETS",
    "RATE_LIMITED",
    "REPAIR_FAILED",
    "RETRYABLE_CODES",
    "SCHEMA",
    "SERVER_ERROR",
    "STOP_CODES",
    "STOP_LABELS",
    "TIMEOUT",
    "TOKENS_PER_MILLION",
    "UNREACHABLE",
    # client
    "AIClient",
    "AIConfig",
    # engine + runner
    "AIEngine",
    # errors
    "AIError",
    # outcomes
    "AIStatus",
    "Annotation",
    "BatchExecution",
    "BatchFailure",
    "BatchPlan",
    "BatchProgress",
    "BatchRun",
    "BatchRunner",
    "CacheEntry",
    "CacheStats",
    "ChatResult",
    "CheckResult",
    "Classification",
    "ClassifyOutcome",
    "CostEstimate",
    "ExplainOutcome",
    "Explanation",
    "IndexDataset",
    "ItemFacts",
    "Message",
    "Meter",
    "MeterSnapshot",
    "ModelInfo",
    "Outcome",
    "PathRedactor",
    "PlanSummary",
    "PromotionError",
    "PromotionResult",
    "ProseStreamer",
    "ProviderConfig",
    "ProviderPreset",
    "ResponseCache",
    "ReviewOutcome",
    "RuleDraft",
    "ScopedDataset",
    "SuggestOutcome",
    "Suggestion",
    "SummarizeOutcome",
    "Usage",
    "UseCase",
    "assert_redacted",
    "build_messages",
    "build_payload",
    "default_cache_dir",
    "default_config_path",
    "default_engine",
    "draft_from_classification",
    "draft_from_suggestion",
    "enforce_local_only",
    "estimate_batch",
    "facts_from_opportunity",
    "http_opener",
    "is_loopback",
    "leaks",
    "locked",
    "messages_from_dicts",
    "parse_annotations",
    "parse_classifications",
    "parse_explanation",
    "parse_models",
    "parse_plan_summary",
    "parse_suggestions",
    "read_answer",
    "redactor",
    "use_case",
    "validate",
    "validate_base_url",
    "write_rules",
]
