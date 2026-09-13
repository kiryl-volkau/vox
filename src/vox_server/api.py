from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from . import __version__
from .config import Settings, get_settings, redacted_base_url
from .health import ServerState, build_health, gpu_health, uptime_seconds
from .llm import LlmResponseError, LlmTimeoutError, LlmUnavailableError
from .models import (
    ErrorBody,
    HealthResponse,
    LlmHealth,
    ModesResponse,
    ProcessResponse,
    SttHealth,
    TranscribeResponse,
    TransformRequest,
    TransformResponse,
)
from .modes import UnknownModeError
from .processor import EmptyOutputError, EmptyTranscriptError, Processor
from .transcription import TranscriptionError

logger = logging.getLogger(__name__)

router = APIRouter()

REQUEST_ID_HEADER = "X-Request-ID"

_DOMAIN_ERRORS: tuple[tuple[type[Exception], int, str, str], ...] = (
    (EmptyTranscriptError, 422, "empty_transcript", "no speech was recognised"),
    (EmptyOutputError, 502, "empty_llm_output", "the language model returned no usable text"),
    (TranscriptionError, 503, "stt_unavailable", "speech recognition is unavailable"),
    (LlmTimeoutError, 504, "llm_timeout", "the language model timed out"),
    (LlmUnavailableError, 503, "llm_unavailable", "the language model is unreachable"),
    (LlmResponseError, 502, "llm_error", "the language model returned an error"),
)

_HTTP_ERROR_NAMES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    413: "audio_too_large",
    415: "unsupported_media_type",
}


class RequestRejectedError(RuntimeError):
    """A request refused before the pipeline ran: bad upload, or the server not ready yet.

    Carries the HTTP status, the ``ErrorBody.error`` code and a short client-safe detail.
    """

    def __init__(self, status_code: int, error: str, detail: str | None = None) -> None:
        super().__init__(detail or error)
        self.status_code = status_code
        self.error = error
        self.detail = detail


def _request_id(request: Request) -> str:
    request_id: str | None = getattr(request.state, "request_id", None)
    if request_id is None:
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
    return request_id


def _error_response(
    request: Request, status_code: int, error: str, detail: str | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorBody(error=error, detail=detail).model_dump(),
        headers={REQUEST_ID_HEADER: _request_id(request)},
    )


async def _request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = _request_id(request)
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


def _server_state(request: Request) -> ServerState:
    state: ServerState | None = getattr(request.app.state, "server_state", None)
    if state is None:
        raise RequestRejectedError(503, "warming", "the server is still starting up")
    return state


def _ready_processor(state: ServerState, *, needs_stt: bool) -> Processor:
    if needs_stt:
        transcriber = state.transcriber
        if transcriber is None or not transcriber.ready:
            failed = state.stt_error or (transcriber.error if transcriber is not None else None)
            if failed is not None and not state.warming:
                raise RequestRejectedError(
                    503, "stt_unavailable", "speech recognition failed to start"
                )
            raise RequestRejectedError(503, "warming", "speech recognition is still loading")
    processor = state.processor
    if processor is None:
        raise RequestRejectedError(503, "warming", "the server is still starting up")
    return processor


async def _read_upload(audio: UploadFile, settings: Settings, audio_seconds: float | None) -> bytes:
    data = await audio.read()
    if not data:
        raise RequestRejectedError(400, "empty_audio", "the uploaded audio is empty")
    if len(data) > settings.max_audio_bytes:
        raise RequestRejectedError(
            413, "audio_too_large", f"audio exceeds {settings.max_audio_bytes} bytes"
        )
    if audio_seconds is not None and audio_seconds > settings.max_audio_seconds:
        raise RequestRejectedError(
            413, "audio_too_large", f"audio exceeds {settings.max_audio_seconds:.0f} seconds"
        )
    return data


def _log_project(
    request_id: str, name: str | None, project: str | None, settings: Settings
) -> None:
    if not project:
        return
    logger.info(
        "request_id=%s project=%s project_bytes=%d",
        request_id,
        name or "-",
        len(project.encode("utf-8")),
    )
    if settings.log_text:
        logger.debug("request_id=%s project_text=%r", request_id, project)


def _degraded_health(settings: Settings, reason: str, state: ServerState | None) -> HealthResponse:
    return HealthResponse(
        status="degraded",
        version=__version__,
        uptime_s=uptime_seconds(state.started_at) if state is not None else 0.0,
        stt=SttHealth(
            ready=False,
            model=settings.stt_model,
            device=settings.stt_device,
            compute_type=settings.stt_compute_type,
            error=reason,
        ),
        llm=LlmHealth(
            ready=False,
            model=settings.llm_model,
            base_url=redacted_base_url(settings.llm_base_url),
            error=reason,
        ),
        gpu=gpu_health(),
    )


@router.get("/health")
async def health(request: Request) -> HealthResponse:
    """Liveness and readiness snapshot. Always HTTP 200: read ``status`` from the body."""
    state: ServerState | None = getattr(request.app.state, "server_state", None)
    if state is None:
        return _degraded_health(get_settings(), "the server is still starting up", None)
    try:
        return await build_health(state)
    except Exception as exc:
        logger.exception("health check failed")
        return _degraded_health(state.settings, f"health check failed: {exc}", state)


@router.get("/v1/modes")
async def list_modes(request: Request) -> ModesResponse:
    """List the prompt modes the backend loaded from the modes directory."""
    state = _server_state(request)
    modes = state.modes
    if modes is None:
        raise RequestRejectedError(503, "warming", "modes are still loading")
    return ModesResponse(modes=[mode.to_info() for mode in modes.list()])


@router.post("/v1/process")
async def process_audio(
    request: Request,
    audio: Annotated[UploadFile, File()],
    mode: Annotated[str, Form()] = "context",
    project: Annotated[str | None, Form()] = None,
    project_name: Annotated[str | None, Form()] = None,
    client_id: Annotated[str | None, Form()] = None,
    client_version: Annotated[str | None, Form()] = None,
    audio_seconds: Annotated[float | None, Form()] = None,
) -> ProcessResponse:
    """Transcribe the uploaded WAV and return the mode's text, ready to paste."""
    state = _server_state(request)
    processor = _ready_processor(state, needs_stt=True)
    request_id = _request_id(request)
    data = await _read_upload(audio, state.settings, audio_seconds)
    logger.debug(
        "request_id=%s mode=%s client=%s/%s bytes=%d client_audio_s=%s",
        request_id,
        mode,
        client_id,
        client_version,
        len(data),
        audio_seconds,
    )
    _log_project(request_id, project_name, project, state.settings)
    return await processor.process(
        data,
        mode,
        request_id=request_id,
        project=project,
        project_name=project_name,
        client_id=client_id,
        client_version=client_version,
        audio_seconds=audio_seconds,
    )


@router.post("/v1/transcribe")
async def transcribe_audio(
    request: Request,
    audio: Annotated[UploadFile, File()],
    language: Annotated[str | None, Form()] = None,
) -> TranscribeResponse:
    """Return the raw transcript of the uploaded WAV, with no LLM post-processing."""
    state = _server_state(request)
    processor = _ready_processor(state, needs_stt=True)
    request_id = _request_id(request)
    data = await _read_upload(audio, state.settings, None)
    return await processor.transcribe_only(data, request_id=request_id, language=language)


@router.post("/v1/transform")
async def transform_text(request: Request, payload: TransformRequest) -> TransformResponse:
    """Run already-transcribed text through a mode, for replaying or testing prompts."""
    state = _server_state(request)
    processor = _ready_processor(state, needs_stt=False)
    request_id = _request_id(request)
    _log_project(request_id, None, payload.project, state.settings)
    return await processor.transform_only(
        payload.text, payload.mode, request_id=request_id, project=payload.project
    )


async def _internal_error_handler(request: Request, exc: Exception) -> Response:
    logger.exception("unhandled error serving %s", request.url.path, exc_info=exc)
    return _error_response(request, 500, "internal_error", None)


async def _domain_error_handler(request: Request, exc: Exception) -> Response:
    if isinstance(exc, UnknownModeError):
        available = ", ".join(exc.available)
        return _error_response(
            request, 400, "unknown_mode", f"unknown mode '{exc.name}'; available: {available}"
        )
    if isinstance(exc, RequestRejectedError):
        return _error_response(request, exc.status_code, exc.error, exc.detail)
    for exc_type, status_code, error, detail in _DOMAIN_ERRORS:
        if isinstance(exc, exc_type):
            logger.warning("request_id=%s %s: %s", _request_id(request), error, exc)
            return _error_response(request, status_code, error, detail)
    return await _internal_error_handler(request, exc)


async def _validation_error_handler(request: Request, exc: Exception) -> Response:
    detail = "malformed request"
    if isinstance(exc, RequestValidationError):
        errors = exc.errors()
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first["loc"])
            message = str(first["msg"])
            detail = f"{location}: {message}" if location else message
    return _error_response(request, 422, "invalid_request", detail)


async def _http_error_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):
        return await _internal_error_handler(request, exc)
    detail = str(exc.detail) if exc.detail else None
    return _error_response(
        request, exc.status_code, _HTTP_ERROR_NAMES.get(exc.status_code, "http_error"), detail
    )


def register_routes(app: FastAPI) -> None:
    """Attach the request-id middleware and every HTTP route to ``app``."""
    app.middleware("http")(_request_id_middleware)
    app.include_router(router)


def register_exception_handlers(app: FastAPI) -> None:
    """Map domain exceptions onto the project's ErrorBody JSON shape and status codes.

    Bodies never carry a stack trace or the LLM API key; an unexpected exception is logged
    with its traceback and answered with a bare ``internal_error``.
    """
    domain_exceptions: tuple[type[Exception], ...] = (
        UnknownModeError,
        RequestRejectedError,
        EmptyTranscriptError,
        EmptyOutputError,
        TranscriptionError,
        LlmTimeoutError,
        LlmUnavailableError,
        LlmResponseError,
    )
    for exc_type in domain_exceptions:
        app.exception_handler(exc_type)(_domain_error_handler)
    app.exception_handler(RequestValidationError)(_validation_error_handler)
    app.exception_handler(StarletteHTTPException)(_http_error_handler)
    app.exception_handler(Exception)(_internal_error_handler)
