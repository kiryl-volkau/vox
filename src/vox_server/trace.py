from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .analysis import Analysis

logger = logging.getLogger(__name__)

type Endpoint = Literal["process", "transcribe", "transform"]
type PromptKind = Literal["task", "dictation"]
type TraceStatus = Literal["ok", "error"]

_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9_-]")


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class RequestTrace:
    """Everything one request did, accumulated stage by stage.

    Only ``request_id`` and ``endpoint`` are known when the trace is created; every other
    field is filled in by the stage that produces it, so a request that fails half way still
    serialises into a useful document.

    This object holds transcripts, prompts and model output in memory. It is written to disk
    only through :class:`TraceWriter`, which the server builds only when tracing is on.
    """

    request_id: str
    endpoint: Endpoint
    prompt_kind: PromptKind | None = None
    started_at: datetime = field(default_factory=_utc_now)
    status: TraceStatus = "ok"
    error_type: str | None = None
    error_message: str | None = None

    client_id: str | None = None
    client_version: str | None = None
    client_audio_seconds: float | None = None

    audio_bytes: int | None = None
    audio_decoded_seconds: float | None = None

    stt_model: str = ""
    stt_device: str = ""
    stt_compute_type: str = ""
    stt_language: str = ""
    stt_language_probability: float = 0.0
    stt_duration_ms: int | None = None
    stt_transcript: str = ""

    requested_language: str | None = None
    output_language: str | None = None

    project_name: str | None = None
    project_received_bytes: int | None = None
    project_used_bytes: int | None = None
    project_text: str | None = None
    project_instructions: str | None = None

    context_received_bytes: int | None = None
    context_used_bytes: int | None = None
    context_text: str | None = None

    glossary_entries: int | None = None
    glossary_prompt_block_chars: int = 0

    prompt_system: str | None = None
    prompt_user: str = ""

    llm_model: str | None = None
    llm_base_url: str = ""
    llm_temperature: float | None = None
    llm_duration_ms: int = 0
    llm_raw_output: str = ""
    llm_cleaned_output: str = ""
    analysis: Analysis | None = None
    analysis_raw_output: str = ""
    analysis_duration_ms: int = 0
    analysis_schema_refused: bool = False

    output_text: str | None = None
    output_fell_back_to_transcript: bool = False

    transcription_ms: int = 0
    llm_ms: int = 0
    total_ms: int = 0

    def record_error(self, exc: BaseException) -> None:
        """Mark the request failed, keeping the exception type and message but no traceback."""
        self.status = "error"
        self.error_type = type(exc).__name__
        self.error_message = str(exc)

    def file_name(self) -> str:
        """Return this trace's file name. The UTC timestamp leads, so name order is time order."""
        stamp = self.started_at.astimezone(UTC)
        request_id = _UNSAFE_IN_NAME.sub("_", self.request_id)[:64] or "request"
        return f"{stamp:%Y%m%d-%H%M%S}-{stamp.microsecond // 1000:03d}-{request_id}.json"

    def to_dict(self) -> dict[str, Any]:
        """Return the trace as the JSON document written to disk.

        A section that does not apply to the request is ``None`` rather than a dict of empty
        values: ``stt`` and ``audio`` for /v1/transform, ``project`` for /v1/transcribe, and
        ``glossary``/``prompt``/``llm`` for a request that never reached the model. A stage
        that was reached but failed keeps whatever it managed to record.
        """
        return {
            "request_id": self.request_id,
            "started_at": self.started_at.astimezone(UTC).isoformat(),
            "endpoint": self.endpoint,
            "status": self.status,
            "error": self._error_section(),
            "client": {
                "id": self.client_id,
                "version": self.client_version,
                "audio_seconds": self.client_audio_seconds,
            },
            "audio": self._audio_section(),
            "stt": self._stt_section(),
            "language": {
                "requested": self.requested_language,
                "written": self.output_language,
            },
            "project": self._project_section(),
            "context": self._context_section(),
            "glossary": self._glossary_section(),
            "prompt": self._prompt_section(),
            "llm": self._llm_section(),
            "analysis": self._analysis_section(),
            "output": self._output_section(),
            "timings_ms": {
                "transcription": self.transcription_ms,
                "llm": self.llm_ms,
                "total": self.total_ms,
            },
        }

    def _error_section(self) -> dict[str, Any] | None:
        if self.error_type is None:
            return None
        return {"type": self.error_type, "message": self.error_message or ""}

    def _audio_section(self) -> dict[str, Any] | None:
        if self.endpoint == "transform":
            return None
        return {"bytes": self.audio_bytes, "decoded_seconds": self.audio_decoded_seconds}

    def _stt_section(self) -> dict[str, Any] | None:
        duration_ms = self.stt_duration_ms
        if duration_ms is None:
            return None
        return {
            "model": self.stt_model,
            "device": self.stt_device,
            "compute_type": self.stt_compute_type,
            "language": self.stt_language,
            "language_probability": self.stt_language_probability,
            "duration_ms": duration_ms,
            "transcript": self.stt_transcript,
        }

    def _project_section(self) -> dict[str, Any] | None:
        if self.endpoint == "transcribe":
            return None
        received, used = self.project_received_bytes, self.project_used_bytes
        return {
            "name": self.project_name,
            "received_bytes": received,
            "used_bytes": used,
            "truncated": received is not None and used is not None and received != used,
            "text": self.project_text,
            "instructions": self.project_instructions,
        }

    def _context_section(self) -> dict[str, Any] | None:
        if self.endpoint == "transcribe":
            return None
        received, used = self.context_received_bytes, self.context_used_bytes
        return {
            "received_bytes": received,
            "used_bytes": used,
            "truncated": received is not None and used is not None and received != used,
            "text": self.context_text,
        }

    def _glossary_section(self) -> dict[str, Any] | None:
        entries = self.glossary_entries
        if entries is None:
            return None
        return {"entries": entries, "prompt_block_chars": self.glossary_prompt_block_chars}

    def _prompt_section(self) -> dict[str, Any] | None:
        system = self.prompt_system
        if system is None:
            return None
        return {
            "kind": self.prompt_kind,
            "system_chars": len(system),
            "user_chars": len(self.prompt_user),
            "system": system,
            "user": self.prompt_user,
        }

    def _llm_section(self) -> dict[str, Any] | None:
        model = self.llm_model
        if model is None:
            return None
        return {
            "model": model,
            "base_url": self.llm_base_url,
            "temperature": self.llm_temperature,
            "duration_ms": self.llm_duration_ms,
            "raw_output": self.llm_raw_output,
            "cleaned_output": self.llm_cleaned_output,
            "cleanup_changed": self.llm_raw_output != self.llm_cleaned_output,
        }

    def _output_section(self) -> dict[str, Any] | None:
        text = self.output_text
        if text is None:
            return None
        return {
            "chars": len(text),
            "fell_back_to_transcript": self.output_fell_back_to_transcript,
            "text": text,
        }

    def _analysis_section(self) -> dict[str, Any] | None:
        """The second pass: how the speech was read, and what that pass cost.

        None when the pass never ran. A pass that ran and came back unreadable still reports,
        with ``fields`` empty and ``raw_output`` holding whatever the model did say - which is
        the only way to find out why it could not be read.
        """
        if not self.analysis_duration_ms and not self.analysis_raw_output:
            return None
        analysis = self.analysis
        return {
            "duration_ms": self.analysis_duration_ms,
            "schema_refused": self.analysis_schema_refused,
            "parsed": analysis is not None,
            "raw_output": self.analysis_raw_output,
            "fields": None if analysis is None else analysis.model_dump(mode="json"),
        }


class TraceWriter:
    """Writes one JSON file per request into a directory, newest ``keep`` files retained.

    Diagnostic only, and opt-in: the files hold transcripts, prompts and model output, which
    the server otherwise never puts on disk.

    :meth:`write` never raises and never blocks a voice request on a filesystem problem; a
    failure is one WARNING line and a ``None`` return. Files appear atomically, so a reader
    watching the directory can never open a half-written trace. ``keep`` is clamped to at
    least 1, so the file just written always survives the prune.
    """

    def __init__(self, directory: Path, keep: int) -> None:
        self._directory = directory
        self._keep = max(1, keep)

    @property
    def directory(self) -> Path:
        """Where traces are written. Created on the first successful write, not before."""
        return self._directory

    def write(self, trace: RequestTrace) -> Path | None:
        """Serialise ``trace`` into the directory and return the file, or None if it failed.

        The directory is created on demand. Text is UTF-8 and unescaped, and indented, so the
        Russian content stays readable when the file is opened by hand.
        """
        path = self._directory / trace.file_name()
        temporary = path.with_name(f"{path.name}.tmp")
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            document = json.dumps(trace.to_dict(), ensure_ascii=False, indent=2)
            temporary.write_text(document, encoding="utf-8")
            temporary.replace(path)
        # ValueError covers the lone surrogates that a model can emit, which UTF-8 cannot encode.
        except (OSError, ValueError) as exc:
            logger.warning("could not write trace %s (%s: %s)", path, type(exc).__name__, exc)
            self._discard(temporary)
            return None
        self._prune()
        return path

    def _prune(self) -> None:
        try:
            stale = sorted(self._directory.glob("*.json"))[: -self._keep]
            for path in stale:
                path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "could not prune traces in %s (%s: %s)", self._directory, type(exc).__name__, exc
            )

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("leftover partial trace %s could not be removed", path)
