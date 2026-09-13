from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from vox_client.api_client import (
    CLIENT_ID,
    ApiError,
    ApiTimeoutError,
    ApiUnavailableError,
    ProcessResult,
    VoiceCodeClient,
    message_for_code,
)
from vox_client.config import ServerConfig

WAV = b"RIFF0000WAVEfmt this-is-the-audio"

Handler = Callable[[httpx.Request], httpx.Response]

PROJECT_TEXT = """# jigward

membership - подписка пользователя, не участие в группе.
"""

PROCESS_BODY = {
    "request_id": "abc123def456",
    "mode": "context",
    "transcript": "посмотри мембершип",
    "normalized_text": "Проверь membership.",
    "output": "VOICE TASK\nПроверь membership.",
    "language": "ru",
    "timings_ms": {"transcription": 120, "llm": 340, "total": 470},
}


def _client(handler: Handler) -> VoiceCodeClient:
    config = ServerConfig(base_url="http://127.0.0.1:8765/")
    return VoiceCodeClient(config, client=httpx.Client(transport=httpx.MockTransport(handler)))


def _json(payload: object, status: int = 200) -> Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def test_process_parses_the_backend_payload() -> None:
    client = _client(_json(PROCESS_BODY))
    try:
        result = client.process(WAV, "context", audio_seconds=1.25)
    finally:
        client.close()

    assert isinstance(result, ProcessResult)
    assert result.request_id == "abc123def456"
    assert result.mode == "context"
    assert result.output == "VOICE TASK\nПроверь membership."
    assert result.transcript == "посмотри мембершип"
    assert result.language == "ru"
    assert result.server_ms == {"transcription": 120, "llm": 340, "total": 470}


def test_process_posts_the_audio_and_the_metadata() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PROCESS_BODY)

    client = _client(handler)
    try:
        client.process(WAV, "task", audio_seconds=2.5)
    finally:
        client.close()

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "http://127.0.0.1:8765/v1/process"
    assert request.headers["content-type"].startswith("multipart/form-data")

    body = request.content
    assert b'name="mode"' in body
    assert b"task" in body
    assert b'name="audio"; filename="audio.wav"' in body
    assert WAV in body
    assert b'name="audio_seconds"' in body
    assert b"2.500" in body
    assert CLIENT_ID.encode() in body


def test_project_context_is_posted_as_its_own_fields() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PROCESS_BODY)

    client = _client(handler)
    try:
        client.process(
            WAV,
            "context",
            audio_seconds=1.0,
            project=PROJECT_TEXT,
            project_name="jigward",
        )
    finally:
        client.close()

    body = seen[0].content
    assert b'name="project"' in body
    assert PROJECT_TEXT.encode() in body
    assert b'name="project_name"' in body
    assert b"jigward" in body


def test_a_request_without_a_project_omits_both_fields() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PROCESS_BODY)

    client = _client(handler)
    try:
        client.process(WAV, "context", audio_seconds=1.0)
    finally:
        client.close()

    body = seen[0].content
    assert b'name="project"' not in body
    assert b'name="project_name"' not in body


def test_a_missing_output_field_is_a_malformed_reply() -> None:
    client = _client(_json({"request_id": "x", "mode": "context"}))
    try:
        with pytest.raises(ApiError) as excinfo:
            client.process(WAV, "context", audio_seconds=1.0)
    finally:
        client.close()

    assert excinfo.value.code == "bad_response"


def test_a_non_json_body_is_a_malformed_reply() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>nope</html>")

    client = _client(handler)
    try:
        with pytest.raises(ApiError) as excinfo:
            client.process(WAV, "context", audio_seconds=1.0)
    finally:
        client.close()

    assert excinfo.value.code == "bad_response"


def test_odd_timings_are_dropped_instead_of_crashing() -> None:
    payload = dict(PROCESS_BODY, timings_ms="not-a-mapping")
    client = _client(_json(payload))
    try:
        result = client.process(WAV, "context", audio_seconds=1.0)
    finally:
        client.close()

    assert result.server_ms == {}


def test_a_refused_connection_is_reported_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)
    try:
        with pytest.raises(ApiUnavailableError) as excinfo:
            client.process(WAV, "context", audio_seconds=1.0)
    finally:
        client.close()

    assert str(excinfo.value) == "Service unavailable"
    assert excinfo.value.code == "unavailable"


def test_a_read_timeout_is_reported_as_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client = _client(handler)
    try:
        with pytest.raises(ApiTimeoutError) as excinfo:
            client.process(WAV, "context", audio_seconds=1.0)
    finally:
        client.close()

    assert excinfo.value.code == "timeout"


def test_a_connect_timeout_is_reported_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route", request=request)

    client = _client(handler)
    try:
        with pytest.raises(ApiUnavailableError):
            client.process(WAV, "context", audio_seconds=1.0)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("status", "code", "message"),
    [
        (503, "llm_unavailable", "Model unavailable"),
        (503, "stt_unavailable", "Whisper unavailable"),
        (503, "warming", "Backend warming up"),
        (422, "empty_transcript", "Nothing recognised"),
        (400, "unknown_mode", "Unknown mode"),
        (413, "audio_too_large", "Recording too long"),
        (502, "empty_llm_output", "Model returned nothing"),
        (500, "internal_error", "Backend error"),
        (418, "some_new_code", "Backend error"),
    ],
)
def test_a_backend_error_becomes_a_short_overlay_message(
    status: int, code: str, message: str
) -> None:
    client = _client(_json({"error": code, "detail": "the long technical story"}, status=status))
    try:
        with pytest.raises(ApiError) as excinfo:
            client.process(WAV, "context", audio_seconds=1.0)
    finally:
        client.close()

    assert excinfo.value.code == code
    assert excinfo.value.status == status
    assert str(excinfo.value) == message
    assert excinfo.value.message == message
    assert "the long technical story" not in str(excinfo.value)


def test_an_llm_timeout_status_becomes_a_timeout_error() -> None:
    client = _client(_json({"error": "llm_timeout"}, status=504))
    try:
        with pytest.raises(ApiTimeoutError) as excinfo:
            client.process(WAV, "context", audio_seconds=1.0)
    finally:
        client.close()

    assert excinfo.value.code == "llm_timeout"


def test_an_error_status_without_a_json_body_still_maps() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    client = _client(handler)
    try:
        with pytest.raises(ApiError) as excinfo:
            client.process(WAV, "context", audio_seconds=1.0)
    finally:
        client.close()

    assert excinfo.value.code == "internal_error"
    assert str(excinfo.value) == "Backend error"


def test_health_returns_the_parsed_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ready", "version": "0.1.0"})

    client = _client(handler)
    try:
        body = client.health()
    finally:
        client.close()

    assert body["status"] == "ready"
    assert seen[0].method == "GET"
    assert str(seen[0].url) == "http://127.0.0.1:8765/health"


def test_health_reports_an_unreachable_backend() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)
    try:
        with pytest.raises(ApiUnavailableError):
            client.health()
    finally:
        client.close()


@pytest.mark.parametrize(
    ("code", "message"),
    [("llm_timeout", "Model timeout"), ("", "Backend error"), ("nope", "Backend error")],
)
def test_message_for_code_never_shows_a_raw_slug(code: str, message: str) -> None:
    assert message_for_code(code) == message
