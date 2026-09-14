"""The second pass: reading the model's report, and what it appends to a message."""

from __future__ import annotations

import json
from typing import cast

import pytest

from vox_server.analysis import (
    ANALYSIS_SCHEMA,
    RESPONSE_FORMAT,
    TOOLS,
    Analysis,
    ClaudeCodeHint,
    Tool,
    hint_line,
    parse_analysis,
    with_hint,
)

FULL = {
    "action": "посмотри",
    "target": "системные prompts",
    "constraints": ["только в этом модуле"],
    "uncertainty": ["кажется"],
    "claude_code": {"tool": "none", "why": ""},
}


def test_a_complete_report_is_read_field_by_field() -> None:
    analysis = parse_analysis(json.dumps(FULL, ensure_ascii=False))

    assert analysis is not None
    assert analysis.action == "посмотри"
    assert analysis.target == "системные prompts"
    assert analysis.constraints == ("только в этом модуле",)
    assert analysis.uncertainty == ("кажется",)
    assert analysis.claude_code.tool == "none"


def test_a_report_wrapped_in_prose_or_a_fence_is_still_read() -> None:
    body = json.dumps(FULL, ensure_ascii=False)

    assert parse_analysis(f"Here you go:\n```json\n{body}\n```") is not None
    assert parse_analysis(f"{body}\n\nHope that helps.") is not None


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "no object here at all",
        "{ not json",
        '["a", "list"]',
        '{"claude_code": {"tool": "invented", "why": ""}}',
        '{"constraints": "not an array"}',
    ],
)
def test_an_unusable_reply_reads_as_no_report(raw: str) -> None:
    """Never raises: the message does not depend on this pass, only the reporting does."""
    assert parse_analysis(raw) is None


def test_the_fields_the_model_left_out_default_to_empty() -> None:
    analysis = parse_analysis('{"action": "проверь"}')

    assert analysis is not None
    assert analysis.action == "проверь"
    assert analysis.target == ""
    assert analysis.constraints == ()
    assert analysis.claude_code.tool == "none"


def test_the_schema_requires_every_field_and_forbids_the_rest() -> None:
    """Strict mode is the only reason the reply has the keys it does, so it is asserted."""
    assert ANALYSIS_SCHEMA["additionalProperties"] is False
    assert set(ANALYSIS_SCHEMA["required"]) == set(ANALYSIS_SCHEMA["properties"])
    assert RESPONSE_FORMAT["json_schema"]["strict"] is True
    assert ANALYSIS_SCHEMA["properties"]["claude_code"]["properties"]["tool"]["enum"] == list(TOOLS)


def test_none_leads_the_tool_enum() -> None:
    """A constrained model reaches for the first value; the harmless one has to be first."""
    assert TOOLS[0] == "none"


@pytest.mark.parametrize(
    ("tool", "language", "expected"),
    [
        ("subagent", "en", "Use a subagent for this."),
        ("subagent", "ru", "Сделай это через суб-агента."),
        ("plan", "en", "Plan this first, do not edit yet."),
        ("review", "ru", "Проверь это, не меняя код."),
        ("subagent", "de", "Use a subagent for this."),
        ("none", "en", ""),
        ("none", "ru", ""),
    ],
)
def test_the_appended_line_follows_the_tool_and_the_output_language(
    tool: str, language: str, expected: str
) -> None:
    analysis = Analysis(claude_code=ClaudeCodeHint(tool=cast(Tool, tool), why="because"))

    assert hint_line(analysis, language) == expected


def test_the_hint_goes_on_a_line_of_its_own_after_the_message() -> None:
    analysis = Analysis(claude_code=ClaudeCodeHint(tool="subagent", why="a pre-review"))

    assert with_hint("Check the diff.", analysis, "en") == (
        "Check the diff.\n\nUse a subagent for this."
    )


def test_a_message_that_already_carries_the_hint_does_not_collect_a_second_one() -> None:
    """Replaying a stored message through /v1/transform must not stack instructions."""
    analysis = Analysis(claude_code=ClaudeCodeHint(tool="subagent", why="a pre-review"))
    once = with_hint("Check the diff.", analysis, "en")

    assert with_hint(once, analysis, "en") == once


def test_an_ordinary_request_gets_its_message_back_untouched() -> None:
    analysis = Analysis(action="добавь", target="метод")

    assert with_hint("Add a method.", analysis, "en") == "Add a method."
