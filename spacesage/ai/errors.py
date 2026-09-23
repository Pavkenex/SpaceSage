"""The AI layer's error taxonomy: one exception type, one code per failure kind.

Every failure the AI layer can produce is an :class:`AIError` carrying a
machine-readable ``code`` (``auth``, ``model_missing``, ``unreachable``,
``rate_limited``, ``bad_response``, ...), a one-line actionable ``hint`` for the
user, and - when a server answered - the HTTP status.  The GUI turns ``code``
into an inline state, the CLI into an exit code plus the hint, and tests assert
on the code instead of on a message string.
"""

from __future__ import annotations

from typing import Any

# --- failure codes (the actionable taxonomy) ------------------------------- #

AUTH = "auth"
"""The provider rejected the credentials (HTTP 401/403, or a missing key)."""

MODEL_MISSING = "model_missing"
"""The provider does not serve the configured model (HTTP 404 ``model_not_found``)."""

UNREACHABLE = "unreachable"
"""No answer at all: connection refused, DNS failure, a refused redirect."""

TIMEOUT = "timeout"
"""The provider did not answer (or stopped streaming) inside the deadline."""

RATE_LIMITED = "rate_limited"
"""HTTP 429, or a provider-specific quota answer."""

SERVER_ERROR = "server_error"
"""HTTP 5xx: the provider is reachable but broken.  Retryable."""

BAD_RESPONSE = "bad_response"
"""An answer arrived that is not an OpenAI-compatible chat completion."""

TRUNCATED = "truncated"
"""The completion hit ``max_tokens`` before the model answered (``finish_reason=length``).

A model that reasons before answering spends the completion budget on the
reasoning pass first; when that exhausts the ceiling there is no answer left.
The ceiling is per use case (``prompts.USE_CASES``)."""

SCHEMA = "schema"
"""The model's answer did not satisfy the use case's JSON schema."""

REPAIR_FAILED = "repair_failed"
"""The one repair retry was also invalid; the raw answer is kept for the log."""

BLOCKED_PATH = "blocked_path"
"""The answer referenced a path that is not part of the locked dataset."""

LOCAL_ONLY = "local_only"
"""Local-only mode is on and the endpoint is not loopback."""

DISABLED = "disabled"
"""The AI layer is off: no provider configured, or the user disabled it."""

INVALID_CONFIG = "invalid_config"
"""The provider configuration itself is wrong (bad URL, no model, ...)."""

CANCELLED = "cancelled"
"""The caller cancelled the run (the UI's Cancel button)."""

DISABLED_CODES: frozenset[str] = frozenset({DISABLED})

RETRYABLE_CODES: frozenset[str] = frozenset({RATE_LIMITED, UNREACHABLE, TIMEOUT, SERVER_ERROR})
"""Codes that are worth another attempt (everything else fails immediately)."""


class AIError(RuntimeError):
    """A failure with a stable code, a hint and - optionally - an HTTP status."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str = "",
        status: int | None = None,
        provider: str | None = None,
        retryable: bool | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.status = status
        self.provider = provider
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable
        self.detail: dict[str, Any] = dict(detail or {})

    def __str__(self) -> str:
        """``auth: the provider rejected the key - run `spacesage ai check` ...``"""
        if self.hint:
            return f"{self.code}: {self.message} - {self.hint}"
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view (the GUI's inline error state, the CLI's ``--json``)."""
        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "status": self.status,
            "provider": self.provider,
            "retryable": self.retryable,
        }
