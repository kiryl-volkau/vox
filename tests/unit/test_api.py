from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import make_settings
from vox_server import runtime_config
from vox_server.config import Settings
from vox_server.health import ServerState
from vox_server.llm import LlmResponseError, LlmTimeoutError, LlmUnavailableError
from vox_server.models import (
    ProcessResponse,
    TimingsMs,
    TransformResponse,
)
from vox_server.processor import EmptyTranscriptError, Processor
from vox_server.prompt import Prompt
from vox_server.transcription import TranscriptionError

API_KEY = "sk-super-secret-key"
WAV = b"RIFF----WAVEfmt "
PROJECT = "Термины: джигвард -> Jigward.\nМиграции уже применены."
PROJECT_HEADER = "<project_context>"
CONVERSATION_MARKER = "БЕСЕДА-МАРКЕР-42"
CONVERSATION = f"user: {CONVERSATION_MARKER} почини авторизацию\nassistant: починил SecurityConfig"
MAX_PROJECT_BYTES = 512
PROJECT_LINES = tuple(
    f"ПРАВИЛО-{index:03d}-НАЧАЛО: не трогай миграции ПРАВИЛО-{index:03d}-КОНЕЦ"
    for index in range(64)
)
OVERSIZED_PROJECT = "\n".join(PROJECT_LINES)

StateFactory = Callable[..., ServerState]
AppFactory = Callable[..., FastAPI]


class FakeProcessor:
    """Answers the API routes without STT or an LLM behind it.

    ``projects`` records the project text every project-aware route forwarded, one entry per
    call, so a route that forwards nothing is distinguishable from one that forwards "".
    ``provenance`` records the (project_name, client_id, client_version, audio_seconds) the
    route reported about its caller. ``dictation_calls`` records the ``dictation`` flag each
    call carried. ``contexts``
    and ``languages`` record the conversation context and the output language per call.
    """

    def __init__(self, *, output: str = "Проверь membership.", raises: Exception | None = None):
        self.output = output
        self.raises = raises
        self.calls: list[tuple[bytes, str]] = []
        self.projects: list[str] = []
        self.contexts: list[str | None] = []
        self.languages: list[str | None] = []
        self.provenance: list[tuple[str | None, str | None, str | None, float | None]] = []
        self.dictation_calls: list[bool] = []

    async def process(
        self,
        audio: bytes,
        *,
        request_id: str,
        dictation: bool = False,
        language: str | None = None,
        project: str = "",
        project_name: str | None = None,
        context: str | None = None,
        client_id: str | None = None,
        client_version: str | None = None,
        audio_seconds: float | None = None,
    ) -> ProcessResponse:
        self.calls.append((audio, request_id))
        self.projects.append(project)
        self.contexts.append(context)
        self.languages.append(language)
        self.provenance.append((project_name, client_id, client_version, audio_seconds))
        self.dictation_calls.append(dictation)
        if self.raises is not None:
            raise self.raises
        return ProcessResponse(
            request_id=request_id,
            transcript="посмотри мембершип",
            output=self.output,
            language="ru",
            timings_ms=TimingsMs(transcription=120, llm=340, total=470),
        )

    async def transform_only(
        self,
        text: str,
        *,
        request_id: str,
        dictation: bool = False,
        project: str = "",
        context: str | None = None,
        language: str | None = None,
    ) -> TransformResponse:
        self.calls.append((text.encode("utf-8"), request_id))
        self.projects.append(project)
        self.contexts.append(context)
        self.languages.append(language)
        self.dictation_calls.append(dictation)
        if self.raises is not None:
            raise self.raises
        return TransformResponse(
            request_id=request_id,
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
    prompt_factory: Callable[..., Prompt],
) -> ServerState:
    return state_factory(
        settings=settings_factory(llm_api_key=API_KEY),
        transcriber=transcriber_factory(ready=True),
        llm=llm_factory(base_url=f"http://user:{API_KEY}@ollama:11434/v1"),
        prompt=prompt_factory(),
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

    Reporting "ready" in that window sends the first request straight into a
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


def test_process_returns_the_documented_payload(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    client = TestClient(app_factory(ready_state))

    response = client.post(
        "/v1/process",
        files=_upload(),
        data={"client_id": "vox-windows", "audio_seconds": "1.25"},
    )
    body = response.json()

    assert response.status_code == 200
    assert set(body) == {
        "request_id",
        "transcript",
        "output",
        "language",
        "timings_ms",
        "analysis",
    }
    assert body["output"] == "Проверь membership."
    assert set(body["timings_ms"]) == {"transcription", "llm", "total"}
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert len(body["request_id"]) == 12


def test_process_forwards_the_uploaded_bytes(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    processor = FakeProcessor()
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    client.post("/v1/process", files=_upload(b"RIFFabcdWAVEfmt "))

    audio, _request_id = processor.calls[0]
    assert audio == b"RIFFabcdWAVEfmt "


def test_process_tells_the_processor_this_is_not_dictation(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    processor = FakeProcessor()
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    client.post("/v1/process", files=_upload())

    assert processor.dictation_calls == [False]


def test_an_empty_upload_is_rejected(ready_state: ServerState, app_factory: AppFactory) -> None:
    client = TestClient(app_factory(ready_state))

    response = client.post("/v1/process", files=_upload(b""))

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

    response = client.post("/v1/process", files=_upload(b"x" * 64))

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

    response = client.post("/v1/process", files=_upload(), data={"audio_seconds": "42"})

    assert response.status_code == 413
    assert response.json()["error"] == "audio_too_large"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (EmptyTranscriptError("nothing"), 422, "empty_transcript"),
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

    response = client.post("/v1/process", files=_upload())

    assert response.status_code == status
    assert response.json()["error"] == code
    assert response.headers["X-Request-ID"]


def test_an_empty_model_reply_falls_back_to_the_transcript_on_process(
    state_factory: StateFactory,
    settings_factory: Callable[..., Settings],
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    processor_factory: Callable[..., Processor],
    app_factory: AppFactory,
) -> None:
    settings = settings_factory()
    transcriber = transcriber_factory("посмотри мембершип", ready=True)
    llm = llm_factory("")
    state = state_factory(
        settings=settings,
        transcriber=transcriber,
        llm=llm,
        processor=processor_factory(transcriber, llm, settings=settings),
        llm_ready=True,
    )
    client = TestClient(app_factory(state))

    response = client.post("/v1/process", files=_upload())
    body = response.json()

    assert response.status_code == 200
    assert body["output"] == body["transcript"] == "посмотри мембершип"


def test_an_unexpected_error_becomes_a_bare_internal_error(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    exploding = FakeProcessor(raises=RuntimeError(f"boom while talking to {API_KEY}"))
    state = _with_processor(ready_state, exploding)
    client = TestClient(app_factory(state), raise_server_exceptions=False)

    response = client.post("/v1/process", files=_upload())
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

    response = client.post("/v1/process", files=_upload())

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

    response = client.post("/v1/process", files=_upload())

    assert response.status_code == 503
    assert response.json()["error"] == "stt_unavailable"


def test_requests_before_any_state_exists_are_refused(app_factory: AppFactory) -> None:
    client = TestClient(app_factory())

    response = client.post("/v1/process", files=_upload())

    assert response.status_code == 503
    assert response.json()["error"] == "warming"


def test_transform_runs_without_speech_recognition(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    ready_state.transcriber = None
    client = TestClient(app_factory(ready_state))

    response = client.post("/v1/transform", json={"text": "посмотри мембершип"})
    body = response.json()

    assert response.status_code == 200
    assert body["output"] == "Проверь membership."


def test_transform_forwards_the_dictation_flag(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    processor = FakeProcessor()
    ready_state.transcriber = None
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    client.post("/v1/transform", json={"text": "посмотри мембершип", "dictation": True})

    assert processor.dictation_calls == [True]


def test_an_empty_model_reply_falls_back_to_the_source_text_on_transform(
    state_factory: StateFactory,
    settings_factory: Callable[..., Settings],
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    processor_factory: Callable[..., Processor],
    app_factory: AppFactory,
) -> None:
    settings = settings_factory()
    llm = llm_factory("")
    state = state_factory(
        settings=settings,
        transcriber=transcriber_factory(),
        llm=llm,
        processor=processor_factory(transcriber_factory(), llm, settings=settings),
        llm_ready=True,
    )
    client = TestClient(app_factory(state))

    response = client.post("/v1/transform", json={"text": "посмотри мембершип"})
    body = response.json()

    assert response.status_code == 200
    assert body["output"] == "посмотри мембершип"


def test_a_malformed_json_body_is_a_validation_error(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    client = TestClient(app_factory(ready_state))

    response = client.post("/v1/transform", json={})
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
    is longer than the Docker healthcheck timeout.
    """
    client = TestClient(app_factory(ready_state))

    client.get("/health")

    llm = ready_state.llm
    assert llm is not None
    recorded = llm.check_timeouts  # type: ignore[attr-defined]
    assert recorded, "the health route did not probe the LLM"
    assert all(t is not None and t <= 5.0 for t in recorded), recorded


def test_process_accepts_the_project_form_fields(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    """The client sends .vox.md as a form field; project_name is for logs only.

    FakeProcessor takes no project_name argument, so a route that forwarded it down the
    pipeline would fail here with a TypeError instead of quietly reaching the model.
    """
    processor = FakeProcessor()
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    response = client.post(
        "/v1/process",
        files=_upload(),
        data={
            "project": PROJECT,
            "project_name": "jigward",
            "client_id": "vox-idea",
            "client_version": "0.1.0",
        },
    )

    assert response.status_code == 200
    assert processor.projects == [PROJECT]


def test_process_forwards_the_conversation_context_and_the_language(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    processor = FakeProcessor()
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    response = client.post(
        "/v1/process",
        files=_upload(),
        data={"context": CONVERSATION, "language": "ru"},
    )

    assert response.status_code == 200
    assert processor.contexts == [CONVERSATION]
    assert processor.languages == ["ru"]


def test_a_request_without_a_context_or_language_forwards_nothing(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    """Both fields are optional; an old client that sends neither must keep working."""
    processor = FakeProcessor()
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    response = client.post("/v1/process", files=_upload())

    assert response.status_code == 200
    assert processor.contexts == [None]
    assert processor.languages == [None]


def test_transform_accepts_a_context_and_a_language_in_the_json_body(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    processor = FakeProcessor()
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    response = client.post(
        "/v1/transform",
        json={"text": "посмотри мембершип", "context": CONVERSATION, "language": "ru"},
    )

    assert response.status_code == 200
    assert processor.contexts == [CONVERSATION]
    assert processor.languages == ["ru"]


def test_the_conversation_context_is_never_logged_without_log_text(
    ready_state: ServerState, app_factory: AppFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """It carries whatever was said to the coding agent, so it is held to the transcript rule."""
    processor = FakeProcessor()
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    with caplog.at_level(logging.DEBUG):
        response = client.post("/v1/process", files=_upload(), data={"context": CONVERSATION})

    assert response.status_code == 200
    assert CONVERSATION_MARKER not in caplog.text
    assert "context_bytes=" in caplog.text


def test_process_forwards_what_the_caller_said_about_itself(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    """Provenance is recorded, not acted on; the processor needs it for the log and the trace."""
    processor = FakeProcessor()
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    client.post(
        "/v1/process",
        files=_upload(),
        data={
            "project": PROJECT,
            "project_name": "jigward",
            "client_id": "vox-idea",
            "client_version": "0.1.0",
            "audio_seconds": "1.25",
        },
    )

    assert processor.provenance == [("jigward", "vox-idea", "0.1.0", 1.25)]


def test_process_reports_an_anonymous_caller_as_nothing_at_all(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    processor = FakeProcessor()
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    client.post("/v1/process", files=_upload())

    assert processor.provenance == [(None, None, None, None)]


def test_process_works_without_any_project(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    processor = FakeProcessor()
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    response = client.post("/v1/process", files=_upload())

    assert response.status_code == 200
    assert not processor.projects[0]


def test_transform_accepts_a_project_in_the_json_body(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    processor = FakeProcessor()
    ready_state.transcriber = None
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    response = client.post(
        "/v1/transform",
        json={"text": "посмотри мембершип", "project": PROJECT},
    )

    assert response.status_code == 200
    assert processor.projects == [PROJECT]


def test_transform_works_without_a_project(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    processor = FakeProcessor()
    ready_state.transcriber = None
    client = TestClient(app_factory(_with_processor(ready_state, processor)))

    response = client.post("/v1/transform", json={"text": "посмотри мембершип"})

    assert response.status_code == 200
    assert not processor.projects[0]


def test_an_oversized_project_is_truncated_instead_of_rejected(
    state_factory: StateFactory,
    settings_factory: Callable[..., Settings],
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    prompt_factory: Callable[..., Prompt],
    processor_factory: Callable[..., Processor],
    app_factory: AppFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A .vox.md over the limit is cut on a line boundary and the request still succeeds.

    Drives the real Processor and reads the result off the system prompt the LLM received,
    so the assertion holds wherever the cut is made. Half a project rule is worse than none,
    so no partially copied line may survive.
    """
    settings = settings_factory(max_project_bytes=MAX_PROJECT_BYTES)
    llm = llm_factory("Проверь membership.")
    transcriber = transcriber_factory(ready=True)
    prompt = prompt_factory(system_prompt="Ты редактор инженерных запросов.\n\n{project}")
    state = state_factory(
        settings=settings,
        transcriber=transcriber,
        llm=llm,
        prompt=prompt,
        processor=processor_factory(transcriber, llm, prompt, settings=settings),
        llm_ready=True,
    )
    client = TestClient(app_factory(state))

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/v1/process",
            files=_upload(),
            data={"project": OVERSIZED_PROJECT, "project_name": "jigward"},
        )

    assert response.status_code == 200
    system, user, _temperature = llm.calls[0]
    kept = [line for line in PROJECT_LINES if line in system]
    assert kept, "the whole project was dropped instead of truncated"
    assert kept == list(PROJECT_LINES[: len(kept)])
    assert len("\n".join(kept).encode("utf-8")) <= MAX_PROJECT_BYTES
    for dropped in PROJECT_LINES[len(kept) :]:
        assert dropped.split(":")[0] not in system
    assert PROJECT_HEADER in system
    assert PROJECT_LINES[0] not in user
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert PROJECT_LINES[0] not in caplog.text


def test_process_runs_the_task_prompt_at_the_task_temperature(
    state_factory: StateFactory,
    settings_factory: Callable[..., Settings],
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    processor_factory: Callable[..., Processor],
    app_factory: AppFactory,
) -> None:
    settings = settings_factory(llm_temperature=0.42, llm_dictation_temperature=0.0)
    transcriber = transcriber_factory(ready=True)
    llm = llm_factory("Проверь membership.")
    state = state_factory(
        settings=settings,
        transcriber=transcriber,
        llm=llm,
        processor=processor_factory(transcriber, llm, settings=settings),
        llm_ready=True,
    )
    client = TestClient(app_factory(state))

    response = client.post("/v1/process", files=_upload())

    assert response.status_code == 200
    system, _user, temperature = llm.calls[0]
    assert system == "Ты редактор."
    assert temperature == pytest.approx(0.42)


def test_config_reports_the_live_llm_without_the_key(
    ready_state: ServerState, app_factory: AppFactory
) -> None:
    """The key is reported as set or not; echoing it would hand it to anything on loopback."""
    client = TestClient(app_factory(ready_state))

    body = client.get("/v1/config").json()

    assert body["model"] == "fake-llm"
    assert body["api_key_set"] is False
    assert body["overridden"] is False
    assert body["ready"] is True
    assert "api_key" not in body
    assert "@" not in body["base_url"]


def test_config_reconfigures_the_running_client(
    ready_state: ServerState, app_factory: AppFactory, tmp_path: Path
) -> None:
    ready_state.settings = make_settings(state_dir=tmp_path)
    llm = cast(Any, ready_state.llm)
    client = TestClient(app_factory(ready_state))

    body = client.put(
        "/v1/config",
        json={"base_url": "https://api.test/v1", "model": "gpt-x", "api_key": "sk-1"},
    ).json()

    assert llm.reconfigured == [("https://api.test/v1", "gpt-x", "sk-1")]
    assert body["model"] == "gpt-x"
    assert body["base_url"] == "https://api.test/v1"
    assert body["api_key_set"] is True
    assert body["overridden"] is True


def test_config_persists_so_a_restart_keeps_it(
    ready_state: ServerState, app_factory: AppFactory, tmp_path: Path
) -> None:
    ready_state.settings = make_settings(state_dir=tmp_path)
    client = TestClient(app_factory(ready_state))

    client.put("/v1/config", json={"model": "gpt-x"})

    assert runtime_config.load(ready_state.settings).model == "gpt-x"


def test_config_leaves_out_what_the_update_did_not_mention(
    ready_state: ServerState, app_factory: AppFactory, tmp_path: Path
) -> None:
    ready_state.settings = make_settings(state_dir=tmp_path)
    client = TestClient(app_factory(ready_state))

    client.put("/v1/config", json={"base_url": "https://api.test/v1", "model": "gpt-x"})
    body = client.put("/v1/config", json={"model": "gpt-y"}).json()

    assert body["base_url"] == "https://api.test/v1"
    assert body["model"] == "gpt-y"


def test_config_reprobes_and_reports_an_unreachable_endpoint(
    ready_state: ServerState, app_factory: AppFactory, tmp_path: Path
) -> None:
    """The point of probing on write: a wrong key is a dialog, not a timeout mid-dictation."""
    ready_state.settings = make_settings(state_dir=tmp_path)
    cast(Any, ready_state.llm).check_result = (False, "HTTP 401")
    client = TestClient(app_factory(ready_state))

    body = client.put("/v1/config", json={"api_key": "wrong"}).json()

    assert body["ready"] is False
    assert body["error"] == "HTTP 401"


@pytest.mark.parametrize("url", ["", "   ", "ollama:11434/v1", "ftp://host/v1"])
def test_config_rejects_a_base_url_that_is_not_http(
    ready_state: ServerState, app_factory: AppFactory, url: str
) -> None:
    client = TestClient(app_factory(ready_state))

    response = client.put("/v1/config", json={"base_url": url})

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_config_is_refused_while_the_server_is_still_starting(
    state_factory: StateFactory, app_factory: AppFactory
) -> None:
    client = TestClient(app_factory(state_factory(warming=True)))

    response = client.put("/v1/config", json={"model": "gpt-x"})

    assert response.status_code == 503
    assert response.json()["error"] == "warming"
