from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

import ctranslate2

from . import __version__
from .config import Settings, redacted_base_url
from .glossary import Glossary
from .llm import OpenAICompatibleClient
from .models import GpuHealth, HealthResponse, LlmHealth, SttHealth
from .processor import Processor
from .prompt import Prompt
from .transcription import Transcriber

logger = logging.getLogger(__name__)


@dataclass
class ServerState:
    """The live objects built during startup, shared by every request.

    ``started_at`` is a ``time.monotonic()`` reading taken when the lifespan began, so
    uptime is immune to wall-clock changes. Fields other than ``settings`` and
    ``started_at`` stay ``None``/``False`` until the corresponding startup step finishes,
    and ``warming`` flips to False once startup is done, successfully or not.
    """

    settings: Settings
    started_at: float
    transcriber: Transcriber | None = None
    llm: OpenAICompatibleClient | None = None
    prompt: Prompt | None = None
    dictation_prompt: Prompt | None = None
    glossary: Glossary | None = None
    processor: Processor | None = None
    stt_error: str | None = None
    llm_ready: bool = False
    llm_error: str | None = None
    warming: bool = True


async def refresh_llm(state: ServerState, timeout_seconds: float | None = None) -> None:
    """Probe the LLM endpoint and update ``llm_ready``/``llm_error`` on ``state``.

    Never raises and never records the API key: ``OpenAICompatibleClient.check`` reports
    failures as a short reason string.
    """
    llm = state.llm
    if llm is None:
        state.llm_ready = False
        state.llm_error = "llm client not initialised"
        return
    ready, reason = await llm.check(timeout_seconds)
    state.llm_ready = ready
    state.llm_error = None if ready else (reason or "llm check failed")


HEALTH_PROBE_TIMEOUT_S = 2.0


async def build_health(state: ServerState) -> HealthResponse:
    """Return a fresh health snapshot, re-probing the LLM endpoint on every call.

    ``status`` is "warming" until startup has finished publishing the processor, then
    "ready" when STT is loaded and the LLM answers, and "degraded" otherwise. The reported
    LLM base URL has any credentials stripped; the API key is never included.
    """
    # A short budget on purpose: /health is polled by the Docker healthcheck and by
    # start.ps1, and a firewalled endpoint that black-holes packets would otherwise stall
    # every health response for the full LLM timeout.
    await refresh_llm(state, HEALTH_PROBE_TIMEOUT_S)
    settings = state.settings
    transcriber = state.transcriber
    stt_ready = transcriber is not None and transcriber.ready

    status: Literal["ready", "warming", "degraded"]
    if state.warming:
        # Startup publishes the processor last, so "ready" must wait for the whole sequence:
        # reporting it earlier makes start.ps1 launch the companion into 503 "warming".
        status = "warming"
    elif stt_ready and state.llm_ready:
        status = "ready"
    else:
        status = "degraded"

    stt = SttHealth(
        ready=stt_ready,
        model=transcriber.model_name if transcriber is not None else settings.stt_model,
        device=transcriber.device if transcriber is not None else settings.stt_device,
        compute_type=(
            transcriber.compute_type if transcriber is not None else settings.stt_compute_type
        ),
        error=(transcriber.error if transcriber is not None else None) or state.stt_error,
    )
    llm = state.llm
    llm_health = LlmHealth(
        ready=state.llm_ready,
        model=llm.model if llm is not None else settings.llm_model,
        base_url=redacted_base_url(llm.base_url if llm is not None else settings.llm_base_url),
        error=state.llm_error,
    )
    return HealthResponse(
        status=status,
        version=__version__,
        uptime_s=uptime_seconds(state.started_at),
        stt=stt,
        llm=llm_health,
        gpu=gpu_health(),
    )


def gpu_health() -> GpuHealth:
    """Report visible CUDA devices, degrading to zero when CTranslate2 cannot be queried."""
    try:
        count = int(ctranslate2.get_cuda_device_count())
    except Exception as exc:
        logger.debug("cuda device count unavailable: %s", exc)
        return GpuHealth(cuda_available=False, device_count=0)
    return GpuHealth(cuda_available=count > 0, device_count=count)


def uptime_seconds(started_at: float) -> float:
    """Seconds since ``started_at``, a ``time.monotonic()`` reading, rounded to 3 decimals."""
    return round(max(0.0, time.monotonic() - started_at), 3)
