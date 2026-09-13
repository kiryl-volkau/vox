from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from vox_server.llm import (
    LlmResponseError,
    LlmTimeoutError,
    LlmUnavailableError,
    OpenAICompatibleClient,
)

API_KEY = "sk-never-leak-me"

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler, *, api_key: str = API_KEY) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url="http://llm.test:11434/v1/",
        model="qwen2.5:7b-instruct",
        api_key=api_key,
        timeout_seconds=7.0,
        temperature=0.1,
        max_tokens=256,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _completion(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _always(payload: dict[str, Any], status: int = 200) -> Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def test_the_trailing_slash_is_stripped_from_the_base_url() -> None:
    client = _client(_always(_completion("x")))

    assert client.base_url == "http://llm.test:11434/v1"
    assert client.model == "qwen2.5:7b-instruct"


async def test_chat_posts_the_documented_payload() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion("ответ"))

    client = _client(handler)
    try:
        result = await client.chat("СИСТЕМА", "ПОЛЬЗОВАТЕЛЬ")
    finally:
        await client.aclose()

    assert result == "ответ"
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "http://llm.test:11434/v1/chat/completions"

    body = json.loads(request.content)
    assert body["model"] == "qwen2.5:7b-instruct"
    assert body["messages"] == [
        {"role": "system", "content": "СИСТЕМА"},
        {"role": "user", "content": "ПОЛЬЗОВАТЕЛЬ"},
    ]
    assert body["temperature"] == 0.1
    assert body["max_tokens"] == 256
    assert body["stream"] is False
    assert request.headers["authorization"] == f"Bearer {API_KEY}"


async def test_an_empty_system_prompt_is_omitted_from_the_messages() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion("ok"))

    client = _client(handler)
    try:
        await client.chat("   ", "только пользователь")
    finally:
        await client.aclose()

    messages = json.loads(seen[0].content)["messages"]

    assert messages == [{"role": "user", "content": "только пользователь"}]


async def test_no_authorization_header_without_an_api_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion("ok"))

    client = _client(handler, api_key="")
    try:
        await client.chat("s", "u")
    finally:
        await client.aclose()

    assert "authorization" not in seen[0].headers


async def test_the_per_call_temperature_overrides_the_default() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion("ok"))

    client = _client(handler)
    try:
        await client.chat("s", "u", temperature=0.9)
    finally:
        await client.aclose()

    assert json.loads(seen[0].content)["temperature"] == 0.9


async def test_the_raw_content_is_returned_uncleaned() -> None:
    raw = "```\nвнутри ограждения\n```"
    client = _client(_always(_completion(raw)))
    try:
        assert await client.chat("s", "u") == raw
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": {"text": "no"}}}]},
        {"choices": "not-a-list"},
    ],
)
async def test_an_unusable_payload_raises_a_response_error(payload: dict[str, Any]) -> None:
    client = _client(_always(payload))
    try:
        with pytest.raises(LlmResponseError):
            await client.chat("s", "u")
    finally:
        await client.aclose()


async def test_a_non_json_body_raises_a_response_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    client = _client(handler)
    try:
        with pytest.raises(LlmResponseError):
            await client.chat("s", "u")
    finally:
        await client.aclose()


async def test_a_timeout_raises_a_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client = _client(handler)
    try:
        with pytest.raises(LlmTimeoutError, match="7s"):
            await client.chat("s", "u")
    finally:
        await client.aclose()


async def test_a_connection_failure_raises_an_unavailable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)
    try:
        with pytest.raises(LlmUnavailableError) as excinfo:
            await client.chat("s", "u")
    finally:
        await client.aclose()

    assert API_KEY not in str(excinfo.value)


async def test_a_non_2xx_status_never_leaks_the_api_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"bad key {API_KEY}"}})

    client = _client(handler)
    try:
        with pytest.raises(LlmResponseError) as excinfo:
            await client.chat("s", "u")
    finally:
        await client.aclose()

    assert "401" in str(excinfo.value)
    assert API_KEY not in str(excinfo.value)
    assert API_KEY not in repr(excinfo.value)


async def test_check_probes_the_models_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    client = _client(handler)
    try:
        assert await client.check() == (True, None)
    finally:
        await client.aclose()

    assert seen[0].method == "GET"
    assert str(seen[0].url) == "http://llm.test:11434/v1/models"


async def test_check_reports_a_bad_status_without_raising() -> None:
    client = _client(_always({"detail": "nope"}, status=503))
    try:
        ready, reason = await client.check()
    finally:
        await client.aclose()

    assert ready is False
    assert reason == "HTTP 503"


async def test_check_reports_an_unreachable_endpoint_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)
    try:
        ready, reason = await client.check()
    finally:
        await client.aclose()

    assert ready is False
    assert reason is not None
    assert "unreachable" in reason


async def test_check_reports_a_timeout_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow", request=request)

    client = _client(handler)
    try:
        ready, reason = await client.check()
    finally:
        await client.aclose()

    assert ready is False
    assert reason == "timeout after 7s"
