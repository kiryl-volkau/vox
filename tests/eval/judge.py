"""An LLM grading whether a rewritten message says what was actually said.

Term matching catches the mechanical half of the question - did "промытоты" become "prompt".
It cannot catch the half that matters as much: whether the sentence the term landed in still
means what the speaker meant, or whether a plausible-looking word was swapped in that makes
the request read as something else. That judgement is what this asks a model for.

The verdict is advisory by design. A local judge is itself a small model and will disagree
with itself across runs, so the suite holds it to an aggregate pass rate rather than failing
a build on one harsh answer; the deterministic checks are what fail per case.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from vox_server.llm import OpenAICompatibleClient, clean_llm_output

# The suite's autouse fixture clears LLM_* from the environment so a developer's .env cannot
# steer the unit tests. That runs before any eval test body, so the fallback is snapshotted
# here at import time; VOX_EVAL_* names are outside what the fixture touches and win when set.
_LLM_ENV_AT_IMPORT = {
    "base_url": os.environ.get("LLM_BASE_URL", "http://127.0.0.1:11434/v1"),
    "model": os.environ.get("LLM_MODEL", "qwen2.5:7b-instruct"),
    "api_key": os.environ.get("LLM_API_KEY", "local"),
}

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

SYSTEM_PROMPT = """\
You grade one output of a speech-to-task rewriter, and you answer with JSON only.

The text you are grading is Russian, and the JSON you answer with is English. Write "reason"
in English whatever language the graded text is in - a reason nobody on the team reads is the
same as no reason at all.

The rewriter is given a raw speech-recognition transcript of a developer talking. Russian
speech with English engineering terms in it, often with a term mangled by the recogniser into
a near-homophone that means nothing. The rewriter must tidy the speech into a written message
and restore any mangled term to the real engineering word the context implies. It must not add
anything that was not said.

You are told what the speaker actually wanted. Judge the output against that intent.

Answer with this JSON object and nothing else:

{
  "terms_sensible": true or false,
  "faithful": true or false,
  "invented": true or false,
  "reason": "one short sentence"
}

terms_sensible - every engineering term in the output is a real term that makes sense in this
  sentence. False if a word is still nonsense, or if a mangled word was replaced by a term that
  does not fit what is being described.
faithful - the output means what the speaker meant. False if a request became a statement, a
  statement became a request, an uncertainty became a fact, a negation or a limit was dropped,
  or something the speaker took back is still there.
invented - the output contains a requirement, task or entity the speaker never mentioned.
  Judge only additions, not wording.
"""

USER_TEMPLATE = """\
What the speaker wanted: {intent}

Transcript the rewriter was given:
{transcript}

Output the rewriter produced:
{output}
"""


@dataclass(frozen=True, slots=True)
class Verdict:
    """One judge answer. ``parsed`` is False when the model returned nothing usable."""

    terms_sensible: bool
    faithful: bool
    invented: bool
    reason: str
    parsed: bool = True

    @property
    def passed(self) -> bool:
        return self.terms_sensible and self.faithful and not self.invented

    def describe(self) -> str:
        if self.passed:
            return "ok"
        if not self.parsed:
            return f"unparseable judge answer: {self.reason}"
        failed = [
            name
            for name, ok in (
                ("terms_sensible", self.terms_sensible),
                ("faithful", self.faithful),
                ("not invented", not self.invented),
            )
            if not ok
        ]
        return f"{', '.join(failed)} - {self.reason}"


def judge_config() -> dict[str, str]:
    """The endpoint the judge talks to: VOX_EVAL_* when set, else the backend's own LLM."""
    return {
        key: os.environ.get(f"VOX_EVAL_LLM_{key.upper()}", fallback)
        for key, fallback in _LLM_ENV_AT_IMPORT.items()
    }


def build_judge(timeout_seconds: float = 120.0) -> OpenAICompatibleClient:
    """Build the judge client. The caller owns it and must ``aclose`` it."""
    config = judge_config()
    return OpenAICompatibleClient(
        base_url=config["base_url"],
        model=config["model"],
        api_key=config["api_key"],
        timeout_seconds=timeout_seconds,
        # Grading is a classification, not a composition: sample it greedily so a rerun of the
        # same suite moves because the rewriter moved, not because the judge did.
        temperature=0.0,
        max_tokens=300,
    )


async def grade(
    judge: OpenAICompatibleClient, *, intent: str, transcript: str, output: str
) -> Verdict:
    """Ask ``judge`` to grade one output. Never raises on a bad answer; see ``Verdict.parsed``."""
    user = USER_TEMPLATE.format(intent=intent, transcript=transcript, output=output)
    raw = await judge.chat(SYSTEM_PROMPT, user, temperature=0.0)
    return parse_verdict(raw)


def parse_verdict(raw: str) -> Verdict:
    """Parse a judge answer, tolerating the prose and fences a small model wraps JSON in."""
    cleaned = clean_llm_output(raw).strip()
    match = _JSON_OBJECT.search(cleaned)
    if match is None:
        return _unparseable(cleaned)
    try:
        body = json.loads(match.group(0))
    except json.JSONDecodeError:
        return _unparseable(cleaned)
    if not isinstance(body, dict):
        return _unparseable(cleaned)
    return Verdict(
        terms_sensible=_flag(body, "terms_sensible"),
        faithful=_flag(body, "faithful"),
        invented=_flag(body, "invented", default=False),
        reason=str(body.get("reason", "")).strip()[:300],
    )


def _flag(body: dict[str, Any], name: str, *, default: bool = False) -> bool:
    value = body.get(name, default)
    if isinstance(value, bool):
        return value
    # A small model answers "true" as often as true; refusing that would grade the judge's
    # JSON rather than the rewriter.
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "yes", "1"}
    return default


def _unparseable(cleaned: str) -> Verdict:
    return Verdict(
        terms_sensible=False,
        faithful=False,
        invented=False,
        reason=cleaned[:200] or "(empty)",
        parsed=False,
    )
