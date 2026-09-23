"""The transport: one OpenAI-compatible protocol over stdlib ``urllib``.

The client's job is to make the network boring: correct requests, SSE parsed
into deltas, retries only where they help, and every failure arriving as a coded
:class:`~spacesage.ai.errors.AIError` that the UI can render with a fix.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request

import pytest

from ai_stub import StubReply, StubServer
from spacesage.ai import AIClient, AIError, Message, ProviderConfig
from spacesage.ai.client import http_opener, is_opencode_endpoint


def provider_for(server: StubServer, **overrides: object) -> ProviderConfig:
    """A provider pointing at the stub (loopback, no key, retries off)."""
    values: dict[str, object] = {
        "name": "stub",
        "kind": "custom",
        "base_url": server.url,
        "model": "stub-model",
        "timeout_s": 5.0,
    }
    values.update(overrides)
    return ProviderConfig(**values)  # type: ignore[arg-type]


def client_for(server: StubServer, **overrides: object) -> AIClient:
    """A client on the stub: retries off, sleeps patched out (fast, deterministic)."""
    return AIClient(
        provider_for(server, **overrides),
        opener=http_opener,
        sleep=lambda _seconds: None,
        retries=0,
    )


def test_models_are_listed(ai_stub: StubServer) -> None:
    models = client_for(ai_stub).models()

    assert [info.id for info in models] == ["stub-embed", "stub-model", "stub-model-mini"]
    assert len(ai_stub.model_requests) == 1


def test_chat_sends_the_messages_and_parses_the_answer(ai_stub: StubServer) -> None:
    ai_stub.queue_text("hello from the stub", usage={"prompt_tokens": 21, "completion_tokens": 4})
    client = client_for(ai_stub)

    result = client.chat((Message(role="user", content="hi"),))

    assert result.text == "hello from the stub"
    assert result.usage.prompt_tokens == 21
    assert result.usage.completion_tokens == 4
    assert result.usage.estimated is False
    assert result.streamed is False
    assert result.model == "stub-model"
    request = ai_stub.chat_requests[-1]
    assert request.stream is False
    assert request.messages[0]["content"] == "hi"


def test_streaming_returns_deltas_and_the_whole_answer(ai_stub: StubServer) -> None:
    answer = {"explanation": "this folder is old and unused", "confidence": 0.5}
    ai_stub.queue_json(answer)
    client = client_for(ai_stub)
    deltas: list[str] = []

    result = client.chat_stream(
        (Message(role="user", content="explain it"),), on_delta=deltas.append
    )

    assert result.text == __import__("json").dumps(answer)
    assert "".join(deltas) == result.text
    assert result.streamed is True
    assert ai_stub.streamed == 1
    assert ai_stub.chat_requests[-1].body is not None
    assert ai_stub.chat_requests[-1].body["stream"] is True  # type: ignore[index]


def test_a_malformed_body_is_a_bad_response_error(ai_stub: StubServer) -> None:
    ai_stub.push(StubReply(raw="{not json at all"))

    with pytest.raises(AIError) as caught:
        client_for(ai_stub).chat((Message(role="user", content="hi"),))

    assert caught.value.code == "bad_response"
    assert "json" in caught.value.message.lower()


def test_auth_failure_names_the_environment_variable(ai_stub: StubServer) -> None:
    ai_stub.require_key = "secret-key"
    client = client_for(ai_stub, api_key_env="STUB_API_KEY")

    with pytest.raises(AIError) as caught:
        client.chat((Message(role="user", content="hi"),))

    error = caught.value
    assert error.code == "auth"
    assert error.retryable is False
    assert error.status == 401
    assert "STUB_API_KEY" in error.hint


def test_a_refusal_quotes_the_providers_own_reason(ai_stub: StubServer) -> None:
    """A 403 body carries the real explanation; the key hint must not deny it.

    OpenCode Zen's free models answer exactly this way to any client but its
    own: the variable is set, the key is sent, and "set the key environment
    variable" would send the user chasing the wrong thing.
    """
    ai_stub.queue_error(
        403,
        "OpenCode's free tier can only be used from within OpenCode",
        code="FreeTierError",
    )

    with pytest.raises(AIError) as caught:
        client_for(ai_stub, api_key_env="STUB_API_KEY").chat((Message(role="user", content="hi"),))

    error = caught.value
    assert error.code == "auth"
    assert error.status == 403
    assert "free tier can only be used from within OpenCode" in error.message
    assert "STUB_API_KEY" in error.hint, "the hint still names where the key came from"
    assert "set the key" not in error.hint, "the key was set; do not tell the user to set it"


def test_missing_model_is_coded(ai_stub: StubServer) -> None:
    ai_stub.queue_error(404, "model 'nope' not found", code="model_not_found")

    with pytest.raises(AIError) as caught:
        client_for(ai_stub).chat((Message(role="user", content="hi"),), model="nope")

    assert caught.value.code == "model_missing"
    assert caught.value.status == 404
    assert ai_stub.chat_requests[-1].body is not None
    assert ai_stub.chat_requests[-1].body["model"] == "nope"  # type: ignore[index]


def test_rate_limited_requests_are_retried(ai_stub: StubServer) -> None:
    ai_stub.queue_error(429, "slow down")
    ai_stub.queue_text("second try worked")
    client = AIClient(
        provider_for(ai_stub, extra_headers={}),
        sleep=lambda _seconds: None,
    )

    result = client.chat((Message(role="user", content="hi"),))

    assert result.text == "second try worked"
    assert ai_stub.calls == 2
    assert client.retries >= 1


def test_retries_stop_and_report_the_last_failure(ai_stub: StubServer) -> None:
    ai_stub.queue_error(500, "boom")
    ai_stub.queue_error(500, "boom again")
    client = AIClient(provider_for(ai_stub), retries=1, sleep=lambda _seconds: None)

    with pytest.raises(AIError) as caught:
        client.chat((Message(role="user", content="hi"),))

    assert caught.value.code == "server_error"
    assert caught.value.retryable is True
    assert ai_stub.calls == 2  # the first try plus exactly one retry


def test_an_unreachable_provider_is_coded(tmp_path: object) -> None:
    server = StubServer().start()
    url = server.url
    server.stop()

    with pytest.raises(AIError) as caught:
        AIClient(
            ProviderConfig(
                name="stub", kind="custom", base_url=url, model="stub-model", timeout_s=2.0
            ),
            sleep=lambda _seconds: None,
        ).chat((Message(role="user", content="hi"),))

    assert caught.value.code in {"unreachable", "timeout"}
    assert caught.value.retryable is True


def test_a_timeout_is_coded_and_not_retried_forever(ai_stub: StubServer) -> None:
    ai_stub.push(StubReply(content='{"a": 1}', delay_s=0.5))
    client = client_for(ai_stub, timeout_s=0.1)

    with pytest.raises(AIError) as caught:
        client.chat((Message(role="user", content="hi"),))

    assert caught.value.code == "timeout"
    assert ai_stub.calls == 1


def test_an_empty_envelope_is_a_bad_response(ai_stub: StubServer) -> None:
    ai_stub.push(StubReply(omit_content=True))

    with pytest.raises(AIError) as caught:
        client_for(ai_stub).chat((Message(role="user", content="hi"),))

    assert caught.value.code == "bad_response"


def test_a_closed_port_is_not_mistaken_for_a_timeout() -> None:
    """A closed loopback port fails fast (the OS says no), it does not hang.

    Some machines -- the Windows CI runners' loopback is one -- drop a closed
    port instead of refusing it, and then the socket genuinely times out; the
    classification itself is covered deterministically below, and this test
    only proves the fast-refusal shape where the OS produces it.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    started = time.monotonic()
    with pytest.raises(AIError) as caught:
        AIClient(
            ProviderConfig(
                name="stub",
                kind="custom",
                base_url=f"http://127.0.0.1:{port}/v1",
                model="stub-model",
                timeout_s=2.0,
            ),
            sleep=lambda _seconds: None,
        ).chat((Message(role="user", content="hi"),))
    elapsed = time.monotonic() - started

    if caught.value.code == "timeout" and elapsed >= 1.0:
        pytest.skip("this machine's loopback drops a closed port instead of refusing it")
    assert caught.value.code == "unreachable"
    assert str(port) in caught.value.message or "connect" in caught.value.message.lower()


def test_the_transport_tells_a_refusal_from_a_stall() -> None:
    """The classification itself, on every platform: a refused connection is
    ``unreachable``, a stall is ``timeout`` -- no OS in the way, the opener
    is owned by the test."""
    provider = ProviderConfig(
        name="stub", kind="custom", base_url="http://127.0.0.1:1/v1", model="stub-model"
    )

    def refusing(request: urllib.request.Request, timeout: float) -> object:
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    def stalling(request: urllib.request.Request, timeout: float) -> object:
        raise TimeoutError("timed out")

    with pytest.raises(AIError) as caught:
        AIClient(provider, opener=refusing, sleep=lambda _: None, retries=0).chat(
            (Message(role="user", content="hi"),)
        )
    assert caught.value.code == "unreachable"

    with pytest.raises(AIError) as caught:
        AIClient(provider, opener=stalling, sleep=lambda _: None, retries=0).chat(
            (Message(role="user", content="hi"),)
        )
    assert caught.value.code == "timeout"


def test_cancel_stops_before_the_call(ai_stub: StubServer) -> None:
    class Cancel:
        def is_set(self) -> bool:
            return True

    with pytest.raises(AIError) as caught:
        client_for(ai_stub).chat((Message(role="user", content="hi"),), cancel=Cancel())

    assert caught.value.code == "cancelled"
    assert ai_stub.calls == 0


# --------------------------------------------------------------------------- #
# OpenCode Zen: the routing headers the gateway requires
# --------------------------------------------------------------------------- #


def opencode_client_for(
    server: StubServer, *, session_id: str | None = None, **provider_overrides: object
) -> AIClient:
    """A client on the real ``opencode`` preset, aimed at the stub server.

    The preset's *kind* is what marks the endpoint, so the headers are exercised
    against real HTTP without a request ever leaving loopback: the routing rules
    are the subject, the gateway address is not.
    """
    provider = ProviderConfig.from_preset(
        "opencode",
        "opencode",
        base_url=server.url,
        model="stub-model",
        timeout_s=5.0,
        **provider_overrides,
    )
    return AIClient(
        provider,
        opener=http_opener,
        sleep=lambda _seconds: None,
        retries=0,
        session_id=session_id,
    )


def test_an_opencode_provider_sends_the_required_routing_headers(ai_stub: StubServer) -> None:
    ai_stub.queue_text("hello")
    client = opencode_client_for(ai_stub, session_id="pinned-session")

    client.chat((Message(role="user", content="hi"),))

    headers = ai_stub.chat_requests[-1].headers
    assert headers["x-opencode-session"] == "pinned-session"
    assert headers["x-opencode-client"] == "spacesage"


def test_the_session_id_is_stable_per_client_and_new_per_instance(ai_stub: StubServer) -> None:
    """One conversation, one routing id: the client reuses it, the next one does not."""
    ai_stub.push("first", "second", "third")
    first = opencode_client_for(ai_stub)
    first.chat((Message(role="user", content="hi"),))
    first.chat((Message(role="user", content="again"),))
    second = opencode_client_for(ai_stub)
    second.chat((Message(role="user", content="hi"),))

    ids = [request.headers["x-opencode-session"] for request in ai_stub.chat_requests]

    assert len(ids) == 3 and all(ids)  # every request carried one
    assert ids[0] == ids[1], "the same client keeps its conversation id"
    assert ids[2] != ids[0], "a new client is a new conversation"
    assert len(ids[0]) == 32 and set(ids[0]) <= set("0123456789abcdef")  # uuid4().hex


def test_the_models_listing_carries_the_routing_headers(ai_stub: StubServer) -> None:
    """Test connection / Refresh models go through the same header builder."""
    opencode_client_for(ai_stub, session_id="pinned-session").models()

    assert len(ai_stub.model_requests) == 1
    headers = ai_stub.model_requests[-1].headers
    assert headers["x-opencode-session"] == "pinned-session"
    assert headers["x-opencode-client"] == "spacesage"


def test_a_configured_header_overrides_the_routing_default(ai_stub: StubServer) -> None:
    ai_stub.queue_text("hello")
    client = opencode_client_for(
        ai_stub,
        extra_headers={
            "x-opencode-session": "my-own-id",
            "x-opencode-client": "not-spacesage",
        },
    )

    client.chat((Message(role="user", content="hi"),))

    headers = ai_stub.chat_requests[-1].headers
    assert headers["x-opencode-session"] == "my-own-id"
    assert headers["x-opencode-client"] == "not-spacesage"


def test_a_plain_provider_gets_no_routing_headers(ai_stub: StubServer) -> None:
    ai_stub.queue_text("hello")

    client_for(ai_stub).chat((Message(role="user", content="hi"),))

    headers = ai_stub.chat_requests[-1].headers
    assert "x-opencode-session" not in headers
    assert "x-opencode-client" not in headers


def test_the_gateway_host_alone_marks_a_provider_as_opencode() -> None:
    """The preset is convenient, not required: pointing base_url at Zen is enough."""

    def provider(base_url: str) -> ProviderConfig:
        return ProviderConfig(name="zen", kind="custom", base_url=base_url, model="m")

    assert is_opencode_endpoint(provider("https://opencode.ai/zen/v1")) is True
    assert is_opencode_endpoint(provider("https://api.opencode.ai:443/zen/v1/")) is True
    assert is_opencode_endpoint(provider("https://notopencode.ai/zen/v1")) is False
    assert is_opencode_endpoint(provider("http://127.0.0.1:11434/v1")) is False
    assert is_opencode_endpoint(ProviderConfig(name="zen", kind="opencode", base_url="")) is True


def test_a_provider_pointed_at_the_gateway_sends_the_headers() -> None:
    """No socket: the request object is captured, and it is the one that counts."""
    seen: list[urllib.request.Request] = []

    class Response:
        def read(self, size: int = -1) -> bytes:
            return b'{"data": [{"id": "deepseek-v4-flash"}]}'

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

    def opener(request: urllib.request.Request, timeout: float) -> Response:
        seen.append(request)
        return Response()

    provider = ProviderConfig(
        name="zen",
        kind="custom",
        base_url="https://opencode.ai/zen/v1",
        model="deepseek-v4-flash",
    )
    client = AIClient(
        provider, opener=opener, sleep=lambda _seconds: None, retries=0, session_id="pinned"
    )

    assert [info.id for info in client.models()] == ["deepseek-v4-flash"]
    assert seen[0].full_url == "https://opencode.ai/zen/v1/models"
    sent = {key.lower(): value for key, value in seen[0].headers.items()}
    assert sent["x-opencode-session"] == "pinned"
    assert sent["x-opencode-client"] == "spacesage"
