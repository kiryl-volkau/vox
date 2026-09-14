from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from vox_server.config import Settings
from vox_server.glossary import Glossary
from vox_server.llm import LlmTimeoutError
from vox_server.processor import EmptyTranscriptError, Processor
from vox_server.prompt import Prompt

AUDIO = b"RIFF-not-really-decoded-by-the-fake"
PROJECT_MARKER = "ПРОЕКТ-МАРКЕР-7"
PROJECT = f"{PROJECT_MARKER}: модуль billing не трогаем.\nДжигвард -> Jigward."
PROJECT_HEADER = "<project_context>"
SYSTEM_WITH_PROJECT = "Ты редактор инженерных запросов.\n\n{project}"

PromptFactory = Callable[..., Prompt]
ProcessorFactory = Callable[..., Processor]


async def test_the_llm_receives_the_rendered_prompt(
    prompt_factory: PromptFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    transcriber = transcriber_factory("посмотри мембершип")
    llm = llm_factory("Проверь membership.")
    prompt = prompt_factory(
        system_prompt="Ты редактор инженерных запросов.",
        user_template="Словарь:\n{glossary}\n\nРасшифровка:\n{transcript}",
    )
    processor = processor_factory(
        transcriber,
        llm,
        prompt,
        glossary=Glossary.from_mapping({"мембершип": "membership"}),
    )

    response = await processor.process(AUDIO, request_id="req-2")

    assert len(llm.calls) == 1
    system, user, temperature = llm.calls[0]
    assert system == "Ты редактор инженерных запросов."
    assert "мембершип -> membership" in user
    assert "посмотри мембершип" in user
    assert "{transcript}" not in user
    assert "{glossary}" not in user
    assert temperature == pytest.approx(0.1)
    assert response.output == "Проверь membership."


async def test_the_llm_reply_is_cleaned_before_it_is_used(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    llm = llm_factory("<think>подумаю</think>\n```\nГотовый текст\n```")
    processor = processor_factory(transcriber_factory(), llm)

    response = await processor.process(AUDIO, request_id="req-3")

    assert response.output == "Готовый текст"


async def test_an_empty_transcript_is_rejected(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    llm = llm_factory()
    processor = processor_factory(transcriber_factory("   \n  "), llm)

    with pytest.raises(EmptyTranscriptError):
        await processor.process(AUDIO, request_id="req-5")

    assert llm.calls == []


@pytest.mark.parametrize("reply", ["", "   "])
async def test_an_empty_llm_reply_falls_back_to_the_transcript(
    reply: str,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    processor = processor_factory(transcriber_factory("посмотри мембершип"), llm_factory(reply))

    response = await processor.process(AUDIO, request_id="req-7")

    assert response.output == "посмотри мембершип"


async def test_an_empty_llm_reply_logs_a_warning(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    processor = processor_factory(transcriber_factory(), llm_factory(""))

    with caplog.at_level(logging.WARNING):
        await processor.process(AUDIO, request_id="req-8")

    assert any(
        record.levelno == logging.WARNING
        and "falling back to the transcript" in record.getMessage()
        for record in caplog.records
    )


async def test_the_output_language_never_reaches_the_transcriber(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    """What was spoken and what the answer is written in are independent.

    Forcing Whisper into the output language would make it mis-transcribe - or silently
    translate - speech in any other one.
    """
    transcriber = transcriber_factory()
    processor = processor_factory(transcriber, llm_factory())

    await processor.process(AUDIO, request_id="req-10", language="en")

    assert transcriber.calls == [(AUDIO, None)]


async def test_a_language_override_reaches_the_transcriber_when_transcribing_only(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    """/v1/transcribe returns speech as recognised, so there its language is the spoken one."""
    transcriber = transcriber_factory()
    processor = processor_factory(transcriber, llm_factory())

    response = await processor.transcribe_only(AUDIO, request_id="req-10", language="en")

    assert transcriber.calls == [(AUDIO, "en")]
    assert response.language == "en"


async def test_the_output_language_picks_the_prompt_language_block(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    prompt_factory: Callable[..., Any],
) -> None:
    llm = llm_factory()
    prompt = prompt_factory(system_prompt="Ты редактор.\n{language}")
    prompt = replace(
        prompt,
        language_blocks=(("en", "Answer in English."), ("ru", "Отвечай по-русски.")),
    )
    processor = processor_factory(transcriber_factory(), llm, prompt)

    await processor.process(AUDIO, request_id="req-11", language="ru")

    system, _user, _temperature = llm.calls[0]
    assert "Отвечай по-русски." in system
    assert "Answer in English." not in system


async def test_the_conversation_context_reaches_the_system_prompt_only(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    prompt_factory: PromptFactory,
) -> None:
    llm = llm_factory()
    prompt = prompt_factory(system_prompt="Ты редактор.\n{context}")
    processor = processor_factory(transcriber_factory("посмотри это"), llm, prompt)

    await processor.process(AUDIO, request_id="req-ctx", context="user: почини SecurityConfig")

    system, user, _temperature = llm.calls[0]
    assert "<conversation>" in system
    assert "SecurityConfig" in system
    assert "SecurityConfig" not in user


async def test_an_oversized_conversation_context_is_truncated_not_rejected(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    prompt_factory: PromptFactory,
    settings_factory: Callable[..., Settings],
    caplog: pytest.LogCaptureFixture,
) -> None:
    lines = tuple(f"СТРОКА-{index:03d}: что-то было сказано" for index in range(64))
    llm = llm_factory()
    prompt = prompt_factory(system_prompt="Ты редактор.\n{context}")
    processor = processor_factory(
        transcriber_factory(), llm, prompt, settings=settings_factory(max_context_bytes=256)
    )

    with caplog.at_level(logging.WARNING):
        await processor.process(AUDIO, request_id="req-big", context="\n".join(lines))

    system, _user, _temperature = llm.calls[0]
    kept = [line for line in lines if line in system]
    assert kept, "the whole conversation was dropped instead of truncated"
    assert kept == list(lines[: len(kept)])
    assert len("\n".join(kept).encode("utf-8")) <= 256
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert lines[0] not in caplog.text


async def test_a_vox_md_system_section_becomes_its_own_instruction_block(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    prompt_factory: PromptFactory,
) -> None:
    llm = llm_factory()
    prompt = prompt_factory(system_prompt="Ты редактор.\n{instructions}\n{project}")
    processor = processor_factory(transcriber_factory(), llm, prompt)

    await processor.process(
        AUDIO,
        request_id="req-vox",
        project="Термины: джигвард -> Jigward.\n\n## SYSTEM\nОтвечай одним предложением.",
    )

    system, _user, _temperature = llm.calls[0]
    instructions = system.index("<project_instructions>")
    context = system.index("<project_context>")
    assert instructions < context
    assert "Отвечай одним предложением." in system
    assert "Отвечай одним предложением." not in system[context:]
    assert "Jigward" in system[context:]


async def test_timings_are_populated_and_consistent(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    processor = processor_factory(transcriber_factory(delay_s=0.03), llm_factory(delay_s=0.03))

    response = await processor.process(AUDIO, request_id="req-11")

    timings = response.timings_ms
    assert timings.transcription >= 25
    assert timings.llm >= 25
    assert timings.total >= timings.transcription
    assert timings.total >= timings.llm


async def test_transcribe_only_skips_the_llm(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    transcriber = transcriber_factory("  сырая расшифровка  ")
    llm = llm_factory()
    processor = processor_factory(transcriber, llm)

    response = await processor.transcribe_only(AUDIO, request_id="req-12", language="ru")

    assert response.transcript == "сырая расшифровка"
    assert response.language == "ru"
    assert response.timings_ms.llm == 0
    assert llm.calls == []


async def test_transcribe_only_rejects_an_empty_transcript(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    processor = processor_factory(transcriber_factory(""), llm_factory())

    with pytest.raises(EmptyTranscriptError):
        await processor.transcribe_only(AUDIO, request_id="req-13")


async def test_transform_only_skips_speech_recognition(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    transcriber = transcriber_factory()
    llm = llm_factory("Проверь membership.")
    processor = processor_factory(transcriber, llm)

    response = await processor.transform_only("  посмотри мембершип  ", request_id="req-14")

    assert transcriber.calls == []
    assert llm.calls[0][1] == "посмотри мембершип"
    assert response.output == "Проверь membership."
    assert response.timings_ms.transcription == 0


async def test_transform_only_falls_back_to_the_source_text_on_an_empty_reply(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    processor = processor_factory(transcriber_factory(), llm_factory("   "))

    response = await processor.transform_only("посмотри мембершип", request_id="req-14b")

    assert response.output == "посмотри мембершип"


async def test_transform_only_rejects_blank_text(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    processor = processor_factory(transcriber_factory(), llm_factory())

    with pytest.raises(EmptyTranscriptError):
        await processor.transform_only("   ", request_id="req-15")


async def test_an_llm_failure_propagates_unchanged(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    processor = processor_factory(
        transcriber_factory(), llm_factory(raises=LlmTimeoutError("too slow"))
    )

    with pytest.raises(LlmTimeoutError):
        await processor.process(AUDIO, request_id="req-16")


async def test_the_semaphore_reflects_the_configured_concurrency(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    settings_factory: Callable[..., Settings],
) -> None:
    processor = processor_factory(
        transcriber_factory(),
        llm_factory(),
        settings=settings_factory(processing_concurrency=0),
    )

    assert isinstance(processor.semaphore, asyncio.Semaphore)
    await processor.semaphore.acquire()
    assert processor.semaphore.locked()
    processor.semaphore.release()


async def test_the_project_reaches_the_system_prompt_and_never_the_user_message(
    prompt_factory: PromptFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    """Project context belongs in the system prompt; the transcript stays last in the user one.

    Ollama caches the prompt prefix, so a system prompt that is stable for a given project is
    re-used across requests (measured: 87 ms on the first call, 24-27 ms afterwards, even with
    a different user message). The transcript changes every request, so project text placed
    after it would push the divergence into the cached region and force a full prefill every
    single time.
    """
    llm = llm_factory("Проверь membership.")
    prompt = prompt_factory(
        system_prompt=SYSTEM_WITH_PROJECT,
        user_template="Словарь:\n{glossary}\n\nРасшифровка:\n{transcript}",
    )
    processor = processor_factory(transcriber_factory("посмотри мембершип"), llm, prompt)

    await processor.process(AUDIO, request_id="req-17", project=PROJECT)

    system, user, _temperature = llm.calls[0]
    assert PROJECT in system
    assert PROJECT_HEADER in system
    assert "{project}" not in system
    assert PROJECT not in user
    assert PROJECT_MARKER not in user
    assert user.rstrip().endswith("посмотри мембершип")


async def test_without_a_project_the_system_prompt_stays_bare(
    prompt_factory: PromptFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    llm = llm_factory("Проверь membership.")
    prompt = prompt_factory(system_prompt=SYSTEM_WITH_PROJECT)
    processor = processor_factory(transcriber_factory(), llm, prompt)

    await processor.process(AUDIO, request_id="req-18")

    system, _user, _temperature = llm.calls[0]
    assert system.strip() == "Ты редактор инженерных запросов."
    assert PROJECT_HEADER not in system
    assert "{project}" not in system


async def test_transform_only_also_puts_the_project_in_the_system_prompt(
    prompt_factory: PromptFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
) -> None:
    llm = llm_factory("Проверь membership.")
    prompt = prompt_factory(system_prompt=SYSTEM_WITH_PROJECT)
    processor = processor_factory(transcriber_factory(), llm, prompt)

    response = await processor.transform_only(
        "посмотри мембершип", request_id="req-20", project=PROJECT
    )

    system, user, _temperature = llm.calls[0]
    assert PROJECT in system
    assert PROJECT not in user
    assert user == "посмотри мембершип"
    assert response.output == "Проверь membership."


async def test_the_project_text_never_reaches_an_info_log(
    prompt_factory: PromptFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    settings_factory: Callable[..., Settings],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """log_text unlocks DEBUG only: INFO lines stay safe to ship to a log aggregator."""
    prompt = prompt_factory(system_prompt=SYSTEM_WITH_PROJECT)
    processor = processor_factory(
        transcriber_factory(),
        llm_factory("Проверь membership."),
        prompt,
        settings=settings_factory(log_text=True),
    )

    with caplog.at_level(logging.INFO):
        await processor.process(AUDIO, request_id="req-21", project=PROJECT)

    assert any(record.levelno == logging.INFO for record in caplog.records)
    assert PROJECT_MARKER not in caplog.text
    assert PROJECT not in caplog.text


async def test_the_info_line_reports_provenance_but_still_no_text(
    prompt_factory: PromptFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One line per request, carrying which project and how big a prompt, never the text."""
    transcript = "посмотри мембершип"
    prompt = prompt_factory(system_prompt=SYSTEM_WITH_PROJECT)
    processor = processor_factory(
        transcriber_factory(transcript), llm_factory("Проверь membership."), prompt
    )

    with caplog.at_level(logging.INFO):
        await processor.process(AUDIO, request_id="req-22", project=PROJECT, project_name="jigward")

    line = next(record.getMessage() for record in caplog.records if record.levelno == logging.INFO)
    assert "request_id=req-22" in line
    assert "project=jigward" in line
    assert f"project_bytes={len(PROJECT.encode())}" in line
    assert "system_chars=" in line
    assert "user_chars=" in line
    assert transcript not in line
    assert PROJECT_MARKER not in line


async def test_the_info_line_says_a_dash_when_nothing_was_traced(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    processor = processor_factory(transcriber_factory(), llm_factory())

    with caplog.at_level(logging.INFO):
        await processor.process(AUDIO, request_id="req-23")

    line = next(record.getMessage() for record in caplog.records if record.levelno == logging.INFO)
    assert "project=-" in line
    assert "project_bytes=0" in line
    assert "trace=-" in line


async def test_dictation_uses_the_dictation_prompt_and_temperature(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    settings_factory: Callable[..., Settings],
) -> None:
    llm = llm_factory("Расшифровка, готово.")
    processor = processor_factory(
        transcriber_factory("расшифровка"),
        llm,
        settings=settings_factory(llm_temperature=0.4, llm_dictation_temperature=0.0),
    )

    response = await processor.process(AUDIO, request_id="req-dict", dictation=True)

    system, _user, temperature = llm.calls[0]
    assert system == "Ты диктофон."
    assert temperature == pytest.approx(0.0)
    assert response.output == "Расшифровка, готово."


async def test_without_dictation_the_task_prompt_and_temperature_are_used(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    settings_factory: Callable[..., Settings],
) -> None:
    llm = llm_factory("Проверь membership.")
    processor = processor_factory(
        transcriber_factory("посмотри мембершип"),
        llm,
        settings=settings_factory(llm_temperature=0.4, llm_dictation_temperature=0.0),
    )

    response = await processor.process(AUDIO, request_id="req-task")

    system, _user, temperature = llm.calls[0]
    assert system == "Ты редактор."
    assert temperature == pytest.approx(0.4)
    assert response.output == "Проверь membership."


async def test_transform_only_honours_the_dictation_flag(
    processor_factory: ProcessorFactory,
    transcriber_factory: Callable[..., Any],
    llm_factory: Callable[..., Any],
    settings_factory: Callable[..., Settings],
) -> None:
    llm = llm_factory("Расшифровка, готово.")
    processor = processor_factory(
        transcriber_factory(),
        llm,
        settings=settings_factory(llm_temperature=0.4, llm_dictation_temperature=0.0),
    )

    response = await processor.transform_only(
        "посмотри мембершип", request_id="req-tx-dict", dictation=True
    )

    system, _user, temperature = llm.calls[0]
    assert system == "Ты диктофон."
    assert temperature == pytest.approx(0.0)
    assert response.output == "Расшифровка, готово."
