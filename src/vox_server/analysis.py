"""The structured reading of one dictated request, as the model reports it.

A second pass over a finished request asks the model to report how the speech was read - the
action, what the action is aimed at, the constraints it heard, what it was unsure of, and
whether the job is one Claude Code should hand to a subagent. That report is what makes a bad
rewrite diagnosable after the fact instead of only visible as a sentence that reads wrong.

It is a pass of its own rather than extra fields on the rewrite because asking one 7B model to
do both measurably cost the rewrite: with the reporting folded into it, English requests came
back in Russian on a third of the evaluation set.

Nothing here may fail a request. Every model on the other end of an OpenAI-compatible URL is
free to ignore the schema, answer with prose, or reject ``response_format`` outright, so
:func:`parse_analysis` returns None rather than raising and the caller falls back to treating
the whole reply as the message.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

type Tool = Literal["subagent", "review", "plan", "none"]

# "none" leads the enum because a constrained model reaches for the first value when the
# speech does not clearly pick one, and the harmless answer is the one that should win.
TOOLS: tuple[Tool, ...] = ("none", "subagent", "review", "plan")

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

# The instruction appended to the message when the model says the job wants one of Claude
# Code's own affordances. The server writes this sentence rather than the model, so the same
# decision always reads the same way and can be tested; the model only chooses the tool.
# English is the fallback for any output language without a line of its own.
_HINT_LINES: dict[Tool, dict[str, str]] = {
    "subagent": {
        "en": "Use a subagent for this.",
        "ru": "Сделай это через суб-агента.",
    },
    "review": {
        "en": "Review this rather than change it.",
        "ru": "Проверь это, не меняя код.",
    },
    "plan": {
        "en": "Plan this first, do not edit yet.",
        "ru": "Сначала составь план, код пока не меняй.",
    },
    "none": {},
}


class ClaudeCodeHint(BaseModel):
    """Which Claude Code affordance the spoken job calls for, and why.

    ``tool`` is ``"none"`` for ordinary work, which is most of it. ``why`` is one short line
    for the person reading the trace; it never reaches the message.
    """

    tool: Tool = "none"
    why: str = ""


class Analysis(BaseModel):
    """One request as the model read it.

    Reporting only: the message itself is never in here, because the pass that produces this
    is given the finished message rather than writing it. ``action`` and ``target`` are the
    verb that was spoken and the thing it was aimed at, ``constraints`` the limits heard
    ("only in this module", "without touching the tests"), and ``uncertainty`` whatever the
    speaker hedged or the recogniser left unreadable.
    """

    action: str = ""
    target: str = ""
    constraints: tuple[str, ...] = ()
    uncertainty: tuple[str, ...] = ()
    claude_code: ClaudeCodeHint = ClaudeCodeHint()


def _array_of_strings(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


# Sent as OpenAI's `response_format: {"type": "json_schema", ...}`. Every property is required
# and additionalProperties is false because that is what strict mode demands; Ollama honours
# the same shape, and a backend that rejects it entirely is handled by the caller's retry.
ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "description": "The verb that was actually spoken."},
        "target": {"type": "string", "description": "What that verb was aimed at."},
        "constraints": _array_of_strings("Limits heard in the speech, each in its own string."),
        "uncertainty": _array_of_strings("What the speaker hedged or left unclear."),
        "claude_code": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tool": {
                    "type": "string",
                    "enum": list(TOOLS),
                    "description": "'none' unless the job clearly calls for the affordance.",
                },
                "why": {"type": "string", "description": "One short line, or empty."},
            },
            "required": ["tool", "why"],
        },
    },
    "required": ["action", "target", "constraints", "uncertainty", "claude_code"],
}

RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "vox_analysis", "strict": True, "schema": ANALYSIS_SCHEMA},
}


def parse_analysis(raw: str) -> Analysis | None:
    """Read the model's reply as an :class:`Analysis`, or return None when it is not one.

    Tolerant on purpose about what surrounds the object - a reply wrapped in a markdown fence
    or carrying a sentence before it still parses, because the outermost ``{...}`` is what is
    read. Returns None when there is no object, when it is not valid JSON, or when it does not
    fit the schema; the message itself never depends on this pass, so a failed read costs the
    reporting and nothing else.
    """
    if not raw or not raw.strip():
        return None
    match = _JSON_OBJECT.search(raw)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return Analysis.model_validate(payload)
    except ValidationError as exc:
        logger.debug("the model's JSON did not fit the analysis schema", exc_info=exc)
        return None


def hint_line(analysis: Analysis, language: str) -> str:
    """Return the sentence to append for ``analysis``, or "" when there is nothing to add.

    Empty for ``tool == "none"`` and for any tool the model invented despite the enum.
    """
    lines = _HINT_LINES.get(analysis.claude_code.tool)
    if not lines:
        return ""
    return lines.get(language) or lines.get("en", "")


def with_hint(output: str, analysis: Analysis, language: str) -> str:
    """Return ``output`` with the Claude Code instruction appended, if there is one.

    The instruction goes on its own line after the message so that the message the person
    dictated stays readable on its own. An output that already ends with the exact sentence
    is left alone, which keeps a replay through ``/v1/transform`` from stacking hints.
    """
    line = hint_line(analysis, language)
    if not line:
        return output
    body = output.rstrip()
    if body.endswith(line):
        return body
    return f"{body}\n\n{line}" if body else line
