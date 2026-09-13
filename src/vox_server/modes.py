from __future__ import annotations

import builtins
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .models import ModeInfo

logger = logging.getLogger(__name__)

_FRONT_MATTER_DELIMITER = "---"
_SECTION_HEADERS = {"## SYSTEM": "system", "## USER": "user", "## WRAPPER": "wrapper"}
_DEFAULT_USER_TEMPLATE = "{transcript}"


class ModeError(RuntimeError):
    """A mode file is missing, unreadable or internally inconsistent."""


class UnknownModeError(ModeError):
    """A mode was requested by a name that is not registered.

    ``name`` is the name exactly as requested (not normalised); ``available`` lists the
    registered mode names, sorted, so the caller can show them to the user.
    """

    def __init__(self, name: str, available: list[str]) -> None:
        super().__init__(f"unknown mode {name!r}; available: {', '.join(available) or 'none'}")
        self.name = name
        self.available = available


@dataclass(frozen=True, slots=True)
class Mode:
    """One prompt mode, parsed from ``modes/<name>.md``.

    ``temperature`` is ``None`` when the mode does not override the server default.
    ``user_template`` and ``wrapper_template`` are raw text holding the literal placeholders
    ``{transcript}``/``{glossary}`` and ``{normalized}``; they are filled by ``str.replace``,
    never ``str.format``, because prompt text legitimately contains braces.
    """

    name: str
    label: str
    description: str
    requires_llm: bool
    wrap_for_claude: bool
    temperature: float | None
    fallback_to_transcript: bool
    system_prompt: str
    user_template: str
    wrapper_template: str

    def render_user(self, transcript: str, glossary: str) -> str:
        """Return the user message with ``{transcript}`` and ``{glossary}`` substituted.

        Placeholders the template does not contain are simply not substituted, and any other
        brace-delimited text is left exactly as written.
        """
        return self.user_template.replace("{transcript}", transcript).replace(
            "{glossary}", glossary
        )

    def render_wrapper(self, normalized: str) -> str:
        """Return the text to deliver to the client for this mode.

        Returns ``normalized`` unchanged when the mode does not wrap for Claude or has no
        wrapper text; otherwise substitutes ``{normalized}`` into the wrapper.
        """
        if not self.wrap_for_claude or not self.wrapper_template:
            return normalized
        return self.wrapper_template.replace("{normalized}", normalized)

    def to_info(self) -> ModeInfo:
        """Return the client-facing description of this mode."""
        return ModeInfo(
            name=self.name,
            label=self.label,
            description=self.description,
            requires_llm=self.requires_llm,
            wrap_for_claude=self.wrap_for_claude,
        )


class ModeRegistry:
    """Every mode available to the server, addressed by name, case-insensitively."""

    def __init__(self, modes: Iterable[Mode]) -> None:
        by_name: dict[str, Mode] = {}
        for mode in sorted(modes, key=lambda item: item.name):
            key = mode.name.casefold()
            if key in by_name:
                raise ModeError(f"duplicate mode name: {mode.name!r}")
            by_name[key] = mode
        self._by_name = by_name

    @classmethod
    def load(cls, modes_dir: Path) -> ModeRegistry:
        """Read every ``*.md`` file in ``modes_dir``, sorted by filename.

        Raises ModeError when the directory is missing, when a file is malformed, or when the
        directory yields no modes. Adding a mode file requires no code change.
        """
        if not modes_dir.is_dir():
            raise ModeError(f"modes directory not found: {modes_dir}")
        registry = cls(_parse_mode_file(path) for path in sorted(modes_dir.glob("*.md")))
        if len(registry) == 0:
            raise ModeError(f"no mode files (*.md) in {modes_dir}")
        logger.info("loaded %d modes from %s: %s", len(registry), modes_dir, registry.names())
        return registry

    def get(self, name: str) -> Mode:
        """Return the mode called ``name``, ignoring case and surrounding whitespace.

        Raises UnknownModeError when no such mode is registered.
        """
        mode = self._by_name.get(name.strip().casefold())
        if mode is None:
            raise UnknownModeError(name, self.names())
        return mode

    def list(self) -> builtins.list[Mode]:
        """Return every mode, sorted by name."""
        return builtins.list(self._by_name.values())

    def names(self) -> builtins.list[str]:
        """Return every mode name, sorted."""
        return [mode.name for mode in self._by_name.values()]

    def __len__(self) -> int:
        return len(self._by_name)


def _parse_mode_file(path: Path) -> Mode:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ModeError(f"{path}: cannot read mode file: {exc}") from exc

    front_matter, body = _split_front_matter(text, path)
    sections = _split_sections(body)

    name = _str_field(front_matter, "name", path.stem, path).strip() or path.stem
    label = _str_field(front_matter, "label", name.title(), path)
    description = _str_field(front_matter, "description", "", path)
    requires_llm = _bool_field(front_matter, "requires_llm", True, path)
    wrap_for_claude = _bool_field(front_matter, "wrap_for_claude", False, path)
    fallback_to_transcript = _bool_field(front_matter, "fallback_to_transcript", False, path)
    temperature = _temperature_field(front_matter, path)

    system_prompt = sections.get("system", "")
    user_template = sections.get("user", "") or _DEFAULT_USER_TEMPLATE
    wrapper_template = sections.get("wrapper", "")

    if requires_llm and not system_prompt:
        raise ModeError(
            f"{path}: mode {name!r} requires an LLM but has an empty '## SYSTEM' section"
        )
    if wrap_for_claude and not wrapper_template:
        raise ModeError(
            f"{path}: mode {name!r} sets wrap_for_claude but has an empty '## WRAPPER' section"
        )

    return Mode(
        name=name,
        label=label,
        description=description,
        requires_llm=requires_llm,
        wrap_for_claude=wrap_for_claude,
        temperature=temperature,
        fallback_to_transcript=fallback_to_transcript,
        system_prompt=system_prompt,
        user_template=user_template,
        wrapper_template=wrapper_template,
    )


def _split_front_matter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONT_MATTER_DELIMITER:
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONT_MATTER_DELIMITER:
            block = "\n".join(lines[1:index])
            return _parse_front_matter(block, path), "\n".join(lines[index + 1 :])
    raise ModeError(f"{path}: front matter starts with '---' but is never closed")


def _parse_front_matter(block: str, path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise ModeError(f"{path}: invalid YAML front matter: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ModeError(f"{path}: front matter must be a mapping, got {type(data).__name__}")
    return {str(key): value for key, value in data.items()}


def _split_sections(body: str) -> dict[str, str]:
    collected: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in body.splitlines():
        section = _SECTION_HEADERS.get(line.strip())
        if section is not None:
            current = collected.setdefault(section, [])
            continue
        if current is not None:
            current.append(line)
    return {section: "\n".join(lines).strip() for section, lines in collected.items()}


def _str_field(data: Mapping[str, Any], key: str, default: str, path: Path) -> str:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ModeError(
            f"{path}: front matter {key!r} must be a string, got {type(value).__name__}"
        )
    return value


def _bool_field(data: Mapping[str, Any], key: str, default: bool, path: Path) -> bool:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ModeError(f"{path}: front matter {key!r} must be true or false")
    return value


def _temperature_field(data: Mapping[str, Any], path: Path) -> float | None:
    value = data.get("temperature")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ModeError(f"{path}: front matter 'temperature' must be a number")
    return float(value)
