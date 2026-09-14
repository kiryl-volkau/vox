"""Parsing what the judge model answers, checked offline.

A small model asked for JSON returns JSON wrapped in prose, in fences, with string booleans,
or not at all. Every one of those has to resolve to a verdict rather than an exception: a
crash mid-evaluation loses the answers for every case that already cost a model call.
"""

from __future__ import annotations

import pytest

from tests.eval.judge import parse_verdict

CLEAN = '{"terms_sensible": true, "faithful": true, "invented": false, "reason": "fine"}'


def test_a_clean_answer_parses_and_passes() -> None:
    verdict = parse_verdict(CLEAN)

    assert verdict.parsed
    assert verdict.passed
    assert verdict.reason == "fine"
    assert verdict.describe() == "ok"


@pytest.mark.parametrize(
    "raw",
    [
        f"```json\n{CLEAN}\n```",
        f"```\n{CLEAN}\n```",
        f"Here is my verdict:\n{CLEAN}",
        f"<think>let me check</think>\n{CLEAN}",
        f"{CLEAN}\n\nHope that helps.",
    ],
)
def test_json_is_found_through_the_wrapping(raw: str) -> None:
    verdict = parse_verdict(raw)

    assert verdict.parsed
    assert verdict.passed


def test_string_booleans_are_accepted() -> None:
    """Refusing "true" would grade the judge's JSON rather than the rewriter."""
    verdict = parse_verdict(
        '{"terms_sensible": "true", "faithful": "yes", "invented": "false", "reason": "ok"}'
    )

    assert verdict.parsed
    assert verdict.passed


def test_a_failing_verdict_names_every_failed_dimension() -> None:
    verdict = parse_verdict(
        '{"terms_sensible": false, "faithful": false, "invented": true,'
        ' "reason": "added tests nobody asked for"}'
    )

    described = verdict.describe()
    assert not verdict.passed
    assert "terms_sensible" in described
    assert "faithful" in described
    assert "not invented" in described
    assert "added tests nobody asked for" in described


def test_invention_alone_fails_the_verdict() -> None:
    verdict = parse_verdict(
        '{"terms_sensible": true, "faithful": true, "invented": true, "reason": "extra work"}'
    )

    assert verdict.parsed
    assert not verdict.passed


@pytest.mark.parametrize("raw", ["", "   ", "I cannot answer that.", "{not json at all", "[1, 2]"])
def test_an_unusable_answer_is_a_verdict_rather_than_a_crash(raw: str) -> None:
    verdict = parse_verdict(raw)

    assert not verdict.parsed
    assert not verdict.passed
    assert "unparseable" in verdict.describe()


def test_missing_fields_default_to_failure() -> None:
    """A judge that omits a dimension has not approved it."""
    verdict = parse_verdict('{"reason": "said nothing useful"}')

    assert verdict.parsed
    assert not verdict.passed


def test_a_long_reason_is_truncated() -> None:
    verdict = parse_verdict(
        '{"terms_sensible": true, "faithful": true, "invented": false, "reason": "%s"}'
        % ("x" * 500)
    )

    assert len(verdict.reason) <= 300
