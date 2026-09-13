from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from vox_server.config import Settings
from vox_server.glossary import Glossary
from vox_server.llm import LlmTimeoutError
from vox_server.modes import Mode, ModeRegistry, UnknownModeError
from vox_server.processor import (
    EmptyOutputError,
    EmptyTranscriptError,
    Processor,
)

AUDIO = b"RIFF-not-really-decoded-by-the-fake"

ModeFactory = Callable[..., Mode]
RegistryFactory = Callable[..., ModeRegistry]
ProcessorFactory = Callable[..., Processor]


async def test_a_mode_without_an_llm_never_calls_the_model(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    transcriber = transcriber_factory("сырой текст")
    llm = llm_factory("НЕ ДОЛЖНО ПОЯВИТЬСЯ")
    modes = registry_factory(mode_factory("dictation", requires_llm=False))
    processor = processor_factory(transcriber, llm, modes)

    response = await processor.process(AUDIO, "dictation", request_id="req-1")

    assert llm.calls == []
    assert response.transcript == "сырой текст"
    assert response.normalized_text == "сырой текст"
    assert response.output == "сырой текст"
    assert response.mode == "dictation"
    assert response.request_id == "req-1"
    assert response.language == "ru"
    assert response.timings_ms.llm == 0


async def test_an_llm_mode_receives_the_rendered_prompt(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    transcriber = transcriber_factory("посмотри мембершип")
    llm = llm_factory("Проверь membership.")
    mode = mode_factory(
        "clean",
        requires_llm=True,
        temperature=0.35,
        system_prompt="Ты редактор инженерных запросов.",
        user_template="Словарь:\n{glossary}\n\nРасшифровка:\n{transcript}",
    )
    processor = processor_factory(
        transcriber,
        llm,
        registry_factory(mode),
        glossary=Glossary.from_mapping({"мембершип": "membership"}),
    )

    response = await processor.process(AUDIO, "clean", request_id="req-2")

    assert len(llm.calls) == 1
    system, user, temperature = llm.calls[0]
    assert system == "Ты редактор инженерных запросов."
    assert "мембершип -> membership" in user
    assert "посмотри мембершип" in user
    assert "{transcript}" not in user
    assert "{glossary}" not in user
    assert temperature == pytest.approx(0.35)
    assert response.normalized_text == "Проверь membership."
    assert response.output == "Проверь membership."


async def test_the_llm_reply_is_cleaned_before_it_is_used(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    llm = llm_factory("<think>подумаю</think>\n```\nГотовый текст\n```")
    processor = processor_factory(
        transcriber_factory(),
        llm,
        registry_factory(mode_factory("clean", requires_llm=True)),
    )

    response = await processor.process(AUDIO, "clean", request_id="req-3")

    assert response.normalized_text == "Готовый текст"


async def test_wrap_for_claude_wraps_the_normalized_text(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    mode = mode_factory(
        "context",
        requires_llm=True,
        wrap_for_claude=True,
        wrapper_template="VOICE TASK\n---\n{normalized}\n---",
    )
    processor = processor_factory(
        transcriber_factory(), llm_factory("Проверь membership."), registry_factory(mode)
    )

    response = await processor.process(AUDIO, "context", request_id="req-4")

    assert response.normalized_text == "Проверь membership."
    assert response.output == "VOICE TASK\n---\nПроверь membership.\n---"


async def test_an_empty_transcript_is_rejected(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    llm = llm_factory()
    processor = processor_factory(
        transcriber_factory("   \n  "),
        llm,
        registry_factory(mode_factory("clean", requires_llm=True)),
    )

    with pytest.raises(EmptyTranscriptError):
        await processor.process(AUDIO, "clean", request_id="req-5")

    assert llm.calls == []


async def test_empty_llm_output_falls_back_to_the_transcript_when_allowed(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    mode = mode_factory("clean", requires_llm=True, fallback_to_transcript=True)
    processor = processor_factory(
        transcriber_factory("исходная расшифровка"), llm_factory("   "), registry_factory(mode)
    )

    response = await processor.process(AUDIO, "clean", request_id="req-6")

    assert response.normalized_text == "исходная расшифровка"
    assert response.output == "исходная расшифровка"


async def test_empty_llm_output_without_a_fallback_is_an_error(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    mode = mode_factory("task", requires_llm=True, fallback_to_transcript=False)
    processor = processor_factory(transcriber_factory(), llm_factory(""), registry_factory(mode))

    with pytest.raises(EmptyOutputError):
        await processor.process(AUDIO, "task", request_id="req-7")


async def test_a_wrapping_mode_never_emits_a_wrapper_around_nothing(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    mode = mode_factory(
        "context",
        requires_llm=True,
        wrap_for_claude=True,
        fallback_to_transcript=False,
        wrapper_template="VOICE TASK\n---\n{normalized}\n---",
    )
    processor = processor_factory(
        transcriber_factory(), llm_factory("\n\n"), registry_factory(mode)
    )

    with pytest.raises(EmptyOutputError) as excinfo:
        await processor.process(AUDIO, "context", request_id="req-8")

    assert "VOICE TASK" not in str(excinfo.value)


async def test_an_unknown_mode_fails_before_the_transcriber_runs(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    transcriber = transcriber_factory()
    llm = llm_factory()
    processor = processor_factory(
        transcriber, llm, registry_factory(mode_factory("clean", requires_llm=True))
    )

    with pytest.raises(UnknownModeError) as excinfo:
        await processor.process(AUDIO, "nonsense", request_id="req-9")

    assert transcriber.calls == []
    assert llm.calls == []
    assert excinfo.value.available == ["clean"]


async def test_a_language_override_reaches_the_transcriber(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    transcriber = transcriber_factory()
    processor = processor_factory(
        transcriber, llm_factory(), registry_factory(mode_factory("dictation"))
    )

    response = await processor.process(AUDIO, "dictation", request_id="req-10", language="en")

    assert transcriber.calls == [(AUDIO, "en")]
    assert response.language == "en"


async def test_timings_are_populated_and_consistent(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    mode = mode_factory("clean", requires_llm=True)
    processor = processor_factory(
        transcriber_factory(delay_s=0.03), llm_factory(delay_s=0.03), registry_factory(mode)
    )

    response = await processor.process(AUDIO, "clean", request_id="req-11")

    timings = response.timings_ms
    assert timings.transcription >= 25
    assert timings.llm >= 25
    assert timings.total >= timings.transcription
    assert timings.total >= timings.llm


async def test_transcribe_only_skips_the_llm(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    transcriber = transcriber_factory("  сырая расшифровка  ")
    llm = llm_factory()
    processor = processor_factory(
        transcriber, llm, registry_factory(mode_factory("clean", requires_llm=True))
    )

    response = await processor.transcribe_only(AUDIO, request_id="req-12", language="ru")

    assert response.transcript == "сырая расшифровка"
    assert response.language == "ru"
    assert response.timings_ms.llm == 0
    assert llm.calls == []


async def test_transcribe_only_rejects_an_empty_transcript(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    processor = processor_factory(
        transcriber_factory(""), llm_factory(), registry_factory(mode_factory("clean"))
    )

    with pytest.raises(EmptyTranscriptError):
        await processor.transcribe_only(AUDIO, request_id="req-13")


async def test_transform_only_skips_speech_recognition(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    transcriber = transcriber_factory()
    llm = llm_factory("Проверь membership.")
    mode = mode_factory(
        "context",
        requires_llm=True,
        wrap_for_claude=True,
        wrapper_template="<<{normalized}>>",
    )
    processor = processor_factory(transcriber, llm, registry_factory(mode))

    response = await processor.transform_only(
        "  посмотри мембершип  ", "context", request_id="req-14"
    )

    assert transcriber.calls == []
    assert llm.calls[0][1] == "посмотри мембершип"
    assert response.normalized_text == "Проверь membership."
    assert response.output == "<<Проверь membership.>>"
    assert response.timings_ms.transcription == 0


async def test_transform_only_rejects_blank_text(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    processor = processor_factory(
        transcriber_factory(), llm_factory(), registry_factory(mode_factory("clean"))
    )

    with pytest.raises(EmptyTranscriptError):
        await processor.transform_only("   ", "clean", request_id="req-15")


async def test_an_llm_failure_propagates_unchanged(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    processor = processor_factory(
        transcriber_factory(),
        llm_factory(raises=LlmTimeoutError("too slow")),
        registry_factory(mode_factory("clean", requires_llm=True)),
    )

    with pytest.raises(LlmTimeoutError):
        await processor.process(AUDIO, "clean", request_id="req-16")


async def test_the_semaphore_reflects_the_configured_concurrency(
    mode_factory: ModeFactory,
    registry_factory: RegistryFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    settings_factory: Callable[..., Settings],
) -> None:
    processor = processor_factory(
        transcriber_factory(),
        llm_factory(),
        registry_factory(mode_factory()),
        settings=settings_factory(processing_concurrency=0),
    )

    assert isinstance(processor.semaphore, asyncio.Semaphore)
    await processor.semaphore.acquire()
    assert processor.semaphore.locked()
    processor.semaphore.release()
