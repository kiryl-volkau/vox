"""The tool evaluation set is itself checked, so a typo in it reads as a broken set.

A case naming a tool the code does not know, or two cases sharing an id, would otherwise be
reported as a model that answered wrongly - which is the one diagnosis that wastes the most
time, because nobody looks at the dataset when the model is the usual suspect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.eval.tools_dataset import ToolDatasetError, load_tool_cases

VALID = """
cases:
  - id: pre-commit-check
    transcript: прогони проверку перед коммитом
    language: en
    tool: subagent
    intent: A verification pass with a finding to bring back.
  - id: an-edit
    transcript: добавь метод в этот класс
    language: en
    tool: none
    intent: Ordinary editing.
    known_gap: the model answers subagent today
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tools.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_repository_tool_set_loads() -> None:
    cases = load_tool_cases()

    assert cases
    assert len({case.id for case in cases}) == len(cases)
    assert any(case.tool == "subagent" for case in cases)
    assert any(case.tool == "none" for case in cases)


def test_ordinary_work_is_the_larger_half_of_the_set() -> None:
    """Naming a tool nobody asked for is the expensive error, so it is the better covered one."""
    cases = load_tool_cases()
    ordinary = sum(case.tool == "none" for case in cases)

    assert ordinary >= len(cases) / 2


def test_a_case_is_read_field_by_field(tmp_path: Path) -> None:
    cases = load_tool_cases(_write(tmp_path, VALID))

    assert [case.id for case in cases] == ["pre-commit-check", "an-edit"]
    assert cases[0].tool == "subagent"
    assert cases[0].language == "en"
    assert cases[0].known_gap == ""
    assert cases[1].known_gap == "the model answers subagent today"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("cases: []", "non-empty list"),
        ("nothing: here", "non-empty list"),
        ("- just\n- a\n- list", "mapping with a 'cases' key"),
        (
            "cases:\n  - id: x\n    transcript: t\n    language: en\n    tool: delegate\n"
            "    intent: i",
            "unknown tool",
        ),
        (
            "cases:\n  - id: x\n    transcript: t\n    language: en\n    tool: none\n"
            "    intent: i\n"
            "  - id: x\n    transcript: u\n    language: en\n    tool: none\n    intent: j",
            "duplicate case id",
        ),
        (
            "cases:\n  - id: x\n    transcript: ''\n    language: en\n    tool: none\n"
            "    intent: i",
            "'transcript' is required",
        ),
    ],
)
def test_a_malformed_set_names_what_is_wrong(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ToolDatasetError, match=message):
        load_tool_cases(_write(tmp_path, body))


def test_a_missing_file_is_a_dataset_error_not_an_os_error(tmp_path: Path) -> None:
    with pytest.raises(ToolDatasetError, match="cannot read"):
        load_tool_cases(tmp_path / "absent.yaml")
