"""The OpenAI-compatible client: ``/chat/completions`` (streaming) and ``/models``.

Zero dependencies (stdlib ``urllib`` only, like the rest of the engine), one
protocol for cloud providers and local runtimes alike.  The client knows nothing
about prompts, schemas or caching - it speaks HTTP, retries what is worth
retrying, and turns every failure into an :class:`~spacesage.ai.errors.AIError`
with an actionable code.

Streaming is real SSE: chunks are parsed as they arrive and handed to an
``on_delta`` callback (the details pane's "Explain with AI"), while the full
text is accumulated for the guardrails to validate at the end.  A server that
ignores ``stream: true`` and answers with one JSON body is handled too.

Requests to OpenCode Zen carry the routing headers it requires
(``x-opencode-session`` and ``x-opencode-client``): see
:func:`is_opencode_endpoint`.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from spacesage import __version__
from spacesage.ai.config import ProviderConfig, enforce_local_only
from spacesage.ai.errors import (
    AUTH,
    BAD_RESPONSE,
    CANCELLED,
    INVALID_CONFIG,
    MODEL_MISSING,
    RATE_LIMITED,
    SERVER_ERROR,
    TIMEOUT,
    UNREACHABLE,
    AIError,
)

USER_AGENT = f"spacesage/{__version__}"
"""Sent with every request so a provider's logs can tell where the call came from."""

OPENCODE_HOST = "opencode.ai"
"""Host (and its subdomains) whose requests carry OpenCode Zen's routing headers."""

OPENCODE_CLIENT = "spacesage"
"""``x-opencode-client`` value: names this app in OpenCode Zen's logs."""

OPENCODE_SESSION_HEADER = "x-opencode-session"
"""OpenCode Zen's routing / prompt-cache key: one stable id per conversation."""

OPENCODE_CLIENT_HEADER = "x-opencode-client"
"""OpenCode Zen's client name header."""

MAX_BODY_BYTES = 8 * 1024 * 1024
"""Hard cap on a non-streaming answer (a runaway provider must not exhaust memory)."""

MAX_RETRY_AFTER_S = 30.0
"""Longest ``Retry-After`` the client honours before giving up."""


@dataclass(frozen=True)
class Message:
    """One chat message (``system`` / ``user`` / ``assistant``)."""

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        """The wire form."""
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class Usage:
    """Token accounting for one call (``estimated`` when the server stayed silent)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated": self.estimated,
        }


@dataclass(frozen=True)
class ModelInfo:
    """One entry of ``GET /models``."""

    id: str
    owned_by: str | None = None
    created: int | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view."""
        return {"id": self.id, "owned_by": self.owned_by, "created": self.created}


@dataclass(frozen=True)
class ChatResult:
    """One completed chat call: the text plus how it was produced."""

    text: str
    model: str
    usage: Usage
    latency_s: float
    provider: str
    streamed: bool = False
    finish_reason: str | None = None
    chunks: int = 0

    def to_dict(self, *, chars: int = 400) -> dict[str, object]:
        """JSON-ready view (the text is truncated for logs)."""
        return {
            "model": self.model,
            "provider": self.provider,
            "streamed": self.streamed,
            "finish_reason": self.finish_reason,
            "chunks": self.chunks,
            "latency_s": round(self.latency_s, 3),
            "usage": self.usage.to_dict(),
            "chars": len(self.text),
            "preview": self.text[:chars],
        }


Opener = Callable[[urllib.request.Request, float], Any]
"""``(request, timeout) -> response``; injectable so tests own the socket."""


def http_opener(request: urllib.request.Request, timeout: float) -> Any:
    """The default opener: plain ``urlopen`` with a timeout."""
    return urllib.request.urlopen(request, timeout=timeout)


def is_opencode_endpoint(provider: ProviderConfig) -> bool:
    """True when ``provider`` is OpenCode Zen - by preset kind, or by host.

    OpenCode Zen routes (and prompt-caches) on ``x-opencode-session``, and
    announced that requests without it may be refused.  The check is the ``opencode``
    preset *and* the ``opencode.ai`` host, so a hand-written provider or a
    ``base_url`` override pointed at the gateway still gets the headers.  The host
    is taken with :mod:`urllib.parse` (ports and paths do not matter), and
    ``notopencode.ai`` is not this provider.
    """
    if provider.kind == "opencode":
        return True
    host = provider.host().rstrip(".").lower()
    return host == OPENCODE_HOST or host.endswith(f".{OPENCODE_HOST}")


class AIClient:
    """Speaks to one configured provider."""

    def __init__(
        self,
        provider: ProviderConfig,
        *,
        local_only: bool = False,
        env: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        opener: Opener = http_opener,
        retries: int | None = None,
        retry_backoff_s: float = 0.5,
        timeout_s: float | None = None,
        session_id: str | None = None,
    ) -> None:
        self.provider = provider.validated()
        self.local_only = local_only
        self.env = env
        self.retries = provider_retries(provider, retries)
        self.retry_backoff_s = retry_backoff_s
        self.timeout_s = timeout_s if timeout_s is not None else provider.timeout_s
        self._sleep = sleep
        self._opener = opener
        self.calls = 0
        """Successful HTTP round trips (used by tests and the cost meter)."""
        self.session_id = session_id or uuid.uuid4().hex
        """One id per conversation, sent to OpenCode Zen as ``x-opencode-session``.

        Zen routes (and prompt-caches) on it: stable for this client's lifetime -
        every request in a conversation carries the same value - and new for the
        next client.  Injectable so a test can pin it.
        """

    # -- public API --------------------------------------------------------- #

    def chat(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cancel: Any = None,
    ) -> ChatResult:
        """One non-streaming ``/chat/completions`` call.

        Retries on rate limits, timeouts, unreachable endpoints and 5xx answers;
        every other failure is reported immediately with its code.
        """
        body = self._body(messages, model=model, temperature=temperature, max_tokens=max_tokens)
        text = self._with_retries(
            lambda: self._post_text(body, stream=False, cancel=cancel), cancel=cancel
        )
        payload = self._parse_completion(text)
        return self._result(payload, streamed=False, started=time.monotonic(), chunks=0)

    def chat_stream(
        self,
        messages: Sequence[Message],
        *,
        on_delta: Callable[[str], None] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cancel: Any = None,
    ) -> ChatResult:
        """The same call, read as an SSE stream chunk by chunk."""
        body = self._body(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, stream=True
        )
        started = time.monotonic()
        deltas: list[str] = []
        state: dict[str, Any] = {"finish_reason": None, "model": None, "usage": None, "chunks": 0}
        self._with_retries(
            lambda: self._consume_stream(
                body, deltas=deltas, state=state, on_delta=on_delta, cancel=cancel
            ),
            cancel=cancel,
        )
        text = "".join(deltas)
        usage = _usage_from(state.get("usage"), prompt_chars=_prompt_chars(body), completion=text)
        return ChatResult(
            text=text,
            model=str(state.get("model") or self.provider.model),
            usage=usage,
            latency_s=time.monotonic() - started,
            provider=self.provider.name,
            streamed=True,
            finish_reason=_optional_str(state.get("finish_reason")),
            chunks=int(state.get("chunks", 0)),
        )

    def models(self, *, cancel: Any = None) -> tuple[ModelInfo, ...]:
        """``GET /models`` - the model picker's list, sorted by id."""
        text = self._with_retries(lambda: self._get_text(cancel=cancel), cancel=cancel)
        return parse_models(text)

    # -- request plumbing --------------------------------------------------- #

    def _headers(self, *, stream: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "text/event-stream" if stream else "application/json",
        }
        key = self.provider.api_key(self.env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if is_opencode_endpoint(self.provider):
            # Zen announced (2026-09-03) that calls without the session header may
            # be refused, and uses it to route and to match the prompt cache: sent
            # before the provider's own headers, so a configured value still wins.
            headers[OPENCODE_SESSION_HEADER] = self.session_id
            headers[OPENCODE_CLIENT_HEADER] = OPENCODE_CLIENT
        headers.update(self.provider.extra_headers)
        return headers

    def _body(
        self,
        messages: Sequence[Message],
        *,
        model: str | None,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        if not messages:
            raise AIError(
                INVALID_CONFIG,
                "no messages to send",
                hint="a use case must render at least one message",
                provider=self.provider.name,
            )
        body: dict[str, Any] = {
            "model": model or self.provider.model,
            "messages": [message.to_dict() for message in messages],
        }
        if stream:
            # OpenAI-compatible servers decide between SSE and one JSON body on
            # this flag; the Accept header alone is not enough.
            body["stream"] = True
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if self.provider.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def _open(self, request: urllib.request.Request) -> Any:
        """Open a request, mapping every socket/HTTP failure to a code."""
        if self.local_only:
            enforce_local_only(self.provider.base_url, provider=self.provider.name)
        try:
            return self._opener(request, self.timeout_s)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except urllib.error.URLError as exc:
            raise self._url_error(exc) from exc
        except TimeoutError as exc:
            raise self._timeout_error() from exc
        except OSError as exc:  # connection reset/refused that is not a URLError
            raise AIError(
                UNREACHABLE,
                f"cannot reach {self.provider.name} at {self.provider.origin()}: {exc}",
                hint=("is the server running? `spacesage ai check` reports the endpoint it tried"),
                provider=self.provider.name,
            ) from exc

    def _post_text(self, body: Mapping[str, Any], *, stream: bool, cancel: Any) -> str:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.provider.chat_url(), data=data, headers=self._headers(stream=stream), method="POST"
        )
        self._check_cancel(cancel)
        with self._open(request) as response:
            raw = bytes(response.read(MAX_BODY_BYTES + 1))
        self.calls += 1
        if len(raw) > MAX_BODY_BYTES:
            raise AIError(
                BAD_RESPONSE,
                f"{self.provider.name} sent more than {MAX_BODY_BYTES} bytes",
                hint="check that base_url points at an OpenAI-compatible /chat/completions",
                provider=self.provider.name,
            )
        return raw.decode("utf-8", errors="replace")

    def _get_text(self, *, cancel: Any) -> str:
        request = urllib.request.Request(
            self.provider.models_url(), headers=self._headers(stream=False), method="GET"
        )
        self._check_cancel(cancel)
        with self._open(request) as response:
            raw = bytes(response.read(MAX_BODY_BYTES + 1))
        self.calls += 1
        return raw.decode("utf-8", errors="replace")

    def _consume_stream(
        self,
        body: Mapping[str, Any],
        *,
        deltas: list[str],
        state: dict[str, Any],
        on_delta: Callable[[str], None] | None,
        cancel: Any,
    ) -> None:
        """Read the SSE stream, accumulating text and reporting deltas."""
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.provider.chat_url(), data=data, headers=self._headers(stream=True), method="POST"
        )
        self._check_cancel(cancel)
        with self._open(request) as response:
            self.calls += 1
            for item in iter_stream(response, cancel=cancel):
                kind, payload = item
                if kind == "done":
                    break
                if kind == "raw":
                    self._apply_chunk(payload, deltas=deltas, state=state, on_delta=on_delta)
                    continue
                # kind == "json": a server that ignored `stream` and sent one body
                text, usage, model, finish = parse_completion_object(payload)
                state["model"] = model
                state["usage"] = usage
                state["finish_reason"] = finish
                state["chunks"] = int(state.get("chunks", 0)) + 1
                deltas.append(text)
                if on_delta is not None and text:
                    on_delta(text)
                break
        if not deltas and state.get("finish_reason") is None:
            raise AIError(
                BAD_RESPONSE,
                f"{self.provider.name} streamed no content",
                hint="the model or the endpoint produced an empty answer; try `spacesage ai check`",
                provider=self.provider.name,
            )

    def _apply_chunk(
        self,
        payload: Mapping[str, Any],
        *,
        deltas: list[str],
        state: dict[str, Any],
        on_delta: Callable[[str], None] | None,
    ) -> None:
        state["chunks"] = int(state.get("chunks", 0)) + 1
        if payload.get("model"):
            state["model"] = payload["model"]
        if payload.get("usage"):
            state["usage"] = payload["usage"]
        choices = payload.get("choices")
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)):
            return
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            finish = choice.get("finish_reason")
            if finish:
                state["finish_reason"] = finish
            delta = choice.get("delta")
            text = ""
            if isinstance(delta, Mapping):
                content = delta.get("content")
                if isinstance(content, str):
                    text = content
            elif isinstance(choice.get("text"), str):  # /completions-style chunk
                text = str(choice["text"])
            if text:
                deltas.append(text)
                if on_delta is not None:
                    on_delta(text)

    # -- results ------------------------------------------------------------ #

    def _parse_completion(self, text: str) -> Mapping[str, Any]:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIError(
                BAD_RESPONSE,
                f"{self.provider.name} sent a body that is not JSON ({exc.msg})",
                hint="check that base_url points at an OpenAI-compatible /chat/completions",
                provider=self.provider.name,
                detail={"body": text[:500]},
            ) from exc
        if not isinstance(payload, Mapping):
            raise AIError(
                BAD_RESPONSE,
                f"{self.provider.name} sent {type(payload).__name__} instead of an object",
                hint="expected an OpenAI-compatible chat completion",
                provider=self.provider.name,
            )
        if "error" in payload and "choices" not in payload:
            raise self._error_payload(payload)
        return payload

    def _result(
        self, payload: Mapping[str, Any], *, streamed: bool, started: float, chunks: int
    ) -> ChatResult:
        text, usage, model, finish = parse_completion_object(payload)
        if not text:
            raise AIError(
                BAD_RESPONSE,
                f"{self.provider.name} answered without content",
                hint="the model returned an empty completion; check the model name",
                provider=self.provider.name,
                detail={"finish_reason": finish},
            )
        return ChatResult(
            text=text,
            model=model or self.provider.model,
            usage=usage,
            latency_s=time.monotonic() - started,
            provider=self.provider.name,
            streamed=streamed,
            finish_reason=finish,
            chunks=chunks,
        )

    # -- failure mapping ---------------------------------------------------- #

    def _error_payload(self, payload: Mapping[str, Any]) -> AIError:
        error = payload.get("error")
        message = ""
        if isinstance(error, Mapping):
            message = str(error.get("message") or error.get("type") or "")
        elif error is not None:
            message = str(error)
        return AIError(
            BAD_RESPONSE,
            f"{self.provider.name} refused the call: {message or 'unknown error'}",
            hint="run `spacesage ai check` to test the provider with a minimal request",
            provider=self.provider.name,
        )

    def _http_error(self, exc: urllib.error.HTTPError) -> AIError:
        status = exc.code
        detail = _error_text(exc)
        retry_after = _retry_after(exc)
        if status in {401, 403}:
            return AIError(
                AUTH,
                f"{self.provider.name} rejected the credentials (HTTP {status})",
                hint=(
                    f"set the key environment variable "
                    f"({self.provider.api_key_env or 'the provider key'}); "
                    "`spacesage ai check` names the variable it looked for"
                ),
                status=status,
                provider=self.provider.name,
                detail={"body": detail},
            )
        if status == 404:
            if "model" in detail.lower():
                return AIError(
                    MODEL_MISSING,
                    f"{self.provider.name} does not serve the model {self.provider.model!r}",
                    hint="run `spacesage ai models` and set the model in ai.toml",
                    status=status,
                    provider=self.provider.name,
                    detail={"body": detail},
                )
            return AIError(
                BAD_RESPONSE,
                f"{self.provider.name} has no {self.provider.chat_url()} (HTTP 404)",
                hint="base_url must point at the OpenAI-compatible root, e.g. http://localhost:11434/v1",
                status=status,
                provider=self.provider.name,
                detail={"body": detail},
            )
        if status == 429:
            return AIError(
                RATE_LIMITED,
                f"{self.provider.name} is rate-limiting this key (HTTP 429)",
                hint="wait a moment, lower the batch size, or use a local provider",
                status=status,
                provider=self.provider.name,
                detail={"body": detail, "retry_after": retry_after},
            )
        if status >= 500:
            return AIError(
                SERVER_ERROR,
                f"{self.provider.name} failed with HTTP {status}",
                hint="the provider is reachable but broken; retrying automatically",
                status=status,
                provider=self.provider.name,
                detail={"body": detail, "retry_after": retry_after},
            )
        return AIError(
            BAD_RESPONSE,
            f"{self.provider.name} answered HTTP {status}",
            hint="check the endpoint, model and request shape (`spacesage ai check`)",
            status=status,
            provider=self.provider.name,
            detail={"body": detail},
        )

    def _url_error(self, exc: urllib.error.URLError) -> AIError:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return self._timeout_error()
        if isinstance(reason, urllib.error.HTTPError):  # pragma: no cover - defers to HTTPError
            return self._http_error(reason)
        return AIError(
            UNREACHABLE,
            f"cannot reach {self.provider.name} at {self.provider.origin()}: {reason}",
            hint=(
                "is the server running and the base_url right? "
                "`spacesage ai check` reports the endpoint it tried"
            ),
            provider=self.provider.name,
        )

    def _timeout_error(self) -> AIError:
        return AIError(
            TIMEOUT,
            f"{self.provider.name} did not answer within {self.timeout_s:g}s",
            hint="raise timeout_s for a slow local model, or pick a smaller one",
            provider=self.provider.name,
        )

    # -- retries ------------------------------------------------------------ #

    def _check_cancel(self, cancel: Any) -> None:
        if cancel is not None and getattr(cancel, "is_set", None) is not None and cancel.is_set():
            raise AIError(
                CANCELLED,
                "the run was cancelled",
                hint="nothing was executed - AI output is only ever a suggestion",
                provider=self.provider.name,
            )

    def _with_retries(self, call: Callable[[], Any], *, cancel: Any) -> Any:
        attempts = self.retries + 1
        last: AIError | None = None
        for attempt in range(attempts):
            self._check_cancel(cancel)
            try:
                return call()
            except AIError as exc:
                if not exc.retryable or attempt == attempts - 1:
                    raise
                last = exc
                delay = self.retry_backoff_s * (2**attempt)
                retry_after = exc.detail.get("retry_after")
                if isinstance(retry_after, (int, float)) and retry_after > 0:
                    delay = max(delay, min(float(retry_after), MAX_RETRY_AFTER_S))
                self._sleep(delay)
        raise last if last is not None else AIError(UNREACHABLE, "no attempt was made")


def provider_retries(provider: ProviderConfig, retries: int | None) -> int:
    """Retries to use: the caller's number, else two (three attempts)."""
    if retries is not None:
        return max(0, retries)
    return 2


# --------------------------------------------------------------------------- #
# Parsing helpers (shared with the tests that assert on the wire format)
# --------------------------------------------------------------------------- #


def parse_completion_object(
    payload: Mapping[str, Any],
) -> tuple[str, Usage, str, str | None]:
    """Pull ``(text, usage, model, finish_reason)`` out of a completion body."""
    choices = payload.get("choices")
    text = ""
    finish: str | None = None
    if isinstance(choices, Sequence) and not isinstance(choices, (str, bytes)) and choices:
        first = choices[0]
        if isinstance(first, Mapping):
            message = first.get("message")
            if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                text = str(message["content"])
            elif isinstance(first.get("text"), str):
                text = str(first["text"])
            finish = _optional_str(first.get("finish_reason"))
    usage = _usage_from(payload.get("usage"), prompt_chars=0, completion=text)
    model = _optional_str(payload.get("model")) or ""
    return text, usage, model, finish


def parse_models(text: str) -> tuple[ModelInfo, ...]:
    """Parse a ``/models`` listing (OpenAI shape, tolerant of variants)."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIError(
            BAD_RESPONSE,
            f"the /models listing is not JSON ({exc.msg})",
            hint="the endpoint is not OpenAI-compatible; check base_url",
            detail={"body": text[:500]},
        ) from exc
    entries: Any = payload.get("data") if isinstance(payload, Mapping) else None
    if entries is None and isinstance(payload, Mapping):
        entries = payload.get("models")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise AIError(
            BAD_RESPONSE,
            "the /models listing has no data array",
            hint='expected {"data": [{"id": ...}]}; check base_url',
            detail={"body": text[:500]},
        )
    models: list[ModelInfo] = []
    for entry in entries:
        if isinstance(entry, Mapping):
            identifier = entry.get("id") or entry.get("name") or entry.get("model")
            if isinstance(identifier, str) and identifier:
                created = entry.get("created")
                models.append(
                    ModelInfo(
                        id=identifier,
                        owned_by=_optional_str(entry.get("owned_by"))
                        or _optional_str(entry.get("publisher")),
                        created=int(created) if isinstance(created, int) else None,
                    )
                )
        elif isinstance(entry, str) and entry:
            models.append(ModelInfo(id=entry))
    return tuple(sorted(models, key=lambda info: info.id))


def iter_stream(response: Any, *, cancel: Any = None) -> Iterator[tuple[str, Any]]:
    """Yield ``("raw", chunk)`` per SSE event, or ``("json", body)`` for a plain body.

    Understands the SSE framing (multi-line ``data:`` fields, comments, the
    ``[DONE]`` sentinel) and degrades to a single JSON body when the server
    ignored ``stream: true``.
    """
    data_lines: list[str] = []
    started = False
    for raw_line in response:
        if cancel is not None and getattr(cancel, "is_set", None) is not None and cancel.is_set():
            raise AIError(CANCELLED, "the run was cancelled", hint="nothing was executed")
        line = (
            raw_line.decode("utf-8", errors="replace")
            if isinstance(raw_line, bytes)
            else str(raw_line)
        )
        line = line.rstrip("\r\n")
        if not started:
            if line == "" or line.startswith(":"):
                continue
            if line.startswith(("data:", "event:", "id:", "retry:")):
                started = True  # the first SSE line is data like any other
            else:  # a plain JSON body, not a stream
                body = (
                    line
                    + "\n"
                    + "".join(
                        chunk.decode("utf-8", errors="replace")
                        if isinstance(chunk, bytes)
                        else str(chunk)
                        for chunk in response
                    )
                )
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise AIError(
                        BAD_RESPONSE,
                        f"the streaming body is neither SSE nor JSON ({exc.msg})",
                        hint="check that base_url points at an OpenAI-compatible endpoint",
                        detail={"body": body[:500]},
                    ) from exc
                yield ("json", parsed)
                return
        if line == "":
            for payload in _dispatch(data_lines):
                if payload == "[DONE]":
                    yield ("done", None)
                    return
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise AIError(
                        BAD_RESPONSE,
                        f"a streamed chunk is not JSON ({exc.msg})",
                        hint="the server's streaming format is not OpenAI-compatible",
                        detail={"body": payload[:500]},
                    ) from exc
                yield ("raw", chunk)
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field_name, _, value = line.partition(":")
        if field_name == "data":
            data_lines.append(value[1:] if value.startswith(" ") else value)
    for payload in _dispatch(data_lines):
        if payload == "[DONE]":
            yield ("done", None)
            return
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AIError(BAD_RESPONSE, f"a streamed chunk is not JSON ({exc.msg})") from exc
        yield ("raw", chunk)


def _dispatch(data_lines: Sequence[str]) -> Iterator[str]:
    """Turn accumulated ``data:`` lines into payloads (multi-line joined)."""
    if not data_lines:
        return
    yield "\n".join(data_lines).strip()


def _usage_from(raw: Any, *, prompt_chars: int, completion: str) -> Usage:
    if isinstance(raw, Mapping):
        prompt = _as_int(raw.get("prompt_tokens"))
        done = _as_int(raw.get("completion_tokens"))
        total = _as_int(raw.get("total_tokens")) or (prompt + done)
        if prompt or done or total:
            return Usage(prompt_tokens=prompt, completion_tokens=done, total_tokens=total)
    return Usage(
        prompt_tokens=estimate_tokens_from_chars(prompt_chars),
        completion_tokens=estimate_tokens_from_chars(len(completion)),
        total_tokens=estimate_tokens_from_chars(prompt_chars + len(completion)),
        estimated=True,
    )


def _prompt_chars(body: Mapping[str, Any]) -> int:
    messages = body.get("messages")
    if not isinstance(messages, Sequence):
        return 0
    return sum(
        len(str(message.get("content", ""))) for message in messages if isinstance(message, Mapping)
    )


def estimate_tokens_from_chars(chars: int) -> int:
    """Rough token count (one token per four characters, rounded up)."""
    return max(0, (chars + 3) // 4)


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _error_text(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(MAX_BODY_BYTES)
    except Exception:  # pragma: no cover - the body is best-effort context
        return ""
    return raw.decode("utf-8", errors="replace")[:1000]


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("Retry-After")
    except AttributeError:  # pragma: no cover - defensive
        return None
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def messages_from_dicts(items: Iterable[Mapping[str, str]]) -> tuple[Message, ...]:
    """Build messages from plain mappings (``{"role": ..., "content": ...}``)."""
    out: list[Message] = []
    for item in items:
        role = str(item.get("role", "user"))
        content = str(item.get("content", ""))
        out.append(Message(role=role, content=content))
    return tuple(out)
