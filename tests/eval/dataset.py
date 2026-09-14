"""Loading and validating the term-recovery evaluation set.

Separate from the tests that run it so the dataset can be checked offline: a case with a
typo in the term it expects would otherwise read as a model failure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .scoring import CaseScore, score, suggest_correction

CASES_PATH = Path(__file__).with_name("cases.yaml")


class DatasetError(ValueError):
    """The evaluation set is malformed, which is a bug in the set rather than in the model."""


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One transcript and everything its rewritten form is required to do."""

    id: str
    transcript: str
    language: str
    garbled: str
    expect: tuple[str, ...]
    forbid: tuple[str, ...]
    intent: str
    known_gap: str = ""

    def score(self, output: str) -> CaseScore:
        return score(
            self.id, output, garbled=self.garbled, expect=self.expect, forbid=self.forbid
        )

    def suggest_correction(self, output: str) -> str | None:
        """The output as it should have read, or None when there is no swap to suggest."""
        return suggest_correction(output, garbled=self.garbled, expect=self.expect)


def load_cases(path: Path = CASES_PATH) -> tuple[EvalCase, ...]:
    """Read the evaluation set. Raises DatasetError naming the offending case."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DatasetError(f"{path}: cannot read the evaluation set: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise DatasetError(f"{path}: the evaluation set is not a mapping")
    entries = raw.get("cases")
    if not isinstance(entries, Sequence) or not entries:
        raise DatasetError(f"{path}: the evaluation set has no 'cases' list")

    cases: list[EvalCase] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        case = _parse(entry, index, path)
        if case.id in seen:
            raise DatasetError(f"{path}: duplicate case id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    return tuple(cases)


def _parse(entry: Any, index: int, path: Path) -> EvalCase:
    where = f"{path}: case #{index}"
    if not isinstance(entry, Mapping):
        raise DatasetError(f"{where} is not a mapping")
    case_id = _text(entry, "id", where, required=True)
    where = f"{path}: case {case_id!r}"
    transcript = _text(entry, "transcript", where, required=True)
    expect = _terms(entry, "expect", where)
    if not expect:
        raise DatasetError(f"{where} expects no terms, so it can never fail")
    return EvalCase(
        id=case_id,
        transcript=transcript,
        language=_text(entry, "language", where, required=True),
        garbled=_text(entry, "garbled", where, required=False),
        expect=expect,
        forbid=_terms(entry, "forbid", where),
        intent=_text(entry, "intent", where, required=True),
        known_gap=" ".join(_text(entry, "known_gap", where, required=False).split()),
    )


def _text(entry: Mapping[str, Any], name: str, where: str, *, required: bool) -> str:
    value = entry.get(name, "")
    if not isinstance(value, str):
        raise DatasetError(f"{where}: '{name}' must be a string")
    text = value.strip()
    if required and not text:
        raise DatasetError(f"{where}: '{name}' is required")
    return text


def _terms(entry: Mapping[str, Any], name: str, where: str) -> tuple[str, ...]:
    value = entry.get(name, [])
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise DatasetError(f"{where}: '{name}' must be a list of strings")
    terms: list[str] = []
    for term in value:
        if not isinstance(term, str) or not term.strip():
            raise DatasetError(f"{where}: '{name}' holds an empty or non-string term")
        terms.append(term.strip())
    return tuple(terms)
