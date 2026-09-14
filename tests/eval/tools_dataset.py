"""Loading and validating the Claude Code tool evaluation set.

Separate from the test that runs it, like the term-recovery loader, so a typo in the set is
reported as a broken dataset rather than as a model that answered wrongly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vox_server.analysis import TOOLS

TOOLS_PATH = Path(__file__).with_name("tools.yaml")


class ToolDatasetError(ValueError):
    """The set is malformed, which is a bug in the set rather than in the model."""


@dataclass(frozen=True, slots=True)
class ToolCase:
    """One transcript and the single affordance it is supposed to call for."""

    id: str
    transcript: str
    language: str
    tool: str
    intent: str
    known_gap: str = ""


def _text(entry: Mapping[str, Any], key: str, where: str, *, required: bool = False) -> str:
    value = entry.get(key, "")
    if not isinstance(value, str):
        raise ToolDatasetError(f"{where}: '{key}' must be a string")
    stripped = value.strip()
    if required and not stripped:
        raise ToolDatasetError(f"{where}: '{key}' is required")
    return stripped


def load_tool_cases(path: Path | None = None) -> tuple[ToolCase, ...]:
    """Read the set, or raise ToolDatasetError naming the case that is wrong.

    Every case is checked to name a tool the code actually knows, and ids are checked to be
    unique: two cases sharing an id would silently overwrite each other's answer.
    """
    source = TOOLS_PATH if path is None else path
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ToolDatasetError(f"{source}: cannot read the tool evaluation set: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ToolDatasetError(f"{source}: the file must be a mapping with a 'cases' key")
    entries = raw.get("cases")
    if not isinstance(entries, Sequence) or isinstance(entries, str) or not entries:
        raise ToolDatasetError(f"{source}: 'cases' must be a non-empty list")

    cases: list[ToolCase] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"{source}: case {index}"
        if not isinstance(entry, Mapping):
            raise ToolDatasetError(f"{where}: each case must be a mapping")
        case_id = _text(entry, "id", where, required=True)
        if case_id in seen:
            raise ToolDatasetError(f"{source}: duplicate case id {case_id!r}")
        seen.add(case_id)
        where = f"{source}: case {case_id!r}"
        tool = _text(entry, "tool", where, required=True)
        if tool not in TOOLS:
            raise ToolDatasetError(f"{where}: unknown tool {tool!r}; expected one of {TOOLS}")
        cases.append(
            ToolCase(
                id=case_id,
                transcript=_text(entry, "transcript", where, required=True),
                language=_text(entry, "language", where, required=True),
                tool=tool,
                intent=_text(entry, "intent", where, required=True),
                known_gap=_text(entry, "known_gap", where),
            )
        )
    return tuple(cases)
