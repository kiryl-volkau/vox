from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from vox_client import __version__
from vox_client.config import ServerConfig

logger = logging.getLogger(__name__)

CLIENT_ID = "vox-windows"
CLIENT_VERSION = __version__

GENERIC_ERROR_MESSAGE = "Backend error"

ERROR_MESSAGES: dict[str, str] = {
    "unknown_mode": "Unknown mode",
    "empty_audio": "No audio captured",
    "audio_too_large": "Recording too long",
    "empty_transcript": "Nothing recognised",
    "empty_llm_output": "Model returned nothing",
    "stt_unavailable": "Whisper unavailable",
    "llm_timeout": "Model timeout",
    "llm_unavailable": "Model unavailable",
    "llm_error": "Model error",
    "warming": "Backend warming up",
    "internal_error": "Backend error",
}


def message_for_code(code: str) -> str:
    """Return a short overlay-ready message for a backend ``ErrorBody.error`` code.

    Unknown codes map to the generic "Backend error" so the overlay never shows a raw
    machine code to the user.
    """
    return ERROR_MESSAGES.get(code, GENERIC_ERROR_MESSAGE)


class ApiError(RuntimeError):
    """A backend call failed.

    ``message`` is short enough for the overlay, ``code`` is the backend's
    ``ErrorBody.error`` slug (or a client-side slug such as ``"bad_response"``), and
    ``status`` is the HTTP status when the failure came from a response.
    """

    def __init__(self, message: str, *, code: str = "error", status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class ApiUnavailableError(ApiError):
    """The backend could not be reached at all."""


class ApiTimeoutError(ApiError):
    """The backend accepted the request but did not answer in time."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """One finished /v1/process round trip.

    ``server_ms`` holds the backend's own timings (keys ``transcription``, ``llm``,
    ``total``) in milliseconds; it is empty when the backend omitted them.
    """

    request_id: str
    mode: str
    output: str
    transcript: str
    language: str
    server_ms: dict[str, int]


class VoiceCodeClient:
    """Blocking HTTP client for the vox backend.

    Every method raises :class:`ApiError` (or one of its subclasses) on failure and never
    lets an ``httpx`` exception escape. Instances are safe to share between threads.
    """

    def __init__(self, config: ServerConfig, client: httpx.Client | None = None) -> None:
        self._base_url = config.base_url.rstrip("/")
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(config.timeout_seconds, connect=config.connect_timeout_seconds),
        )

    def process(self, wav: bytes, mode: str, *, audio_seconds: float) -> ProcessResult:
        """Upload ``wav`` for transcription and mode processing.

        ``audio_seconds`` is the captured duration and is sent as request metadata only.
        Raises :class:`ApiUnavailableError` when the backend is down,
        :class:`ApiTimeoutError` when it is too slow, :class:`ApiError` otherwise.
        """
        response = self._call(
            "POST",
            "/v1/process",
            files={"audio": ("audio.wav", wav, "audio/wav")},
            data={
                "mode": mode,
                "audio_seconds": f"{audio_seconds:.3f}",
                "client_id": CLIENT_ID,
                "client_version": CLIENT_VERSION,
            },
        )
        payload = self._payload(response)
        output = payload.get("output")
        if not isinstance(output, str):
            raise ApiError("Malformed backend reply", code="bad_response", status=200)
        return ProcessResult(
            request_id=_text(payload, "request_id"),
            mode=_text(payload, "mode") or mode,
            output=output,
            transcript=_text(payload, "transcript"),
            language=_text(payload, "language"),
            server_ms=_timings(payload),
        )

    def health(self) -> dict[str, object]:
        """Return the parsed /health body. Raises :class:`ApiError` when unreachable."""
        response = self._call("GET", "/health")
        return dict(self._payload(response))

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self._base_url}{path}"
        try:
            return self._client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ApiUnavailableError("Service unavailable", code="unavailable") from exc
        except httpx.TimeoutException as exc:
            raise ApiTimeoutError("Backend timeout", code="timeout") from exc
        except httpx.RequestError as exc:
            raise ApiUnavailableError("Service unavailable", code="unavailable") from exc

    def _payload(self, response: httpx.Response) -> Mapping[str, Any]:
        if response.status_code >= 400:
            raise self._failure(response)
        try:
            body = response.json()
        except ValueError as exc:
            raise ApiError(
                "Malformed backend reply", code="bad_response", status=response.status_code
            ) from exc
        if not isinstance(body, dict):
            raise ApiError(
                "Malformed backend reply", code="bad_response", status=response.status_code
            )
        return body

    def _failure(self, response: httpx.Response) -> ApiError:
        code = "internal_error"
        detail: str | None = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            raw_code = body.get("error")
            if isinstance(raw_code, str) and raw_code:
                code = raw_code
            raw_detail = body.get("detail")
            if isinstance(raw_detail, str):
                detail = raw_detail
        logger.warning(
            "backend rejected request: status=%s code=%s detail=%s",
            response.status_code,
            code,
            detail,
        )
        message = message_for_code(code)
        error_type = ApiTimeoutError if code == "llm_timeout" else ApiError
        return error_type(message, code=code, status=response.status_code)


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _timings(payload: Mapping[str, Any]) -> dict[str, int]:
    raw = payload.get("timings_ms")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, int)}
