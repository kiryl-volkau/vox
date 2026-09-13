from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .config import Settings, redacted_base_url
from .glossary import Glossary
from .llm import OpenAICompatibleClient, clean_llm_output
from .models import (
    ProcessResponse,
    TimingsMs,
    TranscribeResponse,
    TranscriptionResult,
    TransformResponse,
)
from .modes import Mode, ModeRegistry
from .trace import RequestTrace, TraceWriter
from .transcription import Transcriber

logger = logging.getLogger(__name__)


class ProcessingError(RuntimeError):
    """Base class for failures of the transcribe -> normalise -> wrap pipeline."""


class EmptyTranscriptError(ProcessingError):
    """Speech recognition produced nothing but whitespace."""


class EmptyOutputError(ProcessingError):
    """Normalisation produced nothing and the mode has no transcript fallback."""


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _byte_length(text: str | None) -> int | None:
    return None if text is None else len(text.encode("utf-8"))


def _truncate_project(project: str | None, max_bytes: int) -> str:
    """Return ``project`` cut to at most ``max_bytes`` UTF-8 bytes, on a line boundary.

    ``None`` or blank text yields an empty string. Oversized text is truncated at the last
    newline that fits, or mid-line when there is none, and never rejected; a WARNING names
    the original byte count. The text itself is never logged.
    """
    if not project:
        return ""
    encoded = project.encode("utf-8")
    if len(encoded) <= max_bytes:
        return project
    head = encoded[:max_bytes].decode("utf-8", errors="ignore")
    boundary = head.rfind("\n")
    kept = head[:boundary] if boundary > 0 else head
    logger.warning(
        "project context of %d bytes exceeds max_project_bytes=%d; truncated to %d bytes",
        len(encoded),
        max_bytes,
        len(kept.encode("utf-8")),
    )
    return kept


class Processor:
    """Runs voice requests through STT, the mode's LLM prompt and the Claude wrapper.

    Concurrency is capped at ``settings.processing_concurrency`` (minimum 1) because a
    single GPU serialises Whisper anyway; further requests wait on ``semaphore``.

    ``trace_writer`` is the opt-in diagnostic sink: when it is None nothing is written to
    disk, which is the default and the only behaviour the product promises.
    """

    def __init__(
        self,
        transcriber: Transcriber,
        llm: OpenAICompatibleClient,
        modes: ModeRegistry,
        glossary: Glossary,
        settings: Settings,
        trace_writer: TraceWriter | None = None,
    ) -> None:
        self._transcriber = transcriber
        self._llm = llm
        self._modes = modes
        self._glossary_entries = len(glossary)
        self._glossary_block = glossary.as_prompt_block()
        self._settings = settings
        self._trace_writer = trace_writer
        self._semaphore = asyncio.Semaphore(max(1, settings.processing_concurrency))

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """The concurrency gate shared by every processing entry point."""
        return self._semaphore

    async def process(
        self,
        audio: bytes,
        mode_name: str,
        *,
        request_id: str,
        language: str | None = None,
        project: str | None = None,
        project_name: str | None = None,
        client_id: str | None = None,
        client_version: str | None = None,
        audio_seconds: float | None = None,
    ) -> ProcessResponse:
        """Transcribe ``audio``, normalise it with ``mode_name`` and wrap it for Claude.

        ``language`` overrides the configured STT language; ``None`` keeps the default.
        ``project`` is the caller's project context file; it reaches the system prompt of
        modes that use an LLM, truncated to ``max_project_bytes``, and is ignored by the
        rest. ``project_name``, ``client_id``, ``client_version`` and ``audio_seconds`` are
        what the caller reported about itself; they are only recorded, never acted on.
        Raises UnknownModeError for an unknown mode (before any GPU work),
        EmptyTranscriptError when nothing was recognised, EmptyOutputError when the mode
        yields no text, plus TranscriptionError / LlmError from the underlying stages.
        """
        trace = RequestTrace(
            request_id=request_id,
            endpoint="process",
            mode=mode_name,
            client_id=client_id,
            client_version=client_version,
            client_audio_seconds=audio_seconds,
            audio_bytes=len(audio),
            project_name=project_name,
            project_received_bytes=_byte_length(project),
        )
        started = time.perf_counter()
        try:
            mode = self._modes.get(mode_name)
            trace.mode = mode.name
            async with self._semaphore:
                stt_started = time.perf_counter()
                result = await asyncio.to_thread(self._transcriber.transcribe, audio, language)
                transcription_ms = _elapsed_ms(stt_started)
                transcript = result.text.strip()
                self._record_stt(trace, result, transcript, transcription_ms)
                if not transcript:
                    raise EmptyTranscriptError("speech recognition produced an empty transcript")
                llm_started = time.perf_counter()
                normalized = await self._normalize(mode, transcript, project, trace)
                llm_ms = _elapsed_ms(llm_started) if mode.requires_llm else 0
                output = mode.render_wrapper(normalized)
                timings = TimingsMs(
                    transcription=transcription_ms, llm=llm_ms, total=_elapsed_ms(started)
                )
            self._record_output(trace, mode, output, timings)
            return ProcessResponse(
                request_id=request_id,
                mode=mode.name,
                transcript=transcript,
                normalized_text=normalized,
                output=output,
                language=result.language,
                timings_ms=timings,
            )
        except BaseException as exc:
            trace.record_error(exc)
            trace.total_ms = _elapsed_ms(started)
            raise
        finally:
            self._finish(trace, trace.stt_transcript)

    async def transcribe_only(
        self,
        audio: bytes,
        *,
        request_id: str,
        language: str | None = None,
        project: str | None = None,  # noqa: ARG002
    ) -> TranscribeResponse:
        """Transcribe ``audio`` without touching the LLM. Raises EmptyTranscriptError.

        ``project`` is accepted so every entry point takes the same keywords, and ignored:
        a raw transcript never reaches the LLM, so no trace records it either.
        """
        trace = RequestTrace(
            request_id=request_id,
            endpoint="transcribe",
            mode="transcribe",
            audio_bytes=len(audio),
        )
        started = time.perf_counter()
        try:
            async with self._semaphore:
                stt_started = time.perf_counter()
                result = await asyncio.to_thread(self._transcriber.transcribe, audio, language)
                transcription_ms = _elapsed_ms(stt_started)
            transcript = result.text.strip()
            self._record_stt(trace, result, transcript, transcription_ms)
            if not transcript:
                raise EmptyTranscriptError("speech recognition produced an empty transcript")
            timings = TimingsMs(transcription=transcription_ms, llm=0, total=_elapsed_ms(started))
            trace.total_ms = timings.total
            return TranscribeResponse(
                request_id=request_id,
                transcript=transcript,
                language=result.language,
                timings_ms=timings,
            )
        except BaseException as exc:
            trace.record_error(exc)
            trace.total_ms = _elapsed_ms(started)
            raise
        finally:
            self._finish(trace, trace.stt_transcript)

    async def transform_only(
        self,
        text: str,
        mode_name: str,
        *,
        request_id: str,
        project: str | None = None,
    ) -> TransformResponse:
        """Run ``text`` through a mode's LLM prompt and wrapper, skipping speech recognition.

        Empty or whitespace-only ``text`` raises EmptyTranscriptError. ``project`` is handled
        exactly as in :meth:`process`.
        """
        trace = RequestTrace(
            request_id=request_id,
            endpoint="transform",
            mode=mode_name,
            project_received_bytes=_byte_length(project),
        )
        source = text.strip()
        started = time.perf_counter()
        try:
            mode = self._modes.get(mode_name)
            trace.mode = mode.name
            if not source:
                raise EmptyTranscriptError("no text to transform")
            async with self._semaphore:
                llm_started = time.perf_counter()
                normalized = await self._normalize(mode, source, project, trace)
                llm_ms = _elapsed_ms(llm_started) if mode.requires_llm else 0
                output = mode.render_wrapper(normalized)
            timings = TimingsMs(transcription=0, llm=llm_ms, total=_elapsed_ms(started))
            self._record_output(trace, mode, output, timings)
            return TransformResponse(
                request_id=request_id,
                mode=mode.name,
                normalized_text=normalized,
                output=output,
                timings_ms=timings,
            )
        except BaseException as exc:
            trace.record_error(exc)
            trace.total_ms = _elapsed_ms(started)
            raise
        finally:
            self._finish(trace, source)

    async def _normalize(
        self, mode: Mode, text: str, project: str | None, trace: RequestTrace
    ) -> str:
        if not mode.requires_llm:
            normalized = text
        else:
            context = _truncate_project(project, self._settings.max_project_bytes)
            system = mode.render_system(context)
            user = mode.render_user(text, self._glossary_block)
            self._record_prompt(trace, mode, context, system, user)
            llm_started = time.perf_counter()
            raw = await self._llm.chat(system, user, temperature=mode.temperature)
            trace.llm_duration_ms = _elapsed_ms(llm_started)
            normalized = clean_llm_output(raw)
            trace.llm_raw_output = raw
            trace.llm_cleaned_output = normalized
        normalized = normalized.strip()
        if not normalized and mode.fallback_to_transcript:
            normalized = text
        if not normalized:
            raise EmptyOutputError(f"mode '{mode.name}' produced no text")
        return normalized

    def _record_stt(
        self,
        trace: RequestTrace,
        result: TranscriptionResult,
        transcript: str,
        transcription_ms: int,
    ) -> None:
        trace.audio_decoded_seconds = result.audio_duration_s
        trace.stt_model = self._transcriber.model_name
        trace.stt_device = self._transcriber.device
        trace.stt_compute_type = self._transcriber.compute_type
        trace.stt_language = result.language
        trace.stt_language_probability = result.language_probability
        trace.stt_duration_ms = result.duration_ms
        trace.stt_transcript = transcript
        trace.transcription_ms = transcription_ms

    def _record_prompt(
        self, trace: RequestTrace, mode: Mode, context: str, system: str, user: str
    ) -> None:
        trace.project_used_bytes = len(context.encode("utf-8"))
        trace.project_text = context
        trace.glossary_entries = self._glossary_entries
        trace.glossary_prompt_block_chars = len(self._glossary_block)
        trace.prompt_system = system
        trace.prompt_user = user
        trace.llm_model = self._llm.model
        trace.llm_base_url = redacted_base_url(self._llm.base_url)
        trace.llm_temperature = mode.temperature

    @staticmethod
    def _record_output(trace: RequestTrace, mode: Mode, output: str, timings: TimingsMs) -> None:
        trace.output_wrapped_for_claude = mode.wrap_for_claude
        trace.output_text = output
        trace.llm_ms = timings.llm
        trace.total_ms = timings.total

    def _finish(self, trace: RequestTrace, source: str) -> None:
        written = self._trace_writer.write(trace) if self._trace_writer is not None else None
        if trace.status == "ok":
            self._log_result(trace, source, written)

    def _log_result(self, trace: RequestTrace, source: str, trace_file: Path | None) -> None:
        logger.info(
            "request_id=%s mode=%s audio_s=%.2f stt_ms=%d llm_ms=%d total_ms=%d out_chars=%d "
            "project=%s project_bytes=%d system_chars=%d user_chars=%d trace=%s",
            trace.request_id,
            trace.mode,
            trace.audio_decoded_seconds or 0.0,
            trace.transcription_ms,
            trace.llm_ms,
            trace.total_ms,
            len(trace.output_text or ""),
            trace.project_name or "-",
            trace.project_received_bytes or 0,
            len(trace.prompt_system or ""),
            len(trace.prompt_user),
            trace_file.name if trace_file is not None else "-",
        )
        if self._settings.log_text:
            logger.debug(
                "request_id=%s transcript=%r output=%r",
                trace.request_id,
                source,
                trace.output_text or "",
            )
