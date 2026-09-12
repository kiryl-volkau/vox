from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import httpx

from voice_code_server.config import redacted_base_url

_log = logging.getLogger(__name__)


class LlmError(RuntimeError):
    """Base class for every failure of the OpenAI-compatible backend."""


class LlmUnavailableError(LlmError):
    """The LLM endpoint could not be reached."""


class LlmTimeoutError(LlmError):
    """The LLM did not answer within the configured timeout."""


class LlmResponseError(LlmError):
    """The LLM answered, but with an error status or an unusable payload."""


_THINK_OPEN = re.compile(r"^<think\b[^>]*>", re.IGNORECASE)
_THINK_CLOSE = "</think>"
_FENCE_INFO = re.compile(r"[A-Za-z0-9_+#.-]{0,24}")
_PREAMBLE = re.compile(
    r"^(?:"
    r"here\s+is|here['\u2019]s|here\s+are|certainly|sure|of\s+course|okay|ok|"
    r"result|output|answer|response|prompt|"
    r"final\s+(?:prompt|answer|result|version|text)|"
    r"вот|конечно|"
    r"готово|готовый|"
    r"итоговый|итог|"
    r"результат|ответ"
    r")\b",
    re.IGNORECASE,
)
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("«", "»"))


def _strip_think(text: str) -> str:
    lowered = text.lower()
    opening = _THINK_OPEN.match(text)
    if opening is not None:
        close = lowered.find(_THINK_CLOSE, opening.end())
        if close == -1:
            return text[opening.end() :]
        return text[close + len(_THINK_CLOSE) :]
    # Chat templates for reasoning models often pre-fill the opening tag, so the reply starts
    # mid-reasoning and only the closing tag reaches us.
    if "<think" not in lowered:
        close = lowered.find(_THINK_CLOSE)
        if close != -1:
            return text[close + len(_THINK_CLOSE) :]
    return text


def _strip_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    if _FENCE_INFO.fullmatch(lines[0][3:].strip()) is None:
        return text
    close = -1
    for index in range(len(lines) - 1, 0, -1):
        if lines[index].strip() == "```":
            close = index
            break
    if close == -1:
        return "\n".join(lines[1:])
    if any(line.strip() for line in lines[close + 1 :]):
        return text
    return "\n".join(lines[1:close])


def _strip_preamble(text: str) -> str:
    head, separator, tail = text.partition("\n")
    candidate = head.strip().lstrip("#*_ \t")
    if not candidate.rstrip("*_ \t").endswith(":"):
        return text
    if _PREAMBLE.match(candidate) is None:
        return text
    return tail if separator else ""


def _balanced(inner: str, opening: str, closing: str) -> bool:
    depth = 0
    for char in inner:
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _strip_quotes(text: str) -> str:
    if len(text) < 2:
        return text
    for opening, closing in _QUOTE_PAIRS:
        if not text.startswith(opening) or not text.endswith(closing):
            continue
        inner = text[1:-1]
        reopened = opening == closing and opening in inner
        unbalanced = opening != closing and not _balanced(inner, opening, closing)
        if reopened or unbalanced:
            continue
        return inner
    return text


def clean_llm_output(text: str) -> str:
    """Remove superficial wrapping junk from a model reply without rewriting it.

    Applied in order to a stripped copy: a leading ``<think>...</think>`` reasoning block; a
    fence wrapping the entire text (a markdown code fence with an optional language word,
    closed or not); one leading conversational preamble line that ends with ":" and reads as
    boilerplate ("Here is your prompt:", "Certainly:", "Результат:"); symmetric quotes
    wrapping the whole text; a final strip.

    Interior lines, punctuation and wording are never touched, and a line ending in ":" that
    is not boilerplate is kept. Empty or whitespace-only input returns "".
    """
    cleaned = text.strip()
    if not cleaned:
        return ""
    cleaned = _strip_think(cleaned).strip()
    cleaned = _strip_fence(cleaned).strip()
    cleaned = _strip_preamble(cleaned).strip()
    # A preamble line hides the opening fence from the pass above.
    cleaned = _strip_fence(cleaned).strip()
    return _strip_quotes(cleaned).strip()


def _message_content(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        raise LlmResponseError("LLM response was not a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmResponseError("LLM response contained no choices")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise LlmResponseError("LLM response choice was malformed")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise LlmResponseError("LLM response contained no message")
    content = message.get("content")
    if not isinstance(content, str):
        raise LlmResponseError("LLM response content was not a string")
    return content


class OpenAICompatibleClient:
    """Minimal async client for an OpenAI-compatible /v1 endpoint (Ollama, vLLM, llama.cpp).

    Only ``POST {base_url}/chat/completions`` and ``GET {base_url}/models`` are used. A
    trailing "/" on the base URL is ignored. The API key is sent as a bearer token when it is
    non-empty and never appears in an exception message or a log record.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_seconds: float,
        temperature: float,
        max_tokens: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._client = client if client is not None else httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    async def chat(self, system: str, user: str, *, temperature: float | None = None) -> str:
        """Run one non-streaming chat completion and return the raw assistant content.

        The system message is omitted when ``system`` is empty. ``temperature`` overrides the
        configured default for this call only. The returned string is exactly what the model
        produced - callers apply ``clean_llm_output`` themselves.

        Raises LlmTimeoutError on timeout, LlmUnavailableError when the endpoint is
        unreachable, and LlmResponseError on a non-2xx status or an unusable payload.
        """
        messages: list[dict[str, str]] = []
        if system.strip():
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": self._max_tokens,
            "stream": False,
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions", json=payload, headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise LlmTimeoutError(f"LLM did not respond within {self._timeout_seconds:g}s") from exc
        except httpx.RequestError as exc:
            raise LlmUnavailableError(
                f"LLM at {redacted_base_url(self._base_url)} is unreachable"
            ) from exc
        if not 200 <= response.status_code < 300:
            raise LlmResponseError(f"LLM returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise LlmResponseError("LLM returned a non-JSON response") from exc
        return _message_content(body)

    async def check(self) -> tuple[bool, str | None]:
        """Probe ``GET /models``. Returns (True, None) when reachable, else (False, reason).

        Never raises; the reason is a short string safe to put in a health response.
        """
        try:
            response = await self._client.get(f"{self._base_url}/models", headers=self._headers())
        except httpx.TimeoutException:
            return False, f"timeout after {self._timeout_seconds:g}s"
        except httpx.RequestError as exc:
            return False, f"unreachable ({type(exc).__name__})"
        except Exception as exc:
            _log.debug("LLM health probe failed", exc_info=exc)
            return False, f"probe failed ({type(exc).__name__})"
        if not 200 <= response.status_code < 300:
            return False, f"HTTP {response.status_code}"
        return True, None

    async def aclose(self) -> None:
        """Close the underlying HTTP client. Safe to call more than once."""
        await self._client.aclose()
