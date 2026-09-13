"""Locating the per-project ``.vox.md`` whose text steers the model for one repository.

The companion never reads a repository: it reads exactly one file, the one the user put in
the root of the project being talked about, and sends its text to the backend. Which project
that is comes from the title of the window that was focused when recording started.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_FILE_NAME = ".vox.md"
MAX_PROJECT_BYTES = 8000


def resolve_project(
    roots: Sequence[str | Path],
    window_title: str,
    file_name: str = PROJECT_FILE_NAME,
    *,
    max_bytes: int = MAX_PROJECT_BYTES,
    detect_from_window: bool = True,
) -> tuple[str | None, str | None]:
    """Return ``(project name, project file text)`` for the project ``window_title`` names.

    A root matches when its folder name occurs case-insensitively in ``window_title``, which
    is how IntelliJ and most editors title their windows; the longest matching folder name
    wins, so a root named ``vox`` cannot shadow one named ``vox-client``. The text is
    truncated to ``max_bytes`` UTF-8 bytes, on a line boundary when the truncated part
    contains one, because the backend charges a full prefill for anything oversized.

    Returns ``(None, None)`` when detection is off, ``roots`` is empty, no root matches, the
    file is absent, empty, or cannot be read or decoded. Never raises: a project file is an
    optimisation, and no failure to find one may cost the user a request.

    The window title is an argument rather than a Win32 call, so this is testable, and the
    only side effect is reading that one file.
    """
    if not detect_from_window or not window_title:
        return None, None
    root = _match(roots, window_title)
    if root is None:
        return None, None
    path = root / file_name
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.debug("no %s in %s", file_name, root)
        return None, None
    except (OSError, ValueError) as exc:
        logger.warning("cannot read %s: %s", path, exc)
        return None, None
    if not text.strip():
        return None, None
    return root.name, _truncate(text, max_bytes, path)


def _match(roots: Sequence[str | Path], window_title: str) -> Path | None:
    lowered = window_title.lower()
    best: Path | None = None
    for entry in roots:
        root = Path(entry)
        name = root.name
        if not name or name.lower() not in lowered:
            continue
        if best is None or len(name) > len(best.name):
            best = root
    return best


def _truncate(text: str, max_bytes: int, path: Path) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    logger.warning("%s is %d bytes, sending only the first %d", path, len(encoded), max_bytes)
    # errors="ignore" drops the partial character the byte cut may have left behind.
    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
    boundary = clipped.rfind("\n")
    return clipped[:boundary] if boundary > 0 else clipped
