"""Fixtures for the term-recovery evaluation.

Everything here talks to a backend that is actually running: the evaluation replays
transcripts through ``POST /v1/transform``, which runs the real prompt against the real model
without needing a microphone or Whisper. The whole set is sent once per session and cached,
because each case costs one model round trip and several tests ask about the same answer.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest

from .dataset import EvalCase, load_cases
from .judge import Verdict, build_judge, grade
from .scoring import CaseScore

BASE_URL = os.environ.get("VOX_EVAL_URL", "http://127.0.0.1:8765").rstrip("/")
REQUEST_TIMEOUT = float(os.environ.get("VOX_EVAL_TIMEOUT", "180"))
MIN_PASS_RATE = float(os.environ.get("VOX_EVAL_MIN_PASS_RATE", "0.8"))
MIN_JUDGE_PASS_RATE = float(os.environ.get("VOX_EVAL_MIN_JUDGE_PASS_RATE", "0.7"))

CASES = load_cases()


@dataclass(frozen=True, slots=True)
class Evaluation:
    """One case, the message the backend produced for it, and both gradings."""

    case: EvalCase
    output: str
    score: CaseScore
    verdict: Verdict


def _rewrite(client: httpx.Client, case: EvalCase) -> str:
    response = client.post(
        f"{BASE_URL}/v1/transform",
        json={"text": case.transcript, "dictation": False, "language": case.language},
    )
    if response.status_code != 200:
        pytest.fail(f"case {case.id!r}: backend answered {response.status_code}: {response.text}")
    return str(response.json().get("output", ""))


async def _grade_all(outputs: dict[str, str]) -> dict[str, Verdict]:
    judge = build_judge(timeout_seconds=REQUEST_TIMEOUT)
    try:
        verdicts: dict[str, Verdict] = {}
        for case in CASES:
            verdicts[case.id] = await grade(
                judge,
                intent=case.intent,
                transcript=case.transcript,
                output=outputs[case.id],
            )
        return verdicts
    finally:
        await judge.aclose()


@pytest.fixture(scope="session")
def evaluations() -> Iterator[dict[str, Evaluation]]:
    """Run every case through the backend, then through the judge. One pass per session."""
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        outputs = {case.id: _rewrite(client, case) for case in CASES}
    verdicts = asyncio.run(_grade_all(outputs))
    yield {
        case.id: Evaluation(
            case=case,
            output=outputs[case.id],
            score=case.score(outputs[case.id]),
            verdict=verdicts[case.id],
        )
        for case in CASES
    }
