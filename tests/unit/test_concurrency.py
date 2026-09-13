from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from vox_server.config import Settings
from vox_server.models import TranscriptionResult
from vox_server.modes import Mode, ModeRegistry
from vox_server.processor import Processor

AUDIO = b"audio-payload"
STT_DELAY_S = 0.05


class OverlapRecordingTranscriber:
    """Records the highest number of transcriptions that ever ran at the same time."""

    def __init__(self, delay_s: float = STT_DELAY_S) -> None:
        self._delay_s = delay_s
        self._lock = threading.Lock()
        self._active = 0
        self.max_overlap = 0
        self.total_calls = 0

    def transcribe(self, data: bytes, language: str | None = None) -> TranscriptionResult:
        del data, language
        with self._lock:
            self._active += 1
            self.total_calls += 1
            self.max_overlap = max(self.max_overlap, self._active)
        time.sleep(self._delay_s)
        with self._lock:
            self._active -= 1
        return TranscriptionResult(text="расшифровка", language="ru", audio_duration_s=1.0)


async def _run_three(processor: Processor) -> list[str]:
    responses = await asyncio.gather(
        *(processor.process(AUDIO, "plain", request_id=f"req-{index}") for index in range(3))
    )
    return [response.output for response in responses]


@pytest.mark.parametrize("concurrency", [1, 2])
async def test_the_semaphore_caps_simultaneous_transcriptions(
    concurrency: int,
    mode_factory: Callable[..., Mode],
    registry_factory: Callable[..., ModeRegistry],
    processor_factory: Callable[..., Processor],
    llm_factory: Callable[..., Any],
    settings_factory: Callable[..., Settings],
) -> None:
    transcriber = OverlapRecordingTranscriber()
    processor = processor_factory(
        transcriber,
        llm_factory(),
        registry_factory(mode_factory("plain", requires_llm=False)),
        settings=settings_factory(processing_concurrency=concurrency),
    )

    outputs = await _run_three(processor)

    assert outputs == ["расшифровка"] * 3
    assert transcriber.total_calls == 3
    assert transcriber.max_overlap == concurrency


async def test_requests_queue_instead_of_failing_when_the_gate_is_busy(
    mode_factory: Callable[..., Mode],
    registry_factory: Callable[..., ModeRegistry],
    processor_factory: Callable[..., Processor],
    llm_factory: Callable[..., Any],
    settings_factory: Callable[..., Settings],
) -> None:
    transcriber = OverlapRecordingTranscriber()
    processor = processor_factory(
        transcriber,
        llm_factory(),
        registry_factory(mode_factory("plain", requires_llm=False)),
        settings=settings_factory(processing_concurrency=1),
    )

    started = time.perf_counter()
    await _run_three(processor)
    elapsed = time.perf_counter() - started

    assert elapsed >= 3 * STT_DELAY_S * 0.8
    assert processor.semaphore.locked() is False
