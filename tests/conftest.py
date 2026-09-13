"""Shared fixtures and builders for the vox test suite.

Everything here is offline: no microphone, GPU, network or Docker is ever touched. The
fakes duck-type the real collaborators and are cast at the construction boundary, so the
production signatures stay authoritative.
"""

from __future__ import annotations

import asyncio
import io
import math
import struct
import time
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI

from vox_server.api import register_exception_handlers, register_routes
from vox_server.config import Settings, get_settings
from vox_server.glossary import Glossary
from vox_server.health import ServerState
from vox_server.llm import OpenAICompatibleClient
from vox_server.models import TranscriptionResult
from vox_server.modes import Mode, ModeRegistry
from vox_server.processor import Processor
from vox_server.trace import TraceWriter
from vox_server.transcription import Transcriber

REPO_ROOT = Path(__file__).resolve().parents[1]

_SERVER_ENV_VARS = (
    "VOICE_CODE_HOST",
    "VOICE_CODE_PORT",
    "STT_MODEL",
    "STT_LANGUAGE",
    "STT_DEVICE",
    "STT_COMPUTE_TYPE",
    "STT_BEAM_SIZE",
    "STT_VAD_FILTER",
    "STT_GLOSSARY_HOTWORDS",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_TIMEOUT_SECONDS",
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
    "PROCESSING_CONCURRENCY",
    "LOG_LEVEL",
    "LOG_TEXT",
    "MODES_DIR",
    "GLOSSARY_PATH",
    "MAX_AUDIO_BYTES",
    "MAX_AUDIO_SECONDS",
    "MAX_PROJECT_BYTES",
    "VOX_TRACE_DIR",
    "VOX_TRACE_KEEP",
)


class FakeTranscriber:
    """Returns canned transcripts with no speech model behind it.

    ``calls`` records ``(audio_bytes, language)`` for every transcribe call, so tests can
    assert both that the audio arrived and that nothing touched the GPU path early.
    """

    def __init__(
        self,
        text: str = "проверь этот сервис",
        *,
        language: str = "ru",
        audio_duration_s: float = 1.5,
        delay_s: float = 0.0,
        ready: bool = True,
        error: str | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.text = text
        self.language = language
        self.audio_duration_s = audio_duration_s
        self.delay_s = delay_s
        self.ready = ready
        self.error = error
        self.raises = raises
        self.device = "cpu"
        self.compute_type = "int8"
        self.model_name = "fake-whisper"
        self.calls: list[tuple[bytes, str | None]] = []

    def transcribe(self, data: bytes, language: str | None = None) -> TranscriptionResult:
        self.calls.append((data, language))
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.raises is not None:
            raise self.raises
        return TranscriptionResult(
            text=self.text,
            language=language or self.language,
            language_probability=0.99,
            audio_duration_s=self.audio_duration_s,
            duration_ms=int(self.delay_s * 1000),
        )

    def close(self) -> None:
        self.ready = False


class FakeLlm:
    """Records every chat call and replays a canned reply.

    ``calls`` holds ``(system, user, temperature)`` tuples in call order.
    """

    def __init__(
        self,
        reply: str = "Нормализованный текст.",
        *,
        model: str = "fake-llm",
        base_url: str = "http://llm.test/v1",
        delay_s: float = 0.0,
        raises: Exception | None = None,
        check_result: tuple[bool, str | None] = (True, None),
    ) -> None:
        self.reply = reply
        self.model = model
        self.base_url = base_url
        self.delay_s = delay_s
        self.raises = raises
        self.check_result = check_result
        self.check_timeouts: list[float | None] = []
        self.closed = False
        self.calls: list[tuple[str, str, float | None]] = []

    async def chat(self, system: str, user: str, *, temperature: float | None = None) -> str:
        self.calls.append((system, user, temperature))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.raises is not None:
            raise self.raises
        return self.reply

    async def check(self, timeout_seconds: float | None = None) -> tuple[bool, str | None]:
        self.check_timeouts.append(timeout_seconds)
        return self.check_result

    async def aclose(self) -> None:
        self.closed = True


def make_settings(**overrides: Any) -> Settings:
    """Build Settings from explicit values only, ignoring any .env file on disk."""
    return Settings(_env_file=None, **overrides)


def make_wav_bytes(
    seconds: float = 0.5,
    sample_rate: int = 16000,
    *,
    frequency: float = 440.0,
    amplitude: float = 0.3,
    channels: int = 1,
) -> bytes:
    """Build a 16-bit PCM RIFF/WAVE sine tone entirely in memory."""
    frames = max(0, int(seconds * sample_rate))
    values: list[int] = []
    for index in range(frames):
        sample = int(amplitude * 32767 * math.sin(2.0 * math.pi * frequency * index / sample_rate))
        values.extend([sample] * channels)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(struct.pack(f"<{len(values)}h", *values))
    return buffer.getvalue()


def make_mode(
    name: str = "plain",
    *,
    label: str | None = None,
    description: str = "",
    requires_llm: bool = False,
    wrap_for_claude: bool = False,
    temperature: float | None = None,
    fallback_to_transcript: bool = False,
    system_prompt: str = "Ты редактор.",
    user_template: str = "{transcript}",
    wrapper_template: str = "",
) -> Mode:
    """Build a Mode directly, bypassing the markdown parser."""
    return Mode(
        name=name,
        label=label if label is not None else name.title(),
        description=description,
        requires_llm=requires_llm,
        wrap_for_claude=wrap_for_claude,
        temperature=temperature,
        fallback_to_transcript=fallback_to_transcript,
        system_prompt=system_prompt,
        user_template=user_template,
        wrapper_template=wrapper_template,
    )


def make_registry(*modes: Mode) -> ModeRegistry:
    """Build a ModeRegistry from in-memory modes; with no arguments it holds one plain mode."""
    return ModeRegistry(modes if modes else (make_mode(),))


def make_processor(
    transcriber: Any,
    llm: Any,
    modes: ModeRegistry,
    *,
    glossary: Glossary | None = None,
    settings: Settings | None = None,
    trace_writer: TraceWriter | None = None,
) -> Processor:
    """Wire a Processor around fake collaborators; without a writer nothing is traced."""
    return Processor(
        cast(Transcriber, transcriber),
        cast(OpenAICompatibleClient, llm),
        modes,
        glossary if glossary is not None else Glossary(),
        settings if settings is not None else make_settings(),
        trace_writer,
    )


def make_state(
    *,
    settings: Settings | None = None,
    transcriber: Any = None,
    llm: Any = None,
    modes: ModeRegistry | None = None,
    glossary: Glossary | None = None,
    processor: Any = None,
    stt_error: str | None = None,
    llm_ready: bool = False,
    llm_error: str | None = None,
    warming: bool = False,
) -> ServerState:
    """Build a ServerState holding fakes, as the lifespan would have built it."""
    return ServerState(
        settings=settings if settings is not None else make_settings(),
        started_at=time.monotonic(),
        transcriber=cast("Transcriber | None", transcriber),
        llm=cast("OpenAICompatibleClient | None", llm),
        modes=modes,
        glossary=glossary,
        processor=cast("Processor | None", processor),
        stt_error=stt_error,
        llm_ready=llm_ready,
        llm_error=llm_error,
        warming=warming,
    )


def build_test_app(state: ServerState | None = None) -> FastAPI:
    """Build the API without the real lifespan, with ``state`` injected directly."""
    app = FastAPI()
    register_routes(app)
    register_exception_handlers(app)
    if state is not None:
        app.state.server_state = state
    return app


@pytest.fixture(autouse=True)
def _pristine_settings_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in _SERVER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    return make_settings


@pytest.fixture
def wav_factory() -> Callable[..., bytes]:
    return make_wav_bytes


@pytest.fixture
def mode_factory() -> Callable[..., Mode]:
    return make_mode


@pytest.fixture
def registry_factory() -> Callable[..., ModeRegistry]:
    return make_registry


@pytest.fixture
def transcriber_factory() -> Callable[..., FakeTranscriber]:
    return FakeTranscriber


@pytest.fixture
def llm_factory() -> Callable[..., FakeLlm]:
    return FakeLlm


@pytest.fixture
def processor_factory() -> Callable[..., Processor]:
    return make_processor


@pytest.fixture
def state_factory() -> Callable[..., ServerState]:
    return make_state


@pytest.fixture
def app_factory() -> Callable[..., FastAPI]:
    return build_test_app
