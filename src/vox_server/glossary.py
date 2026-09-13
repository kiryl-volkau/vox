from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Glossary:
    """Spoken form -> canonical engineering term vocabulary.

    Entries are hints only. The canonical terms bias Whisper through its initial prompt and
    the whole mapping is rendered into the LLM prompt as vocabulary context; nothing here
    ever performs string replacement on a transcript, so an entry can never corrupt a
    sentence that happens to contain the same syllables.

    ``entries`` preserves source order and holds no duplicate spoken forms (compared
    case-insensitively). An empty Glossary is falsy.
    """

    entries: tuple[tuple[str, str], ...] = ()

    @classmethod
    def load(cls, path: Path) -> Glossary:
        """Read a YAML glossary from ``path``.

        Accepts either a top-level mapping of spoken form to canonical term, or that
        mapping under a top-level ``terms`` key. A missing, unreadable or malformed file
        logs a warning and yields an empty Glossary: a bad glossary must never stop the
        server from starting. Never raises.
        """
        if not path.is_file():
            _log.warning("Glossary file %s not found; continuing without a glossary", path)
            return cls()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            _log.warning("Glossary file %s could not be parsed (%s); ignoring it", path, exc)
            return cls()
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            _log.warning("Glossary file %s is not a mapping; ignoring it", path)
            return cls()
        section = raw.get("terms")
        glossary = cls.from_mapping(section if isinstance(section, Mapping) else raw)
        _log.info("Loaded %d glossary entries from %s", len(glossary), path)
        return glossary

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, str]) -> Glossary:
        """Build a Glossary from an in-memory mapping.

        Non-string keys or values, blank keys or values, and repeated spoken forms
        (case-insensitive) are skipped. Surrounding whitespace is trimmed.
        """
        entries: list[tuple[str, str]] = []
        seen: set[str] = set()
        for key, value in mapping.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            spoken = key.strip()
            canonical = value.strip()
            if not spoken or not canonical:
                continue
            folded = spoken.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            entries.append((spoken, canonical))
        return cls(entries=tuple(entries))

    def as_prompt_block(self) -> str:
        """Return one ``spoken -> canonical`` line per entry, no trailing newline.

        Returns "" when the glossary is empty.
        """
        return "\n".join(f"{spoken} -> {canonical}" for spoken, canonical in self.entries)

    def as_stt_prompt(self) -> str:
        """Return the unique canonical terms joined by ", " for Whisper's initial prompt.

        Order follows ``entries``; duplicates are dropped. Returns "" when empty.
        """
        terms: list[str] = []
        seen: set[str] = set()
        for _, canonical in self.entries:
            if canonical in seen:
                continue
            seen.add(canonical)
            terms.append(canonical)
        return ", ".join(terms)

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)
