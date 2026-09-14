"""The analysis pass inside the pipeline: when it runs, what it changes, and how it fails.

The rule these tests hold the Processor to is that the reporting is never allowed to cost a
request its message. Every way the second call can go wrong - refused, timed out, unreadable,
not configured - has to end with the same message the first call produced.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tests.conftest import make_prompt
from vox_server.llm import LlmResponseError, LlmTimeoutError
from vox_server.processor import Processor
from vox_server.prompt import Prompt

type Fake = Callable[..., object]
type ProcessorFactory = Callable[..., Processor]
type PromptFactory = Callable[..., Prompt]

TRANSCRIPT = "прогони проверку по этим изменениям перед тем как я закоммичу"
MESSAGE = "Run the checks over these changes before I commit."

SUBAGENT = json.dumps(
    {
        "action": "прогони",
        "target": "эти изменения",
        "constraints": ["перед тем как я закоммичу"],
        "uncertainty": [],
        "claude_code": {"tool": "subagent", "why": "прогони проверку"},
    },
    ensure_ascii=False,
)
ORDINARY = json.dumps(
    {
        "action": "добавь",
        "target": "метод",
        "constraints": [],
        "uncertainty": [],
        "claude_code": {"tool": "none", "why": ""},
    },
    ensure_ascii=False,
)


def _analysis_prompt() -> Prompt:
    return make_prompt("Ты разбираешь запрос.", "{transcript}\n{output}")


async def test_without_an_analysis_prompt_the_pipeline_makes_one_call(
    transcriber_factory: Fake, llm_factory: Fake, processor_factory: ProcessorFactory
) -> None:
    llm = llm_factory(MESSAGE)
    processor = processor_factory(transcriber_factory(TRANSCRIPT), llm)

    response = await processor.transform_only(TRANSCRIPT, request_id="req-1")

    assert response.output == MESSAGE
    assert response.analysis is None
    assert len(llm.calls) == 1  # type: ignore[attr-defined]


async def test_the_second_call_reports_how_the_speech_was_read(
    transcriber_factory: Fake, llm_factory: Fake, processor_factory: ProcessorFactory
) -> None:
    llm = llm_factory(MESSAGE, analysis_reply=SUBAGENT)
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT), llm, analysis_prompt=_analysis_prompt()
    )

    response = await processor.transform_only(TRANSCRIPT, request_id="req-2")

    assert response.analysis is not None
    assert response.analysis.action == "прогони"
    assert response.analysis.constraints == ("перед тем как я закоммичу",)
    assert response.analysis.claude_code.tool == "subagent"


async def test_only_the_second_call_is_constrained_to_the_schema(
    transcriber_factory: Fake, llm_factory: Fake, processor_factory: ProcessorFactory
) -> None:
    """The rewrite stays exactly the call it was; the schema belongs to the reporting."""
    llm = llm_factory(MESSAGE, analysis_reply=ORDINARY)
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT), llm, analysis_prompt=_analysis_prompt()
    )

    await processor.transform_only(TRANSCRIPT, request_id="req-3")

    formats = llm.response_formats  # type: ignore[attr-defined]
    assert formats[0] is None
    assert formats[1] is not None


async def test_the_analysis_call_sees_the_transcript_and_the_finished_message(
    transcriber_factory: Fake, llm_factory: Fake, processor_factory: ProcessorFactory
) -> None:
    llm = llm_factory(MESSAGE, analysis_reply=ORDINARY)
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT), llm, analysis_prompt=_analysis_prompt()
    )

    await processor.transform_only(TRANSCRIPT, request_id="req-4")

    _, user, temperature = llm.calls[1]  # type: ignore[attr-defined]
    assert TRANSCRIPT in user
    assert MESSAGE in user
    assert temperature == 0.0


async def test_a_named_tool_is_appended_to_the_message(
    transcriber_factory: Fake, llm_factory: Fake, processor_factory: ProcessorFactory
) -> None:
    llm = llm_factory(MESSAGE, analysis_reply=SUBAGENT)
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT), llm, analysis_prompt=_analysis_prompt()
    )

    response = await processor.transform_only(TRANSCRIPT, request_id="req-5", language="en")

    assert response.output == f"{MESSAGE}\n\nUse a subagent for this."


async def test_an_ordinary_request_keeps_its_message_exactly(
    transcriber_factory: Fake, llm_factory: Fake, processor_factory: ProcessorFactory
) -> None:
    llm = llm_factory(MESSAGE, analysis_reply=ORDINARY)
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT), llm, analysis_prompt=_analysis_prompt()
    )

    response = await processor.transform_only(TRANSCRIPT, request_id="req-6", language="en")

    assert response.output == MESSAGE


async def test_dictation_never_takes_an_appended_instruction(
    transcriber_factory: Fake, llm_factory: Fake, processor_factory: ProcessorFactory
) -> None:
    """Dictation hands back the speaker's own words; an instruction there was never said."""
    llm = llm_factory(MESSAGE, analysis_reply=SUBAGENT)
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT), llm, analysis_prompt=_analysis_prompt()
    )

    response = await processor.transform_only(
        TRANSCRIPT, request_id="req-7", dictation=True, language="en"
    )

    assert response.output == MESSAGE
    assert response.analysis is not None


async def test_a_backend_that_refuses_the_schema_is_asked_again_plainly(
    transcriber_factory: Fake, llm_factory: Fake, processor_factory: ProcessorFactory
) -> None:
    llm = llm_factory(MESSAGE, refuse_response_format=True)
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT), llm, analysis_prompt=_analysis_prompt()
    )

    response = await processor.transform_only(TRANSCRIPT, request_id="req-8")

    assert response.output == MESSAGE
    assert len(llm.calls) == 3  # type: ignore[attr-defined]


@pytest.mark.parametrize("failure", [LlmTimeoutError("too slow"), LlmResponseError("HTTP 500")])
async def test_a_failing_analysis_call_still_returns_the_message(
    transcriber_factory: Fake,
    processor_factory: ProcessorFactory,
    failure: Exception,
) -> None:
    """The reporting is a diagnostic. Losing it must never lose what the person dictated."""

    class OnlyTheRewriteWorks:
        model = "fake"
        base_url = "http://fake"

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, system: str, user: str, **kwargs: object) -> str:
            self.calls += 1
            if self.calls == 1:
                return MESSAGE
            raise failure

    llm = OnlyTheRewriteWorks()
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT), llm, analysis_prompt=_analysis_prompt()
    )

    response = await processor.transform_only(TRANSCRIPT, request_id="req-9")

    assert response.output == MESSAGE
    assert response.analysis is None


async def test_an_unreadable_report_leaves_the_message_alone(
    transcriber_factory: Fake, llm_factory: Fake, processor_factory: ProcessorFactory
) -> None:
    llm = llm_factory(MESSAGE, analysis_reply="I could not do that, sorry.")
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT), llm, analysis_prompt=_analysis_prompt()
    )

    response = await processor.transform_only(TRANSCRIPT, request_id="req-10")

    assert response.output == MESSAGE
    assert response.analysis is None


async def test_a_transcript_that_produced_nothing_is_never_analysed(
    transcriber_factory: Fake, llm_factory: Fake, processor_factory: ProcessorFactory
) -> None:
    """A request that fell back to the transcript has no message to report on."""
    llm = llm_factory("   ", analysis_reply=SUBAGENT)
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT), llm, analysis_prompt=_analysis_prompt()
    )

    response = await processor.transform_only(TRANSCRIPT, request_id="req-11")

    assert response.output == TRANSCRIPT
    assert response.analysis is None
    assert len(llm.calls) == 1  # type: ignore[attr-defined]
