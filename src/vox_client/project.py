"""Locating the per-project ``.vox.md`` whose text steers the model for one repository.

The companion never reads a repository: it reads exactly one file, the one the user put in
the root of the project being talked about, and sends its text to the backend. Which project
that is comes from the title of the window that was focused when recording started.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

PROJECT_FILE_NAME = ".vox.md"
MAX_PROJECT_BYTES = 8000

ProjectStatus = Literal["off", "no-match", "missing", "unreadable", "empty", "ok"]


@dataclass(frozen=True, slots=True)
class ProjectSource:
    """What the companion resolved for one recording, and where it came from.

    ``name`` and ``text`` are set only when ``status`` is ``"ok"``; every other status means
    nothing is sent with the request, and says why:

    * ``"off"`` - detection is disabled or the window title was unusable;
    * ``"no-match"`` - no configured root's folder name occurs in the window title;
    * ``"missing"`` / ``"unreadable"`` / ``"empty"`` - a root matched but its file was absent,
      could not be read or decoded, or held nothing but whitespace.

    ``path`` is the file that was tried, so it is present for the last three even though
    nothing was read. ``sent_bytes`` counts the UTF-8 bytes of ``text``, what actually goes
    over the wire, which is below the size on disk when ``truncated``.
    """

    status: ProjectStatus = "off"
    name: str | None = None
    text: str | None = None
    path: Path | None = None
    sent_bytes: int = 0
    truncated: bool = False


def resolve_project_source(
    roots: Sequence[str | Path],
    window_title: str,
    file_name: str = PROJECT_FILE_NAME,
    *,
    max_bytes: int = MAX_PROJECT_BYTES,
    detect_from_window: bool = True,
) -> ProjectSource:
    """Return the project context for ``window_title`` together with its provenance.

    A root matches when its folder name occurs case-insensitively in ``window_title``, which
    is how IntelliJ and most editors title their windows; the longest matching folder name
    wins, so a root named ``vox`` cannot shadow one named ``vox-client``. The text is
    truncated to ``max_bytes`` UTF-8 bytes, on a line boundary when the truncated part
    contains one, because the backend charges a full prefill for anything oversized.

    Never raises: a project file is an optimisation, and no failure to find one may cost the
    user a request. Anything that went wrong comes back as a :class:`ProjectSource` status.

    The window title is an argument rather than a Win32 call, so this is testable, and the
    only side effect is reading that one file.
    """
    if not detect_from_window or not window_title:
        return ProjectSource(status="off")
    root = _match(roots, window_title)
    if root is None:
        return ProjectSource(status="no-match")
    path = root / file_name
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.debug("no %s in %s", file_name, root)
        return ProjectSource(status="missing", path=path)
    except (OSError, ValueError) as exc:
        logger.warning("cannot read %s: %s", path, exc)
        return ProjectSource(status="unreadable", path=path)
    if not text.strip():
        return ProjectSource(status="empty", path=path)
    sent = _truncate(text, max_bytes, path)
    return ProjectSource(
        status="ok",
        name=root.name,
        text=sent,
        path=path,
        sent_bytes=len(sent.encode("utf-8")),
        truncated=sent != text,
    )


def resolve_project(
    roots: Sequence[str | Path],
    window_title: str,
    file_name: str = PROJECT_FILE_NAME,
    *,
    max_bytes: int = MAX_PROJECT_BYTES,
    detect_from_window: bool = True,
) -> tuple[str | None, str | None]:
    """Return ``(project name, project file text)`` for the project ``window_title`` names.

    The two-value view of :func:`resolve_project_source`, for callers that only need what is
    sent with the request. Both values are None whenever no text was resolved, whatever the
    reason; :func:`resolve_project_source` says which reason it was.
    """
    source = resolve_project_source(
        roots,
        window_title,
        file_name,
        max_bytes=max_bytes,
        detect_from_window=detect_from_window,
    )
    return source.name, source.text


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
