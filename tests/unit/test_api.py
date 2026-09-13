from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vox_server.config import Settings
from vox_server.health import ServerState
from vox_server.llm import LlmResponseError, LlmTimeoutError, LlmUnavailableError
from vox_server.models import (
    ProcessResponse,
    TimingsMs,
    TranscribeResponse,
    TransformResponse,
)
from vox_server.modes import ModeRegistry, UnknownModeError
from vox_server.processor import EmptyOutputError, EmptyTranscriptError, Processor
from vox_server.transcription import TranscriptionError

API_KEY = "sk-super-secret-key"
WAV = b"RIFF----WAVEfmt "

StateFactory = Callable[..., ServerState]
AppFactory = Callable[..., FastAPI]


class FakeProcessor:
    """Answers the API routes without STT or an LLM behind it."""

    def __init__(self, *, output: str = "Проверь membership.", raises: Exception | None = None):
        self.output = output
        self.raises = raises
        self.calls: list[tuple[bytes, str, str]] = []

    async def process(
        self, audio: bytes, mode_name: str, *, request_id: str, language: str | None = None
    ) -> ProcessResponse:
        del language
        self.calls.append((audio, mode_name, request_id))
        if self.raises is not None:
            raise self.raises
        return ProcessResponse(
            request_id=request_id,
            mode=mode_name,
            transcript="посмотри мембершип",
            normalized_text=self.output,
            output=self.output,
            language="ru",
            timings_ms=TimingsMs(transcription=120, llm=340, total=470),
        )

    async def transcribe_only(
        self, audio: bytes, *, request_id: str, language: str | None = None
    ) -> TranscribeResponse:
        del language
        self.calls.append((audio, "transcribe", request_id))
        if self.raises is not None:
            raise self.raises
        return TranscribeResponse(
            request_id=request_id,
            transcript="посмотри мембершип",
            language="ru",
            timings_ms=TimingsMs(transcription=120, llm=0, total=120),
        )

    async def transform_only(
        self, text: str, mode_name: str, *, request_id: str
    ) -> TransformResponse:
        self.calls.append((text.encode("utf-8"), mode_name, request_id))
        if self.raises is not None:
            raise self.raises
        return TransformResponse(
            request_id=request_id,
            mode=mode_name,
            normalized_text=self.output,
            output=self.output,
            timings_ms=TimingsMs(transcription=0, llm=340, total=340),
        )


def _upload(data: bytes = WAV) -> dict[str, tuple[str, bytes, str]]:
    return {"audio": ("audio.wav", data, "audio/wav")}


def _with_processor(state: ServerState, processor: FakeProcessor) -> ServerState:
    state.processor = cast(Processor, processor)
    return state


@pytest.fixture
def ready_state(
    state_factory: StateFactory,
    settings_factory: Callable[..., Settings],
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    repo_root: Path,
) -> ServerState:
    return state_factory(
        settings=settings_factory(llm_api_key=API_KEY),
        transcriber=transcriber_factory(ready=True),
        llm=llm_factory(base_url=f"http://user:{API_KEY}@ollama:11434/v1"),
        modes=ModeRegistry.load(repo_root / "modes"),
        processor=FakeProcessor(),
        llm_ready=True,
        warming=False,
    )


def test_health_is_ready_when_stt_and_the_llm_answer(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    client = TestClient(app_factory(ready_state))

    response = client.get("/health")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["version"]
    assert body["uptime_s"] >= 0.0
    assert body["stt"]["ready"] is True
    assert body["stt"]["model"] == "fake-whisper"
    assert body["llm"]["ready"] is True
    assert body["llm"]["model"] == "fake-llm"
    assert body["llm"]["base_url"] == "http://ollama:11434/v1"
    assert set(body["gpu"]) == {"cuda_available", "device_count"}


def test_health_never_leaks_the_api_key(ready_state: ServerState, app_factory: AppFactory) -> None:
    client = TestClient(app_factory(ready_state))

    response = client.get("/health")

    assert API_KEY not in response.text
    assert "@" not in response.json()["llm"]["base_url"]


def test_health_is_degraded_when_nothing_started(
    state_factory: StateFactory, app_factory: AppFactory
) -> None:
    client = TestClient(app_factory(state_factory(warming=False)))

    body = client.get("/health").json()

    assert body["status"] == "degraded"
    assert body["stt"]["ready"] is False
    assert body["llm"]["ready"] is False
    assert body["llm"]["error"]


def test_health_is_warming_while_stt_loads(
    state_factory: StateFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    app_factory: AppFactory,
) -> None:
    state = state_factory(
        transcriber=transcriber_factory(ready=False),
        llm=llm_factory(check_result=(False, "unreachable (ConnectError)")),
        warming=True,
    )
    client = TestClient(app_factory(state))

    body = client.get("/health").json()

    assert body["status"] == "warming"
    assert body["llm"]["error"] == "unreachable (ConnectError)"


def test_health_never_reports_ready_while_startup_is_still_running(
    state_factory: StateFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    app_factory: AppFactory,
) -> None:
    """STT and the LLM can both be up while the processor has not been published yet.

    Reporting "ready" in that window makes start.ps1 launch the companion straight into a
    503 "warming" on the first voice request.
    """
    state = state_factory(
        transcriber=transcriber_factory(ready=True),
        llm=llm_factory(check_result=(True, None)),
        processor=None,
        warming=True,
    )
    client = TestClient(app_factory(state))

    body = client.get("/health").json()

    assert body["stt"]["ready"] is True
    assert body["llm"]["ready"] is True
    assert body["status"] == "warming"


def test_health_answers_even_without_any_server_state(app_factory: AppFactory) -> None:
    client = TestClient(app_factory())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_modes_lists_what_the_registry_loaded(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    client = TestClient(app_factory(ready_state))

    body = client.get("/v1/modes").json()
    names = [mode["name"] for mode in body["modes"]]

    assert names == ["clean", "context", "dictation", "task"]
    context = next(mode for mode in body["modes"] if mode["name"] == "context")
    assert context["wrap_for_claude"] is True
    assert context["requires_llm"] is True
    assert context["label"]


def test_process_returns_the_documented_payload(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    client = TestClient(app_factory(ready_state))

    response = client.post(
        "/v1/process",
        files=_upload(),
        data={"mode": "context", "client_id": "vox-windows", "audio_seconds": "1.25"},
    )
    body = response.json()

    assert response.status_code == 200
    assert set(body) == {
        "request_id",
        "mode",
        "transcript",
        "normalized_text",
        "output",
        "language",
        "timings_ms",
    }
    assert body["mode"] == "context"
    assert body["output"] == "Проверь membership."
    assert set(body["timings_ms"]) == {"transcription", "llm", "total"}
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert len(body["request_id"]) == 12


def test_process_forwards_the_uploaded_bytes_and_mode(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    processor = FakeProcessor()
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    client.post("/v1/process", files=_upload(b"RIFFabcdWAVEfmt "), data={"mode": "task"})

    audio, mode, _request_id = processor.calls[0]
    assert audio == b"RIFFabcdWAVEfmt "
    assert mode == "task"


def test_process_defaults_to_the_context_mode(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    processor = FakeProcessor()
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    client.post("/v1/process", files=_upload())

    assert processor.calls[0][1] == "context"


def test_an_empty_upload_is_rejected(ready_state: ServerState, app_factory: AppFactory) -> None:
    client = TestClient(app_factory(ready_state))

    response = client.post("/v1/process", files=_upload(b""), data={"mode": "context"})

    assert response.status_code == 400
    assert response.json()["error"] == "empty_audio"
    assert response.headers["X-Request-ID"]


def test_an_oversized_upload_is_rejected(
    state_factory: StateFactory,
    settings_factory: Callable[..., Settings],
    transcriber_factory: Callable[..., Any],
    app_factory: AppFactory,
) -> None:
    state = state_factory(
        settings=settings_factory(max_audio_bytes=16),
        transcriber=transcriber_factory(ready=True),
        processor=FakeProcessor(),
    )
    client = TestClient(app_factory(state))

    response = client.post("/v1/process", files=_upload(b"x" * 64), data={"mode": "context"})

    assert response.status_code == 413
    assert response.json()["error"] == "audio_too_large"


def test_audio_longer_than_the_limit_is_rejected(
    state_factory: StateFactory,
    settings_factory: Callable[..., Settings],
    transcriber_factory: Callable[..., Any],
    app_factory: AppFactory,
) -> None:
    state = state_factory(
        settings=settings_factory(max_audio_seconds=5.0),
        transcriber=transcriber_factory(ready=True),
        processor=FakeProcessor(),
    )
    client = TestClient(app_factory(state))

    response = client.post(
        "/v1/process", files=_upload(), data={"mode": "context", "audio_seconds": "42"}
    )

    assert response.status_code == 413
    assert response.json()["error"] == "audio_too_large"


def test_an_unknown_mode_reports_the_available_modes(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    unknown = UnknownModeError("bogus", ["clean", "context", "dictation", "task"])
    state = _with_processor(ready_state, FakeProcessor(raises=unknown))
    client = TestClient(app_factory(state))

    response = client.post("/v1/process", files=_upload(), data={"mode": "bogus"})
    body = response.json()

    assert response.status_code == 400
    assert body["error"] == "unknown_mode"
    assert body["detail"] is not None
    assert "bogus" in body["detail"]
    assert "context" in body["detail"]


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (EmptyTranscriptError("nothing"), 422, "empty_transcript"),
        (EmptyOutputError("nothing"), 502, "empty_llm_output"),
        (TranscriptionError("cuda died"), 503, "stt_unavailable"),
        (LlmTimeoutError("slow"), 504, "llm_timeout"),
        (LlmUnavailableError("down"), 503, "llm_unavailable"),
        (LlmResponseError("HTTP 500"), 502, "llm_error"),
    ],
)
def test_domain_errors_map_to_their_documented_status(
    ready_state: ServerState,
    app_factory: AppFactory,
    error: Exception,
    status: int,
    code: str,
) -> None:
    state = _with_processor(ready_state, FakeProcessor(raises=error))
    client = TestClient(app_factory(state))

    response = client.post("/v1/process", files=_upload(), data={"mode": "context"})

    assert response.status_code == status
    assert response.json()["error"] == code
    assert response.headers["X-Request-ID"]


def test_an_unexpected_error_becomes_a_bare_internal_error(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    exploding = FakeProcessor(raises=RuntimeError(f"boom while talking to {API_KEY}"))
    state = _with_processor(ready_state, exploding)
    client = TestClient(app_factory(state), raise_server_exceptions=False)

    response = client.post("/v1/process", files=_upload(), data={"mode": "context"})
    body = response.json()

    assert response.status_code == 500
    assert body == {"error": "internal_error", "detail": None}
    assert API_KEY not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


def test_process_is_refused_while_stt_is_still_loading(
    state_factory: StateFactory,
    transcriber_factory: Callable[..., Any],
    app_factory: AppFactory,
) -> None:
    state = state_factory(
        transcriber=transcriber_factory(ready=False), processor=FakeProcessor(), warming=True
    )
    client = TestClient(app_factory(state))

    response = client.post("/v1/process", files=_upload(), data={"mode": "context"})

    assert response.status_code == 503
    assert response.json()["error"] == "warming"


def test_process_reports_a_failed_stt_start_once_warming_is_over(
    state_factory: StateFactory,
    transcriber_factory: Callable[..., Any],
    app_factory: AppFactory,
) -> None:
    state = state_factory(
        transcriber=transcriber_factory(ready=False),
        processor=FakeProcessor(),
        stt_error="CUDA out of memory",
        warming=False,
    )
    client = TestClient(app_factory(state))

    response = client.post("/v1/process", files=_upload(), data={"mode": "context"})

    assert response.status_code == 503
    assert response.json()["error"] == "stt_unavailable"


def test_requests_before_any_state_exists_are_refused(app_factory: AppFactory) -> None:
    client = TestClient(app_factory())

    response = client.post("/v1/process", files=_upload(), data={"mode": "context"})

    assert response.status_code == 503
    assert response.json()["error"] == "warming"


def test_transcribe_returns_the_raw_transcript(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    client = TestClient(app_factory(ready_state))

    response = client.post("/v1/transcribe", files=_upload(), data={"language": "ru"})
    body = response.json()

    assert response.status_code == 200
    assert set(body) == {"request_id", "transcript", "language", "timings_ms"}
    assert body["transcript"] == "посмотри мембершип"


def test_transform_runs_without_speech_recognition(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    ready_state.transcriber = None
    client = TestClient(app_factory(ready_state))

    response = client.post("/v1/transform", json={"text": "посмотри мембершип", "mode": "clean"})
    body = response.json()

    assert response.status_code == 200
    assert body["mode"] == "clean"
    assert body["output"] == "Проверь membership."


def test_a_malformed_json_body_is_a_validation_error(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    client = TestClient(app_factory(ready_state))

    response = client.post("/v1/transform", json={"mode": "clean"})
    body = response.json()

    assert response.status_code == 422
    assert body["error"] == "invalid_request"
    assert "text" in str(body["detail"])


def test_an_unknown_route_returns_the_error_body_shape(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    client = TestClient(app_factory(ready_state))

    response = client.get("/v1/nope")
    body = response.json()

    assert response.status_code == 404
    assert body["error"] == "not_found"
    assert response.headers["X-Request-ID"]


def test_health_probes_the_llm_with_a_short_budget(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    """A firewalled endpoint that black-holes packets must not stall /health.

    Without a per-probe budget the probe inherits LLM_TIMEOUT_SECONDS (30s by default), which
    is longer than the Docker healthcheck timeout and than start.ps1's poll interval.
    """
    client = TestClient(app_factory(ready_state))

    client.get("/health")

    llm = ready_state.llm
    assert llm is not None
    recorded = llm.check_timeouts  # type: ignore[attr-defined]
    assert recorded, "the health route did not probe the LLM"
    assert all(t is not None and t <= 5.0 for t in recorded), recorded
