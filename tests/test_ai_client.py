"""The transport: one OpenAI-compatible protocol over stdlib ``urllib``.

The client's job is to make the network boring: correct requests, SSE parsed
into deltas, retries only where they help, and every failure arriving as a coded
:class:`~spacesage.ai.errors.AIError` that the UI can render with a fix.
"""

from __future__ import annotations

import socket

import pytest

from ai_stub import StubReply, StubServer
from spacesage.ai import AIClient, AIError, Message, ProviderConfig
from spacesage.ai.client import http_opener


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
    """A closed loopback port fails fast (the OS says no), it does not hang."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

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

    assert caught.value.code == "unreachable"
    assert str(port) in caught.value.message or "connect" in caught.value.message.lower()


def test_cancel_stops_before_the_call(ai_stub: StubServer) -> None:
    class Cancel:
        def is_set(self) -> bool:
            return True

    with pytest.raises(AIError) as caught:
        client_for(ai_stub).chat((Message(role="user", content="hi"),), cancel=Cancel())

    assert caught.value.code == "cancelled"
    assert ai_stub.calls == 0
