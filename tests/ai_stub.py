"""A scripted OpenAI-compatible server for the AI tests (stdlib only).

The AI layer's contract is "speak to anything that speaks the OpenAI API", so
the tests speak back: this module is a real HTTP server on a loopback port that
answers ``GET /models`` and ``POST /chat/completions`` - with SSE streaming when
the request asks for it - from a queue of scripted replies.

Scripting is per request, in order: ``.push(...)`` adds replies, and every
``/chat/completions`` call consumes the next one.  That is what makes it
possible to test the interesting paths without a model: an invalid answer
followed by a valid one is the repair retry, two identical requests are the
cache, ``queue_error(429)`` is the rate limiter.  Requests are recorded, so a
test can assert on what was actually sent (streaming, headers, payload).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_MODEL = "stub-model"
DEFAULT_MODELS: tuple[str, ...] = ("stub-model", "stub-model-mini", "stub-embed")


@dataclass(frozen=True)
class StubRequest:
    """One request the server received."""

    method: str
    path: str
    body: Mapping[str, Any] | None
    headers: Mapping[str, str]
    stream: bool = False

    @property
    def messages(self) -> Sequence[Mapping[str, Any]]:
        """The ``messages`` array of a chat request (``[]`` for other paths)."""
        raw = (self.body or {}).get("messages")
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, Mapping)]
        return []

    @property
    def user_text(self) -> str:
        """Everything the user message carried (prompt + payload)."""
        return "\n".join(str(message.get("content", "")) for message in self.messages)


@dataclass(frozen=True)
class StubReply:
    """One scripted answer.

    ``content`` is the assistant text; ``chunks`` overrides how a streamed answer
    is split; ``status`` >= 400 answers with an error body instead; ``model`` and
    ``usage`` land in the response envelope.
    """

    content: str | None = None
    status: int = 200
    chunks: tuple[str, ...] | None = None
    sse: bool | None = None
    """Force (or forbid) SSE for this reply, whatever the request asked for."""
    delay_s: float = 0.0
    model: str = DEFAULT_MODEL
    usage: Mapping[str, int] | None = None
    error_code: str = "invalid_request_error"
    close_early: bool = False
    """Stop sending after the first chunk (a truncated stream)."""
    omit_content: bool = False
    """Answer with an empty envelope (no ``choices``)."""
    raw: str | None = None
    """Send this exact body instead of an envelope (a malformed provider)."""


class StubServer:
    """The server: start it, script it, inspect what it was asked."""

    def __init__(
        self,
        replies: Iterable[StubReply] = (),
        *,
        models: Sequence[str] = DEFAULT_MODELS,
        require_key: str | None = None,
        models_status: int = 200,
        models_message: str = "the model list is unavailable",
    ) -> None:
        self.replies: list[StubReply] = list(replies)
        self.models = tuple(models)
        self.require_key = require_key
        self.models_status = models_status
        self.models_message = models_message
        self.requests: list[StubRequest] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._model_hits = 0

    # -- scripting ---------------------------------------------------------- #

    def push(self, *replies: StubReply | str) -> StubServer:
        """Queue replies (a bare ``str`` becomes an answer with that content)."""
        with self._lock:
            for reply in replies:
                self.replies.append(StubReply(content=reply) if isinstance(reply, str) else reply)
        return self

    def queue_json(
        self, payload: Mapping[str, Any], *, status: int = 200, sse: bool | None = None
    ) -> StubServer:
        """Queue a reply whose assistant content is exactly ``payload`` as JSON."""
        return self.push(StubReply(content=json.dumps(payload), status=status, sse=sse))

    def queue_text(
        self,
        content: str,
        *,
        sse: bool | None = None,
        usage: Mapping[str, int] | None = None,
        delay_s: float = 0.0,
    ) -> StubServer:
        """Queue a plain-text answer (an invalid JSON answer, usually)."""
        return self.push(StubReply(content=content, sse=sse, usage=usage, delay_s=delay_s))

    def queue_error(
        self,
        status: int,
        message: str = "stub failure",
        *,
        code: str = "invalid_request_error",
        sse: bool | None = None,
    ) -> StubServer:
        """Queue an HTTP error (401 auth, 404 model, 429 rate limit, 500, ...)."""
        return self.push(StubReply(status=status, content=message, error_code=code, sse=sse))

    def next_reply(self) -> StubReply:
        """Consume the next scripted reply (a 500 when the queue is empty)."""
        with self._lock:
            if self.replies:
                return self.replies.pop(0)
        return StubReply(status=500, content="stub: no reply scripted")

    # -- inspection --------------------------------------------------------- #

    @property
    def chat_requests(self) -> tuple[StubRequest, ...]:
        """Every ``/chat/completions`` request, in order."""
        return tuple(
            request for request in self.requests if request.path.endswith("/chat/completions")
        )

    @property
    def model_requests(self) -> tuple[StubRequest, ...]:
        """Every ``/models`` request, in order."""
        return tuple(request for request in self.requests if request.path.endswith("/models"))

    @property
    def calls(self) -> int:
        """How many chat completions were asked for."""
        return len(self.chat_requests)

    @property
    def streamed(self) -> int:
        """How many of them asked for SSE."""
        return sum(1 for request in self.chat_requests if request.stream)

    def last_payload(self) -> str:
        """The raw ``<item-data>`` block of the last chat request."""
        raw = self.chat_requests[-1].user_text
        start = raw.rfind("<item-data>")
        end = raw.rfind("</item-data>")
        if start == -1 or end == -1:
            return raw
        return raw[start + len("<item-data>") : end]

    def reset(self) -> None:
        """Forget the requests (not the queued replies)."""
        with self._lock:
            self.requests.clear()

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def url(self) -> str:
        """The ``base_url`` the client should be given (``.../v1``)."""
        if self._httpd is None:
            raise RuntimeError("the stub server is not running")
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> StubServer:
        """Bind a loopback port and serve in a background thread."""
        if self._httpd is not None:
            return self
        handler = _handler_for(self)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Shut the server down (safe to call twice)."""
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = None
        self._thread = None

    def __enter__(self) -> StubServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def _handler_for(server: StubServer) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to one stub server."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "SpaceSageStub/1"

        def log_message(self, format: str, *args: object) -> None:
            """Keep the test output clean."""
            return

        # -- helpers ---------------------------------------------------- #

        def _read_body(self) -> Mapping[str, Any] | None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return None
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return data if isinstance(data, Mapping) else None

        def _record(self, method: str, body: Mapping[str, Any] | None) -> StubRequest:
            request = StubRequest(
                method=method,
                path=self.path,
                body=body,
                headers={key.lower(): value for key, value in self.headers.items()},
                stream=bool(isinstance(body, Mapping) and body.get("stream") is True),
            )
            with server._lock:
                server.requests.append(request)
            return request

        def _send_json(self, payload: Mapping[str, Any], status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._write(body)

        def _write(self, data: bytes) -> bool:
            """Write a response body; ``False`` when the client hung up first.

            The timeout tests close the connection while a deliberately delayed
            reply is still on its way; the server must not treat that as an
            error and print a traceback over the test output.
            """
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                return False
            return True

        def _send_error_body(self, reply: StubReply) -> None:
            self._send_json(
                {
                    "error": {
                        "message": reply.content or "stub failure",
                        "type": reply.error_code,
                        "code": reply.error_code,
                    }
                },
                status=reply.status,
            )

        def _envelope(self, reply: StubReply, content: str, *, finish: str) -> dict[str, Any]:
            return {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": reply.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": finish,
                    }
                ],
                "usage": dict(reply.usage or {"prompt_tokens": 11, "completion_tokens": 7}),
            }

        def _send_stream(self, reply: StubReply, content: str) -> None:
            pieces = (
                tuple(reply.chunks) if reply.chunks is not None else tuple(_split_chunks(content))
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for index, piece in enumerate(pieces):
                chunk = {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": reply.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": piece},
                            "finish_reason": None,
                        }
                    ],
                }
                if not self._write(f"data: {json.dumps(chunk)}\n\n".encode()):
                    return  # the client went away mid-stream
                if reply.close_early and index == 0:
                    self.close_connection = True
                    return
            final = {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": reply.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            self._write(f"data: {json.dumps(final)}\n\n".encode())
            if reply.usage:
                usage_chunk = {"usage": dict(reply.usage), "model": reply.model}
                self._write(f"data: {json.dumps(usage_chunk)}\n\n".encode())
            self._write(b"data: [DONE]\n\n")
            self.close_connection = True

        # -- routes ----------------------------------------------------- #

        def do_GET(self) -> None:
            self._record("GET", None)
            if not self.path.endswith("/models"):
                self._send_json({"error": {"message": f"no route {self.path}"}}, status=404)
                return
            with server._lock:
                server._model_hits += 1
            if server.models_status >= 400:
                self._send_json(
                    {
                        "error": {
                            "message": server.models_message,
                            "type": "server_error",
                            "code": "server_error",
                        }
                    },
                    status=server.models_status,
                )
                return
            models = server.models or (DEFAULT_MODEL,)
            self._send_json(
                {
                    "object": "list",
                    "data": [
                        {"id": name, "object": "model", "owned_by": "stub", "created": 1}
                        for name in models
                    ],
                }
            )

        def do_POST(self) -> None:
            body = self._read_body()
            request = self._record("POST", body)
            if server.require_key is not None:
                sent = request.headers.get("authorization", "")
                if sent != f"Bearer {server.require_key}":
                    self._send_error_body(
                        StubReply(status=401, content="missing or wrong key", error_code="auth")
                    )
                    return
            if not request.path.endswith("/chat/completions"):
                self._send_json({"error": {"message": f"no route {request.path}"}}, status=404)
                return
            reply = server.next_reply()
            if reply.delay_s:
                time.sleep(reply.delay_s)
            if reply.raw is not None:
                body = reply.raw.encode("utf-8")
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if reply.status >= 400:
                self._send_error_body(reply)
                return
            content = reply.content or ""
            if reply.omit_content:
                self._send_json(
                    {
                        "id": "chatcmpl-stub",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": reply.model,
                        "choices": [],
                    }
                )
                return
            wants_sse = request.stream if reply.sse is None else reply.sse
            if wants_sse:
                self._send_stream(reply, content)
                return
            self._send_json(self._envelope(reply, content, finish="stop"))

    return Handler


def _split_chunks(content: str, *, size: int = 7) -> Iterator[str]:
    """Split an answer into small pieces so streaming is observable."""
    for start in range(0, len(content), size):
        yield content[start : start + size]


def item(path: str, **overrides: Any) -> dict[str, Any]:
    """A suggestion answer entry (``suggestions`` array element)."""
    entry: dict[str, Any] = {
        "path": path,
        "action": "DELETE_QUARANTINE",
        "why": "stale installer of a program that is no longer installed",
        "confidence": 0.9,
        "side_effects": "moves the file to the quarantine folder",
        "alternatives": ["move it to the archive drive"],
    }
    entry.update(overrides)
    return entry


def suggestion_answer(*entries: Mapping[str, Any]) -> dict[str, Any]:
    """A complete ``suggest`` answer with the given entries."""
    return {"suggestions": [dict(entry) for entry in entries]}


def classification_entry(path: str, **overrides: Any) -> dict[str, Any]:
    """A classify answer entry."""
    entry: dict[str, Any] = {
        "path": path,
        "category": "installers",
        "tier": "T2",
        "action": "DELETE_QUARANTINE",
        "confidence": 0.85,
        "rationale": "an old setup package that can be re-downloaded",
    }
    entry.update(overrides)
    return entry


def classification_answer(*entries: Mapping[str, Any]) -> dict[str, Any]:
    """A complete ``classify`` answer with the given entries."""
    return {"classifications": [dict(entry) for entry in entries]}


def explanation_answer(text: str | None = None, **overrides: Any) -> dict[str, Any]:
    """A complete ``explain`` answer (the explanation is at least 40 characters)."""
    payload: dict[str, Any] = {
        "title": "Old installers",
        "explanation": text
        or (
            "This folder holds installers for programs that are no longer installed, "
            "so the bytes are re-downloadable and nothing depends on them."
        ),
        "risks": ["none that matter"],
        "alternatives": ["archive instead"],
        "confidence": 0.7,
    }
    payload.update(overrides)
    return payload


def annotation_entry(action_id: str, **overrides: Any) -> dict[str, Any]:
    """A review annotation entry."""
    entry: dict[str, Any] = {
        "action_id": action_id,
        "severity": "warning",
        "title": "Large move",
        "detail": "this action moves data off the system drive",
        "recommendation": "run it outside working hours",
    }
    entry.update(overrides)
    return entry


def summary_answer(**overrides: Any) -> dict[str, Any]:
    """A complete ``summarize`` answer."""
    payload: dict[str, Any] = {
        "headline": "Two actions free up 4 GiB",
        "summary": "SpaceSage will quarantine one installer folder and move a video library.",
        "highlights": ["4 GiB comes back"],
        "caveats": ["the move needs the archive drive attached"],
    }
    payload.update(overrides)
    return payload


def review_answer(*entries: Mapping[str, Any], summary: str = "Looks safe.") -> dict[str, Any]:
    """A complete ``review`` answer."""
    return {"summary": summary, "annotations": [dict(entry) for entry in entries]}
