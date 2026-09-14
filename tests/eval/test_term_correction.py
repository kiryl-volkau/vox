"""Evaluation: does the pipeline turn a mangled transcript into a sensible message?

Skipped unless VOX_EVAL=1, and deselected by default through the ``eval`` marker, so a normal
``pytest`` run never needs a backend, a model or the network.

    docker compose up -d
    VOX_EVAL=1 uv run pytest tests/eval -m eval -v

Two gradings run over the same answers. The deterministic one asks whether the mangled word
became the canonical English term and nothing was invented; the judge asks whether the term
makes sense where it landed and the meaning survived.

Both are held to a pass rate rather than to every single case, because both grade a model: a
7B rewriter and a 7B judge each fail a case now and then for reasons no code change fixes.
The per-case tests are what say *which* case failed, and a case that is known to fail today
carries ``known_gap`` in the dataset, so a red run means a regression rather than a gap
somebody already wrote down.
"""

from __future__ import annotations

import os

import pytest

from .conftest import CASES, MIN_JUDGE_PASS_RATE, MIN_PASS_RATE, Evaluation
from .dataset import EvalCase

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        os.environ.get("VOX_EVAL") != "1",
        reason="set VOX_EVAL=1 and start the backend to run the evaluation",
    ),
]


CASE_IDS = [case.id for case in CASES]


def _recovery_parameters() -> list[object]:
    """One parameter per case, xfailed where the dataset records a known recovery gap.

    Only term recovery is marked. A gap in recovering a word says nothing about whether the
    answer came back in the right language, and marking that check too would report a healthy
    language result as an unexpected pass.
    """
    return [
        pytest.param(
            case,
            id=case.id,
            marks=(
                [pytest.mark.xfail(reason=case.known_gap, strict=False)] if case.known_gap else []
            ),
        )
        for case in CASES
    ]


def _report(evaluation: Evaluation) -> str:
    corrected = evaluation.case.suggest_correction(evaluation.output)
    return (
        f"{evaluation.case.id}: {evaluation.score.describe()}\n"
        f"  transcript: {evaluation.case.transcript}\n"
        f"  output:     {evaluation.output}"
        + (f"\n  should be:  {corrected}" if corrected else "")
    )


@pytest.mark.parametrize("case", _recovery_parameters())
def test_the_mangled_term_is_replaced_by_the_real_one(
    case: EvalCase, evaluations: dict[str, Evaluation]
) -> None:
    """The nonsense token is gone, the canonical English term is there, nothing was invented."""
    evaluation = evaluations[case.id]

    assert evaluation.score.passed, _report(evaluation)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_the_answer_is_written_in_the_requested_language(
    case: EvalCase, evaluations: dict[str, Evaluation]
) -> None:
    """An English answer may carry no Cyrillic, and a Russian one must carry some.

    Crude on purpose: it is the one language check that cannot itself be wrong. Identifiers
    stay Latin in either language, so the Russian direction asserts presence rather than
    absence.
    """
    output = evaluations[case.id].output
    cyrillic = any("Ѐ" <= character <= "ӿ" for character in output)

    if case.language == "en":
        assert not cyrillic, f"{case.id}: English answer carries Cyrillic: {output}"
    elif case.language == "ru":
        assert cyrillic, f"{case.id}: Russian answer carries no Cyrillic: {output}"


def test_the_term_recovery_rate_meets_the_threshold(evaluations: dict[str, Evaluation]) -> None:
    """The gate for the whole set, known gaps included: a drop here is a regression."""
    failures = [_report(e) for e in evaluations.values() if not e.score.passed]
    rate = 1.0 - len(failures) / len(evaluations)

    assert rate >= MIN_PASS_RATE, (
        f"term recovery {rate:.0%} is below the required {MIN_PASS_RATE:.0%}\n"
        + "\n".join(failures)
    )


def test_the_judge_finds_the_rewrites_sensible(evaluations: dict[str, Evaluation]) -> None:
    """An LLM grades whether each term makes sense where it landed and nothing drifted."""
    failures = [
        f"  {case_id}: {evaluation.verdict.describe()}\n"
        f"    transcript: {evaluation.case.transcript}\n"
        f"    output:     {evaluation.output}"
        for case_id, evaluation in evaluations.items()
        if not evaluation.verdict.passed
    ]
    rate = 1.0 - len(failures) / len(evaluations)

    assert rate >= MIN_JUDGE_PASS_RATE, (
        f"judge pass rate {rate:.0%} is below the required {MIN_JUDGE_PASS_RATE:.0%}\n"
        + "\n".join(failures)
    )


def test_every_case_got_a_readable_verdict(evaluations: dict[str, Evaluation]) -> None:
    """A judge that never returns usable JSON would let the rate above pass on noise."""
    unparseable = [
        f"{case_id}: {evaluation.verdict.reason}"
        for case_id, evaluation in evaluations.items()
        if not evaluation.verdict.parsed
    ]

    assert len(unparseable) <= len(evaluations) // 4, (
        "the judge model mostly failed to answer with JSON, so its verdicts mean nothing:\n"
        + "\n".join(unparseable)
    )
