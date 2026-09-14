from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .languages import DEFAULT_LANGUAGE

logger = logging.getLogger(__name__)

_SECTION_HEADERS = {"## SYSTEM": "system", "## USER": "user"}
_LANGUAGE_HEADER = re.compile(r"^##\s+LANGUAGE\s+([A-Za-z][A-Za-z0-9_-]*)\s*$")
_DEFAULT_USER_TEMPLATE = "{transcript}"
_PROJECT_FILE_SYSTEM_HEADER = re.compile(r"^##\s+SYSTEM\s*$", re.MULTILINE)
_ANY_SECTION_HEADER = re.compile(r"^##\s+\S", re.MULTILINE)

# Each injected block is delimited by a tag rather than a prose header, so the system prompt
# reads the same whatever language it asks the model to answer in.
_INSTRUCTIONS_PLACEHOLDER = "{instructions}"
_PROJECT_PLACEHOLDER = "{project}"
_CONTEXT_PLACEHOLDER = "{context}"
_LANGUAGE_PLACEHOLDER = "{language}"

_INSTRUCTIONS_TAG = "project_instructions"
_PROJECT_TAG = "project_context"
_CONTEXT_TAG = "conversation"

# Matched in one pass so that text injected for one placeholder is never rescanned for the
# next: a project file is free to mention "{project}" without it being expanded.
_PLACEHOLDERS = re.compile(
    "|".join(
        re.escape(placeholder)
        for placeholder in (
            _INSTRUCTIONS_PLACEHOLDER,
            _PROJECT_PLACEHOLDER,
            _CONTEXT_PLACEHOLDER,
            _LANGUAGE_PLACEHOLDER,
        )
    )
)


class PromptError(RuntimeError):
    """The prompt file is missing, unreadable or has no system section."""


@dataclass(frozen=True, slots=True)
class ProjectFile:
    """A caller's ``.vox.md`` split into the two roles it can play.

    ``instructions`` is its ``## SYSTEM`` section, which says how the answer should be
    shaped and is injected as system-prompt text on top of the built-in prompt.
    ``context`` is everything else: the repository's vocabulary, constraints and examples.
    Either may be empty.
    """

    instructions: str = ""
    context: str = ""

    def __bool__(self) -> bool:
        return bool(self.instructions or self.context)


def split_project_file(text: str | None) -> ProjectFile:
    """Split a ``.vox.md`` into its ``## SYSTEM`` instructions and its context.

    A file with no ``## SYSTEM`` header is all context, which is what every file written
    before the header existed is. The section ends at the next ``##`` header, so a file may
    put instructions first and context after. Blank input yields an empty ProjectFile.
    """
    if not text or not text.strip():
        return ProjectFile()
    match = _PROJECT_FILE_SYSTEM_HEADER.search(text)
    if match is None:
        return ProjectFile(context=text.strip())
    before = text[: match.start()]
    rest = text[match.end() :]
    following = _ANY_SECTION_HEADER.search(rest)
    instructions = rest if following is None else rest[: following.start()]
    after = "" if following is None else rest[following.start() :]
    context = "\n".join(part.strip() for part in (before, after) if part.strip())
    return ProjectFile(instructions=instructions.strip(), context=context.strip())


@dataclass(frozen=True, slots=True)
class Prompt:
    """One prompt file: the system prompt, the user template and the language blocks.

    ``system_prompt`` and ``user_template`` are raw text holding the literal placeholders
    ``{instructions}``, ``{project}``, ``{context}``, ``{language}``, ``{transcript}`` and
    ``{glossary}``; they are filled by ``str.replace``, never ``str.format``, because prompt
    text legitimately contains braces.

    ``language_blocks`` maps a language code to the paragraph that tells the model which
    language to answer in, so adding a language is an edit to the prompt file and not to
    this module.
    """

    system_prompt: str
    user_template: str
    language_blocks: tuple[tuple[str, str], ...] = ()

    def render_system(
        self, project: ProjectFile, conversation: str = "", language: str = DEFAULT_LANGUAGE
    ) -> str:
        """Return the system prompt with every context placeholder substituted.

        ``project`` is the caller's already-truncated ``.vox.md``; its ``## SYSTEM`` section
        and its context go into separate delimited blocks. ``conversation`` is the recent
        Claude Code exchange the caller chose to send. Each block is omitted entirely when
        its text is blank, so no delimiter is ever left dangling. ``language`` selects the
        language block, falling back to the default language and then to nothing.
        Placeholders the system prompt does not contain are simply not substituted, and a
        placeholder occurring inside the injected text is left alone: substitution is a
        single pass, so a ``.vox.md`` that documents ``{project}`` cannot make the project
        block appear a second time inside itself.
        """
        replacements = {
            _INSTRUCTIONS_PLACEHOLDER: _block(_INSTRUCTIONS_TAG, project.instructions),
            _PROJECT_PLACEHOLDER: _block(_PROJECT_TAG, project.context),
            _CONTEXT_PLACEHOLDER: _block(_CONTEXT_TAG, conversation),
            _LANGUAGE_PLACEHOLDER: self.language_block(language),
        }
        return _PLACEHOLDERS.sub(lambda match: replacements[match.group(0)], self.system_prompt)

    def render_user(self, transcript: str, glossary: str) -> str:
        """Return the user message with ``{transcript}`` and ``{glossary}`` substituted.

        Placeholders the template does not contain are simply not substituted, and any other
        brace-delimited text is left exactly as written.
        """
        return self.user_template.replace("{transcript}", transcript).replace(
            "{glossary}", glossary
        )

    def language_block(self, language: str) -> str:
        """Return the instruction paragraph for ``language``.

        Falls back to the default language's block, then to "" when the prompt file declares
        no language sections at all.
        """
        blocks = dict(self.language_blocks)
        return blocks.get(language) or blocks.get(DEFAULT_LANGUAGE, "")


def load_prompt(path: Path) -> Prompt:
    """Read a prompt file: markdown with ``## SYSTEM``, ``## USER`` and ``## LANGUAGE xx``.

    A missing ``## USER`` section means the transcript alone is the user message; missing
    language sections mean ``{language}`` renders empty. Raises PromptError when the file
    cannot be read or has no system section; editing the prompt requires no code change, so
    a broken file has to fail loudly at startup.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise PromptError(f"{path}: cannot read the prompt file: {exc}") from exc

    sections, languages = _split_sections(text)
    system_prompt = sections.get("system", "")
    if not system_prompt:
        raise PromptError(f"{path}: the prompt file has no '## SYSTEM' section")

    prompt = Prompt(
        system_prompt=system_prompt,
        user_template=sections.get("user", "") or _DEFAULT_USER_TEMPLATE,
        language_blocks=languages,
    )
    logger.info(
        "loaded the prompt from %s (system %d chars, user %d chars, languages: %s)",
        path,
        len(prompt.system_prompt),
        len(prompt.user_template),
        ", ".join(code for code, _ in prompt.language_blocks) or "none",
    )
    return prompt


def _block(tag: str, text: str) -> str:
    stripped = text.strip()
    return f"\n<{tag}>\n{stripped}\n</{tag}>\n" if stripped else ""


def _split_sections(body: str) -> tuple[dict[str, str], tuple[tuple[str, str], ...]]:
    collected: dict[str, list[str]] = {}
    languages: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in body.splitlines():
        stripped = line.strip()
        section = _SECTION_HEADERS.get(stripped)
        if section is not None:
            current = collected.setdefault(section, [])
            continue
        language = _LANGUAGE_HEADER.match(stripped)
        if language is not None:
            current = languages.setdefault(language.group(1).casefold(), [])
            continue
        if current is not None:
            current.append(line)
    sections = {section: "\n".join(lines).strip() for section, lines in collected.items()}
    blocks = tuple((code, "\n".join(lines).strip()) for code, lines in languages.items())
    return sections, tuple((code, text) for code, text in blocks if text)
