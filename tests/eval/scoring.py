"""Deciding whether one rewritten message carries the terms it was supposed to.

Kept free of I/O and of the LLM so the eval harness can itself be tested offline: a scorer
nobody checks turns a red evaluation into a shrug. ``tests/unit/test_eval_scoring.py`` is
that check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# An acronym is anchored at both ends: an open-ended "PR" would be satisfied by "prepare",
# and a case that passes on a word it never meant is worse than no case at all. Everything
# else is anchored only at the start, so "prompt" is satisfied by "prompts" - the answer is
# a sentence, and a term legitimately carries an inflection or a plural.
_ACRONYM = re.compile(r"^[A-Z0-9]{2,5}$")


def _pattern(term: str) -> re.Pattern[str]:
    words = [re.escape(word) for word in term.split()]
    body = r"[\s\-]+".join(words)
    tail = r"\b" if _ACRONYM.match(term) else ""
    return re.compile(rf"\b{body}{tail}", re.IGNORECASE)


def contains_term(text: str, term: str) -> bool:
    """True when ``text`` carries ``term`` as a word rather than as a fragment.

    Matching is case-insensitive and tolerant of the whitespace or hyphen between the words
    of a multi-word term, so "Spring Boot" is found in "spring-boot". A term written in
    capitals is treated as an acronym and must match a whole word; any other term may carry a
    suffix, so "index" is found in "indexes".
    """
    return _pattern(term).search(text) is not None


def contains_garbled(text: str, garbled: str) -> bool:
    """True when the recogniser's nonsense token survived into ``text``.

    A plain case-insensitive substring test on purpose: the token is not a word anyone meant,
    so any trace of it at all is a failure, inflected or not. An empty ``garbled`` - a case
    that tests something other than term recovery - is never found.
    """
    needle = garbled.strip()
    return bool(needle) and needle.casefold() in text.casefold()


def suggest_correction(output: str, *, garbled: str, expect: tuple[str, ...]) -> str | None:
    """Return ``output`` with the surviving mangled token swapped for the term it should be.

    The point of a failing case is not that a string comparison went red, it is that a word in
    the message means nothing; showing the sentence as it should have read says that in one
    line. The replacement is the first expected term the output is missing, falling back to
    the first expected term. Returns None when the token did not survive or there is nothing
    to put in its place, which is the caller's signal that there is no correction to show.
    """
    if not contains_garbled(output, garbled) or not expect:
        return None
    missing = [term for term in expect if not contains_term(output, term)]
    replacement = missing[0] if missing else expect[0]
    return re.sub(re.escape(garbled.strip()), replacement, output, flags=re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CaseScore:
    """What one output got right, in the terms the case asked about.

    ``passed`` is the conjunction of all three checks; the lists say which term caused a
    failure, so a red run names the word rather than just the case.
    """

    case_id: str
    garbled_survived: str | None
    missing: tuple[str, ...]
    invented: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.garbled_survived is None and not self.missing and not self.invented

    def describe(self) -> str:
        """A one-line reason the case failed, or "ok" when it did not."""
        if self.passed:
            return "ok"
        reasons = []
        if self.garbled_survived is not None:
            reasons.append(f"garbled {self.garbled_survived!r} survived")
        if self.missing:
            reasons.append(f"missing {', '.join(self.missing)}")
        if self.invented:
            reasons.append(f"invented {', '.join(self.invented)}")
        return "; ".join(reasons)


def score(
    case_id: str,
    output: str,
    *,
    garbled: str = "",
    expect: tuple[str, ...] = (),
    forbid: tuple[str, ...] = (),
) -> CaseScore:
    """Score one rewritten message against what its case expected."""
    return CaseScore(
        case_id=case_id,
        garbled_survived=garbled if contains_garbled(output, garbled) else None,
        missing=tuple(term for term in expect if not contains_term(output, term)),
        invented=tuple(term for term in forbid if contains_term(output, term)),
    )
