"""The LLM settings a client may change while the server is running.

``.env`` is the boot default; this file is the override that wins over it, so pointing the
backend at a different model does not mean editing a file on the host and restarting a
container. It holds the API key, which is why it lives in the state directory rather than
anywhere the repository can reach, and why it is written with owner-only permissions where
the platform has them.

Nothing here ever raises on read: a state file that is corrupt, unreadable or written by a
newer build must degrade to "no override" and let the server start on its ``.env`` defaults.
"""

from __future__ import annotations

import json
import logging
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)

FILE_NAME = "llm.json"


@dataclass(frozen=True, slots=True)
class LlmOverride:
    """A partial LLM configuration. ``None`` means "leave whatever ``.env`` said"."""

    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    warmup: bool | None = None

    def __bool__(self) -> bool:
        return any(
            value is not None for value in (self.base_url, self.model, self.api_key, self.warmup)
        )

    def merge(self, other: LlmOverride) -> LlmOverride:
        """Return this override with every field ``other`` states replaced by it."""
        return LlmOverride(
            base_url=other.base_url if other.base_url is not None else self.base_url,
            model=other.model if other.model is not None else self.model,
            api_key=other.api_key if other.api_key is not None else self.api_key,
            warmup=other.warmup if other.warmup is not None else self.warmup,
        )

    def applied_to(self, settings: Settings) -> EffectiveLlm:
        """Resolve this override against ``settings`` into the values actually used."""
        return EffectiveLlm(
            base_url=self.base_url or settings.llm_base_url,
            model=self.model or settings.llm_model,
            api_key=(
                self.api_key
                if self.api_key is not None
                else settings.llm_api_key.get_secret_value()
            ),
            warmup=settings.llm_warmup if self.warmup is None else self.warmup,
            overridden=bool(self),
        )


@dataclass(frozen=True, slots=True)
class EffectiveLlm:
    """What the server is actually talking to, after the override is applied."""

    base_url: str
    model: str
    api_key: str
    warmup: bool
    overridden: bool


def override_path(settings: Settings) -> Path:
    return settings.state_dir / FILE_NAME


def load(settings: Settings) -> LlmOverride:
    """Read the stored override, or an empty one when there is none. Never raises."""
    path = override_path(settings)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LlmOverride()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("ignoring the LLM override at %s (%s: %s)", path, type(exc).__name__, exc)
        return LlmOverride()
    if not isinstance(body, dict):
        logger.warning("ignoring the LLM override at %s: not an object", path)
        return LlmOverride()
    return LlmOverride(
        base_url=_text(body, "base_url"),
        model=_text(body, "model"),
        api_key=_text(body, "api_key", allow_empty=True),
        warmup=_flag(body, "warmup"),
    )


def save(settings: Settings, override: LlmOverride) -> Path | None:
    """Write ``override`` to the state directory, returning the file or None on failure.

    Never raises: a backend that cannot persist a setting must still apply it for this run
    and say so, rather than refusing a request it has already honoured in memory.
    """
    path = override_path(settings)
    body = {
        key: value
        for key, value in (
            ("base_url", override.base_url),
            ("model", override.model),
            ("api_key", override.api_key),
            ("warmup", override.warmup),
        )
        if value is not None
    }
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(body, indent=2), encoding="utf-8")
        _restrict(temporary)
        temporary.replace(path)
    except OSError as exc:
        logger.warning("could not persist the LLM override to %s (%s)", path, exc)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            logger.debug("leftover partial override %s could not be removed", temporary)
        return None
    return path


def clear(settings: Settings) -> None:
    """Remove the stored override so the next start falls back to ``.env``. Never raises."""
    try:
        override_path(settings).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not remove the LLM override (%s)", exc)


def _restrict(path: Path) -> None:
    # The file holds an API key. Windows ignores POSIX modes, so this is best effort rather
    # than a guarantee - the real boundary is the state directory, which is not shared.
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        logger.debug("could not restrict permissions on %s (%s)", path, exc)


def _text(body: dict[str, Any], name: str, *, allow_empty: bool = False) -> str | None:
    value = body.get(name)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped and not allow_empty:
        return None
    return stripped


def _flag(body: dict[str, Any], name: str) -> bool | None:
    value = body.get(name)
    return value if isinstance(value, bool) else None
