"""Evaluation: does the second pass pick the right Claude Code affordance?

Skipped unless VOX_EVAL=1 and deselected by default, exactly like the term-recovery set:

    docker compose up -d
    VOX_EVAL=1 uv run pytest tests/eval -m eval -v

The hint this grades is appended to the message before it reaches Claude Code, so the two
ways of being wrong do not cost the same. Naming a tool that was never asked for puts an
instruction in front of Claude Code that nobody spoke; missing one costs a convenience. The
per-case tests say which case moved, and ``test_no_tool_is_invented_for_ordinary_work`` is the
one that must never go red, because it is the expensive direction.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import httpx
import pytest

from vox_server.analysis import TOOLS

from .conftest import BASE_URL, REQUEST_TIMEOUT
from .tools_dataset import ToolCase, load_tool_cases

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        os.environ.get("VOX_EVAL") != "1",
        reason="set VOX_EVAL=1 and start the backend to run the evaluation",
    ),
]

TOOL_CASES = load_tool_cases()
MIN_TOOL_PASS_RATE = float(os.environ.get("VOX_EVAL_MIN_TOOL_PASS_RATE", "0.7"))


@pytest.fixture(scope="session")
def tool_answers() -> Iterator[dict[str, str]]:
    """Replay every case through the backend once and keep the tool each one came back with.

    Cached for the session because each case costs two model round trips and three tests ask
    about the same answers.
    """
    answers: dict[str, str] = {}
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        for case in TOOL_CASES:
            response = client.post(
                f"{BASE_URL}/v1/transform",
                json={"text": case.transcript, "dictation": False, "language": case.language},
            )
            if response.status_code != 200:
                pytest.fail(f"case {case.id!r}: backend answered {response.status_code}")
            body = response.json()
            analysis = body.get("analysis")
            if analysis is None:
                pytest.fail(
                    f"case {case.id!r}: the backend returned no analysis at all. "
                    "The second pass is not running, which is a wiring failure rather than "
                    "a model one."
                )
            answers[case.id] = str(analysis["claude_code"]["tool"])
    yield answers


def _parameters() -> list[object]:
    return [
        pytest.param(
            case,
            id=case.id,
            marks=(
                [pytest.mark.xfail(reason=case.known_gap, strict=False)] if case.known_gap else []
            ),
        )
        for case in TOOL_CASES
    ]


@pytest.mark.parametrize("case", _parameters())
def test_the_spoken_job_picks_the_right_tool(
    case: ToolCase, tool_answers: dict[str, str]
) -> None:
    got = tool_answers[case.id]

    assert got == case.tool, (
        f"{case.id}: expected {case.tool!r}, got {got!r}\n"
        f"    transcript: {case.transcript}\n"
        f"    why {case.tool!r} is right: {case.intent}"
    )


def test_no_tool_is_invented_for_ordinary_work(tool_answers: dict[str, str]) -> None:
    """The expensive direction: an instruction nobody spoke, carried into Claude Code."""
    invented = [
        f"{case.id}: got {tool_answers[case.id]!r} for {case.transcript!r}"
        for case in TOOL_CASES
        if case.tool == "none" and tool_answers[case.id] != "none"
    ]
    ordinary = sum(case.tool == "none" for case in TOOL_CASES)
    rate = 1 - len(invented) / ordinary

    assert rate >= MIN_TOOL_PASS_RATE, "a tool was invented for ordinary work:\n  " + "\n  ".join(
        invented
    )


def test_the_tool_pass_rate_meets_the_threshold(tool_answers: dict[str, str]) -> None:
    correct = sum(tool_answers[case.id] == case.tool for case in TOOL_CASES)
    rate = correct / len(TOOL_CASES)

    assert rate >= MIN_TOOL_PASS_RATE, (
        f"{correct}/{len(TOOL_CASES)} cases picked the right tool "
        f"({rate:.0%}, floor {MIN_TOOL_PASS_RATE:.0%})"
    )


def test_every_answer_is_one_of_the_declared_tools(tool_answers: dict[str, str]) -> None:
    """The schema constrains this, so a stray value means the constraint was not applied."""
    assert set(tool_answers.values()) <= set(TOOLS)
