"""Offline protocol checks: no paid API calls, real keys, or external network."""

import asyncio
import json
import math

import httpx
import pytest

from argos_studio.agent_provider import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    AgentConfig,
    AgentLimit,
    OpenAIProvider,
    ProviderError,
)

TEST_KEY = "test-only-placeholder-key"
CONFIG = AgentConfig(provider="openai", model="explicit-test-model", api_key=TEST_KEY)
SCHEMA = {
    "name": "get_session_context",
    "description": "Read one session",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
}
REQUEST = {
    "instructions": "Use the bounded session tools.",
    "messages": [{"role": "user", "content": "Inspect the receipt window."}],
    "schemas": [SCHEMA],
    "max_output_tokens": 800,
}


def message(text="Evidence reviewed"):
    return {
        "id": "msg_fixture",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def call(**changes):
    return {
        "id": "fc_fixture",
        "type": "function_call",
        "status": "completed",
        "call_id": "call_fixture",
        "name": "get_session_context",
        "arguments": "{}",
        **changes,
    }


def completed(output=None, usage=None):
    return {
        "id": "resp_fixture",
        "status": "completed",
        "output": [message()] if output is None else output,
        "usage": usage,
    }


def request_with(handler, **overrides):
    async def invoke():
        provider = OpenAIProvider(CONFIG, transport=httpx.MockTransport(handler))
        try:
            return await provider.respond(**{**REQUEST, **overrides})
        finally:
            await provider.close()

    return asyncio.run(invoke())


def reply_for(payload):
    return request_with(lambda _request: httpx.Response(200, json=payload))


def test_fixed_responses_endpoint_strict_tools_and_explicit_configuration(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://untrusted.invalid/override")
    monkeypatch.setenv("HTTPS_PROXY", "not-a-valid-proxy")
    monkeypatch.setenv("HTTP_PROXY", "not-a-valid-proxy")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completed(usage={"input_tokens": 120, "output_tokens": 18}))

    # A caller cannot turn a function schema into a provider-hosted tool or
    # silently relax strict validation through an extra schema field.
    reply = request_with(handler, schemas=[{**SCHEMA, "strict": False, "type": "web_search"}])
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.openai.com/v1/responses"
    assert request.headers["authorization"] == f"Bearer {TEST_KEY}"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body["model"] == CONFIG.model
    assert body["instructions"] == REQUEST["instructions"]
    assert body["input"] == REQUEST["messages"]
    assert body["tools"] == [{**SCHEMA, "type": "function", "strict": True}]
    assert body["store"] is False
    assert body["parallel_tool_calls"] is False
    assert body["max_output_tokens"] == 800
    assert "reasoning.encrypted_content" in body["include"]
    assert "previous_response_id" not in body
    assert TEST_KEY not in request.content.decode()
    assert reply.answer == "Evidence reviewed"
    assert reply.calls == []
    assert reply.usage == {"input_tokens": 120, "output_tokens": 18}


def test_stateless_followup_replays_function_output_and_opaque_reasoning_in_order():
    reasoning = {
        "id": "rs_fixture",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "opaque-provider-reasoning-fixture",
    }
    function = call()
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        output = [reasoning, function] if len(requests) == 1 else [message("Observed one gap.")]
        return httpx.Response(200, json=completed(output))

    async def invoke():
        provider = OpenAIProvider(CONFIG, transport=httpx.MockTransport(handler))
        try:
            first = await provider.respond(**REQUEST)
            assert first.answer == ""
            assert first.calls == [
                {"call_id": "call_fixture", "name": "get_session_context", "arguments": "{}"}
            ]
            assert first.continuation == [reasoning, function]
            tool_output = {
                "type": "function_call_output",
                "call_id": first.calls[0]["call_id"],
                "output": '{"gap_count":1,"source":"simulation"}',
            }
            history = [*REQUEST["messages"], *first.continuation, tool_output]
            final = await provider.respond(**{**REQUEST, "messages": history})
            assert final.answer == "Observed one gap."
            assert requests[1]["input"] == history
            assert "encrypted_content" not in final.answer
        finally:
            await provider.close()

    asyncio.run(invoke())
    assert len(requests) == 2
    assert all(body["store"] is False and "previous_response_id" not in body for body in requests)


def test_refusal_is_returned_as_visible_text_without_creating_an_action():
    refused = message()
    refused["content"] = [{"type": "refusal", "refusal": "I cannot infer physical motor health."}]
    reply = reply_for(completed([refused]))
    assert reply.answer == "I cannot infer physical motor health."
    assert reply.calls == []


def test_message_parts_and_multiple_messages_keep_text_order():
    first = message("Observation")
    first["content"].append({"type": "output_text", "text": "Uncertainty", "annotations": []})
    reply = reply_for(completed([first, message("Next check")]))
    assert reply.answer == "Observation\nUncertainty\nNext check"


def test_arguments_remain_exact_text_for_the_domain_validator():
    # The adapter does not reinterpret parameters or substitute invalid JSON.
    arguments = ' {"unexpected": "untrusted context"} '
    reply = reply_for(completed([call(arguments=arguments)]))
    assert reply.calls[0]["arguments"] == arguments


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 429, 500, 503])
def test_http_failures_never_follow_redirects_retry_or_expose_response_data(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"location": f"https://untrusted.invalid/{TEST_KEY}"},
            text=f"Remote debug: Authorization Bearer {TEST_KEY}; private session content",
        )

    with pytest.raises(ProviderError) as failure:
        request_with(handler)
    assert f"HTTP {status}" in str(failure.value)
    assert TEST_KEY not in str(failure.value)
    assert "private session content" not in str(failure.value)
    assert "untrusted.invalid" not in str(failure.value)
    assert failure.value.usage == {}
    assert len(requests) == 1


@pytest.mark.parametrize(
    "error", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.DecodingError]
)
def test_transport_failures_have_a_public_message_without_echoed_request_or_key(error):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise error(f"Private request with {TEST_KEY}", request=request)

    with pytest.raises(ProviderError) as failure:
        request_with(handler)
    assert TEST_KEY not in str(failure.value)
    assert "Private request" not in str(failure.value)
    assert failure.value.__suppress_context__
    assert calls == 1


def test_incomplete_generation_preserves_reported_usage_but_exposes_no_partial_calls():
    payload = completed([call()], {"input_tokens": 200, "output_tokens": 800, "total_tokens": 1000})
    payload.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    with pytest.raises(AgentLimit) as failure:
        reply_for(payload)
    assert failure.value.usage == {"input_tokens": 200, "output_tokens": 800}
    assert "call_fixture" not in str(failure.value)
    assert not hasattr(failure.value, "calls")


@pytest.mark.parametrize("status", ["failed", "cancelled", "queued", "in_progress", None])
def test_noncompleted_response_never_exposes_provider_error_or_partial_tool(status):
    payload = completed([call()], {"input_tokens": 200, "output_tokens": 10})
    payload.update(status=status, error={"message": f"Debug includes {TEST_KEY}"})
    with pytest.raises(ProviderError) as failure:
        reply_for(payload)
    assert TEST_KEY not in str(failure.value)
    assert failure.value.usage == {"input_tokens": 200, "output_tokens": 10}


@pytest.mark.parametrize("invalid", [-1, 3.5, True, "20", None])
def test_usage_does_not_invent_or_coerce_token_counts(invalid):
    reply = reply_for(
        completed(usage={"input_tokens": invalid, "output_tokens": 0, "debug": TEST_KEY})
    )
    assert reply.usage == {"output_tokens": 0}
    assert reply_for(completed(usage=None)).usage == {}


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "response",
        {"status": "completed"},
        completed(output="message"),
        completed(output=[message()] * 33),
        completed(output=[None]),
        completed(output=[{}]),
        completed(output=[call(name="")]),
        completed(output=[call(call_id=None)]),
        completed(output=[call(arguments={})]),
        completed(output=[call(status="incomplete")]),
        completed(output=[call(), call(call_id="second")]),
        completed(output=[{"type": "message", "role": "user", "content": []}]),
        completed(output=[{"type": "message", "role": "assistant", "content": {}}]),
        completed(output=[{"type": "message", "role": "assistant", "content": ["text"]}]),
        completed(output=[{"type": "message", "role": "assistant", "content": [{}]}]),
        completed(
            output=[
                {"type": "message", "role": "assistant", "content": [{"type": "image", "url": "x"}]}
            ]
        ),
        completed(
            output=[
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": 42}],
                }
            ]
        ),
        completed(output=[{"type": "reasoning", "encrypted_content": []}]),
        completed(output=[{"type": "web_search_call"}]),
        completed(usage=32),
    ],
)
def test_malformed_or_unsupported_response_fails_before_any_tool_can_execute(payload):
    with pytest.raises(ProviderError, match="invalide"):
        reply_for(payload)


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b'{"status":"completed","output":[],"usage":{"input_tokens":NaN}}',
        b'{"status":"completed","output":[],"usage":{"input_tokens":Infinity}}',
        b'{"status":"completed","output":[],"metadata":{"value":1e999}}',
        b'{"status":"completed","output":[],"metadata":{"value":"\\ud800"}}',
        b"[" * 2000 + b"]" * 2000,
        b"\xff\xfe\xff",
    ],
)
def test_invalid_raw_json_is_a_sanitized_provider_error(raw):
    with pytest.raises(ProviderError, match="invalide"):
        request_with(lambda _request: httpx.Response(200, content=raw))


def test_malformed_output_still_preserves_valid_reported_token_counts():
    with pytest.raises(ProviderError) as failure:
        reply_for(completed(output=[{}], usage={"input_tokens": 100, "output_tokens": 20}))
    assert failure.value.usage == {"input_tokens": 100, "output_tokens": 20}


def test_request_size_limit_is_checked_before_transport_and_uses_utf8_bytes():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=completed())

    with pytest.raises(AgentLimit, match="256"):
        request_with(
            handler, messages=[{"role": "user", "content": "é" * (MAX_REQUEST_BYTES // 2)}]
        )
    assert calls == []


class ChunkedResponse(httpx.AsyncByteStream):
    def __init__(self):
        self.read_count = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in (b"x" * MAX_RESPONSE_BYTES, b"x", b"unread tail"):
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def test_response_size_limit_stops_reading_and_closes_the_transport_stream():
    stream = ChunkedResponse()
    with pytest.raises(AgentLimit, match="512"):
        request_with(lambda _request: httpx.Response(200, stream=stream))
    assert stream.read_count == 2
    assert stream.closed


@pytest.mark.parametrize("tokens", [0, -1, True, 3.5, None])
def test_invalid_output_token_limit_does_not_reach_the_transport(tokens):
    calls = []
    with pytest.raises(ProviderError):
        request_with(lambda request: calls.append(request), max_output_tokens=tokens)
    assert calls == []


def test_nonfinite_or_cyclic_request_never_reaches_transport_or_echoes_context():
    cycle = {}
    cycle["cycle"] = cycle
    for value in ({"text": math.nan}, {"text": {TEST_KEY}}, cycle, {"text": "\ud800"}):
        calls = []
        with pytest.raises(ProviderError) as failure:
            request_with(lambda request, calls=calls: calls.append(request), messages=[value])
        assert calls == []
        assert TEST_KEY not in str(failure.value)


def test_environment_configuration_is_explicit_and_key_is_absent_from_repr(monkeypatch):
    for variable in ("ARGOS_STUDIO_AGENT_PROVIDER", "ARGOS_STUDIO_AGENT_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    assert AgentConfig.from_env().reason
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    assert AgentConfig.from_env().reason  # A key alone does not enable an agent.
    monkeypatch.setenv("ARGOS_STUDIO_AGENT_PROVIDER", " openai ")
    assert AgentConfig.from_env().reason
    monkeypatch.setenv("ARGOS_STUDIO_AGENT_MODEL", " explicit-test-model ")
    configured = AgentConfig.from_env()
    assert configured.reason is None
    assert configured.model == "explicit-test-model"
    assert configured.provider == "openai"
    assert TEST_KEY not in repr(configured)


@pytest.mark.parametrize(
    "config",
    [
        AgentConfig(),
        AgentConfig(provider="unsupported", model="test", api_key=TEST_KEY),
        AgentConfig(provider="openai", model="", api_key=TEST_KEY),
        AgentConfig(provider="openai", model="x" * 121, api_key=TEST_KEY),
        AgentConfig(provider="openai", model="test"),
        AgentConfig(provider="openai", model="test", api_key=f"{TEST_KEY}\nInjected: header"),
        AgentConfig(provider="openai", model="test", api_key="non-ascii-é"),
    ],
)
def test_unconfigured_or_invalid_provider_is_rejected_without_exposing_key(config):
    with pytest.raises(ValueError) as failure:
        OpenAIProvider(config, transport=httpx.MockTransport(lambda _: None))
    assert TEST_KEY not in str(failure.value)
