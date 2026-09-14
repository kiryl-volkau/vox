"""Answering in the language that was spoken, which is what a request gets by default.

"auto" is not a language anybody writes in, so it must never reach a prompt: these tests pin
where it gets turned into a real code, and what happens when there is nothing to turn it into.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from vox_server.languages import AUTO_LANGUAGE, FALLBACK_LANGUAGE
from vox_server.processor import Processor
from vox_server.prompt import Prompt
from vox_server.trace import TraceWriter

type Fake = Callable[..., object]
type ProcessorFactory = Callable[..., Processor]
type PromptFactory = Callable[..., Prompt]
type SettingsFactory = Callable[..., object]

RUSSIAN = "проверь этот сервис"
ENGLISH = "check this service"

# A prompt whose language block is the language code itself, so the rendered system prompt
# says outright which block was chosen.
BLOCKS = (("en", "ANSWER-EN"), ("ru", "ANSWER-RU"))


def _prompt(prompt_factory: PromptFactory) -> Prompt:
    return prompt_factory(system_prompt="Editor.\n{language}", language_blocks=BLOCKS)


def _chosen_block(llm: Any) -> str:
    """The last line of the system prompt, which for these prompts is the language block."""
    system, _, _ = llm.calls[0]
    return str(system).strip().splitlines()[-1]


async def test_a_recording_is_answered_in_the_language_whisper_heard(
    transcriber_factory: Fake,
    llm_factory: Fake,
    processor_factory: ProcessorFactory,
    prompt_factory: PromptFactory,
) -> None:
    llm = llm_factory("готово")
    processor = processor_factory(
        transcriber_factory(RUSSIAN, language="ru"), llm, _prompt(prompt_factory)
    )

    response = await processor.process(b"audio", request_id="req-1", language=AUTO_LANGUAGE)

    assert _chosen_block(llm) == "ANSWER-RU"
    assert response.language == "ru"


async def test_the_same_request_follows_whisper_to_another_language(
    transcriber_factory: Fake,
    llm_factory: Fake,
    processor_factory: ProcessorFactory,
    prompt_factory: PromptFactory,
) -> None:
    llm = llm_factory("done")
    processor = processor_factory(
        transcriber_factory(ENGLISH, language="en"), llm, _prompt(prompt_factory)
    )

    await processor.process(b"audio", request_id="req-2", language=AUTO_LANGUAGE)

    assert _chosen_block(llm) == "ANSWER-EN"


async def test_a_named_language_still_overrides_what_was_spoken(
    transcriber_factory: Fake,
    llm_factory: Fake,
    processor_factory: ProcessorFactory,
    prompt_factory: PromptFactory,
) -> None:
    """Auto is a default, not a policy: asking for English over Russian speech still works."""
    llm = llm_factory("done")
    processor = processor_factory(
        transcriber_factory(RUSSIAN, language="ru"), llm, _prompt(prompt_factory)
    )

    await processor.process(b"audio", request_id="req-3", language="en")

    assert _chosen_block(llm) == "ANSWER-EN"


@pytest.mark.parametrize("detected", ["", "   ", "!!"])
async def test_a_detection_nobody_can_read_falls_back(
    transcriber_factory: Fake,
    llm_factory: Fake,
    processor_factory: ProcessorFactory,
    prompt_factory: PromptFactory,
    detected: str,
) -> None:
    """Whisper reporting nothing usable must not put a nonsense language into a prompt."""
    llm = llm_factory("done")
    processor = processor_factory(
        transcriber_factory(RUSSIAN, language=detected), llm, _prompt(prompt_factory)
    )

    await processor.process(b"audio", request_id="req-4", language=AUTO_LANGUAGE)

    assert _chosen_block(llm) == "ANSWER-EN"


async def test_a_replay_is_read_from_the_text_because_there_is_no_audio(
    transcriber_factory: Fake,
    llm_factory: Fake,
    processor_factory: ProcessorFactory,
    prompt_factory: PromptFactory,
) -> None:
    llm = llm_factory("готово")
    processor = processor_factory(
        transcriber_factory(RUSSIAN), llm, _prompt(prompt_factory)
    )

    await processor.transform_only(RUSSIAN, request_id="req-5", language=AUTO_LANGUAGE)

    assert _chosen_block(llm) == "ANSWER-RU"


async def test_a_replay_of_english_text_is_answered_in_english(
    transcriber_factory: Fake,
    llm_factory: Fake,
    processor_factory: ProcessorFactory,
    prompt_factory: PromptFactory,
) -> None:
    llm = llm_factory("done")
    processor = processor_factory(
        transcriber_factory(ENGLISH), llm, _prompt(prompt_factory)
    )

    await processor.transform_only(ENGLISH, request_id="req-6", language=AUTO_LANGUAGE)

    assert _chosen_block(llm) == "ANSWER-EN"


async def test_auto_is_what_a_request_that_names_no_language_gets(
    transcriber_factory: Fake,
    llm_factory: Fake,
    processor_factory: ProcessorFactory,
    prompt_factory: PromptFactory,
    settings_factory: SettingsFactory,
) -> None:
    """The shipped default: Russian speech comes back in Russian with nothing configured."""
    llm = llm_factory("готово")
    processor = processor_factory(
        transcriber_factory(RUSSIAN, language="ru"),
        llm,
        _prompt(prompt_factory),
        settings=settings_factory(default_language=AUTO_LANGUAGE),
    )

    await processor.process(b"audio", request_id="req-7", language=None)

    assert _chosen_block(llm) == "ANSWER-RU"


async def test_the_trace_keeps_both_what_was_asked_for_and_what_was_written(
    transcriber_factory: Fake,
    llm_factory: Fake,
    processor_factory: ProcessorFactory,
    prompt_factory: PromptFactory,
    tmp_path: Path,
) -> None:
    """Auto resolving to "ru" is exactly the thing a reader of a trace needs to see happen."""
    processor = processor_factory(
        transcriber_factory(RUSSIAN, language="ru"),
        llm_factory("готово"),
        _prompt(prompt_factory),
        trace_writer=TraceWriter(tmp_path, 10),
    )

    await processor.process(b"audio", request_id="req-8", language=AUTO_LANGUAGE)
    written = json.loads(next(iter(tmp_path.glob("*.json"))).read_text(encoding="utf-8"))

    assert written["language"] == {"requested": AUTO_LANGUAGE, "written": "ru"}


def test_auto_never_reaches_a_prompt_as_a_language_block(prompt_factory: PromptFactory) -> None:
    """There is no "## LANGUAGE auto" to find, so asking for one has to fall back."""
    prompt = _prompt(prompt_factory)

    assert prompt.language_block(AUTO_LANGUAGE) == dict(BLOCKS)[FALLBACK_LANGUAGE]
