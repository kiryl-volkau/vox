"""The evaluation set itself, checked offline.

A case with a typo in the term it expects, or one that expects a word the glossary never
teaches, reads as a model failure when the evaluation runs. These tests make the dataset
answer for itself first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.eval.dataset import DatasetError, load_cases
from vox_server.glossary import Glossary


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "cases.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_repository_evaluation_set_loads() -> None:
    cases = load_cases()

    assert len(cases) >= 10
    assert all(case.transcript and case.intent for case in cases)
    assert len({case.id for case in cases}) == len(cases)


def test_every_case_states_the_language_its_answer_must_use() -> None:
    languages = {case.language for case in load_cases()}

    assert languages <= {"en", "ru"}
    # Both directions have to be exercised: an English-only set would never catch a prompt
    # that lost its Russian language block.
    assert {"en", "ru"} <= languages


def test_every_recovery_case_expects_a_term_the_glossary_actually_teaches(
    repo_root: Path,
) -> None:
    """A case may only require a correction the model was given the means to make.

    Expecting a word that appears in no glossary entry would be testing the model's world
    knowledge, not this pipeline, and would fail for a reason nobody could act on.
    """
    glossary = Glossary.load(repo_root / "config" / "glossary.yaml")
    taught = {
        term.casefold()
        for entry in glossary.entries
        for _code, term in entry.terms
    }

    for case in load_cases():
        if not case.garbled:
            continue
        assert any(term.casefold() in taught for term in case.expect), (
            f"case {case.id!r} expects {case.expect}, none of which the glossary teaches"
        )


def test_every_expected_term_is_written_in_english(repo_root: Path) -> None:
    """Identifiers stay English whatever language the answer is written in."""
    del repo_root
    for case in load_cases():
        for term in case.expect:
            assert not any("Ѐ" <= character <= "ӿ" for character in term), (
                f"case {case.id!r} expects a Cyrillic term {term!r}"
            )


def test_a_case_that_expects_nothing_is_rejected(tmp_path: Path) -> None:
    """Such a case passes unconditionally, which is worse than having no case."""
    path = _write(
        tmp_path,
        "cases:\n"
        "  - id: empty\n"
        "    transcript: что-то сказали\n"
        "    language: en\n"
        "    expect: []\n"
        "    intent: nothing\n",
    )

    with pytest.raises(DatasetError, match="never fail"):
        load_cases(path)


def test_a_duplicate_id_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "cases:\n"
        "  - id: same\n"
        "    transcript: раз\n"
        "    language: en\n"
        "    expect: [index]\n"
        "    intent: one\n"
        "  - id: same\n"
        "    transcript: два\n"
        "    language: en\n"
        "    expect: [index]\n"
        "    intent: two\n",
    )

    with pytest.raises(DatasetError, match="duplicate case id"):
        load_cases(path)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("cases: []\n", "no 'cases' list"),
        ("- one\n- two\n", "not a mapping"),
        ("cases:\n  - id: x\n    language: en\n    expect: [a]\n    intent: i\n", "transcript"),
        ("cases:\n  - id: x\n    transcript: t\n    expect: [a]\n    intent: i\n", "language"),
        ("cases:\n  - id: x\n    transcript: t\n    language: en\n    expect: a\n", "list of"),
    ],
)
def test_a_malformed_set_is_rejected_naming_the_problem(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(DatasetError, match=message):
        load_cases(_write(tmp_path, body))


def test_an_unreadable_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="cannot read"):
        load_cases(tmp_path / "absent.yaml")


def test_a_case_scores_its_own_output() -> None:
    case = next(case for case in load_cases() if case.id == "membership")

    good = case.score("Check why a user's membership is not loaded after an invite.")
    bad = case.score("Проверь мембершип и добавь тесты.")

    assert good.passed
    assert not bad.passed
    assert case.suggest_correction(bad_output := "Проверь мембершип у юзера.") is not None
    assert "membership" in str(case.suggest_correction(bad_output))
