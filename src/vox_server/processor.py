from __future__ import annotations

import asyncio
import logging
import time

from .config import Settings
from .glossary import Glossary
from .llm import OpenAICompatibleClient, clean_llm_output
from .models import ProcessResponse, TimingsMs, TranscribeResponse, TransformResponse
from .modes import Mode, ModeRegistry
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


class Processor:
    """Runs voice requests through STT, the mode's LLM prompt and the Claude wrapper.

    Concurrency is capped at ``settings.processing_concurrency`` (minimum 1) because a
    single GPU serialises Whisper anyway; further requests wait on ``semaphore``.
    """

    def __init__(
        self,
        transcriber: Transcriber,
        llm: OpenAICompatibleClient,
        modes: ModeRegistry,
        glossary: Glossary,
        settings: Settings,
    ) -> None:
        self._transcriber = transcriber
        self._llm = llm
        self._modes = modes
        self._glossary_block = glossary.as_prompt_block()
        self._settings = settings
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
    ) -> ProcessResponse:
        """Transcribe ``audio``, normalise it with ``mode_name`` and wrap it for Claude.

        ``language`` overrides the configured STT language; ``None`` keeps the default.
        Raises UnknownModeError for an unknown mode (before any GPU work),
        EmptyTranscriptError when nothing was recognised, EmptyOutputError when the mode
        yields no text, plus TranscriptionError / LlmError from the underlying stages.
        """
        mode = self._modes.get(mode_name)
        started = time.perf_counter()
        async with self._semaphore:
            stt_started = time.perf_counter()
            result = await asyncio.to_thread(self._transcriber.transcribe, audio, language)
            transcription_ms = _elapsed_ms(stt_started)
            transcript = result.text.strip()
            if not transcript:
                raise EmptyTranscriptError("speech recognition produced an empty transcript")
            llm_started = time.perf_counter()
            normalized = await self._normalize(mode, transcript)
            llm_ms = _elapsed_ms(llm_started) if mode.requires_llm else 0
            output = mode.render_wrapper(normalized)
            timings = TimingsMs(
                transcription=transcription_ms, llm=llm_ms, total=_elapsed_ms(started)
            )
        self._log(request_id, mode.name, result.audio_duration_s, timings, transcript, output)
        return ProcessResponse(
            request_id=request_id,
            mode=mode.name,
            transcript=transcript,
            normalized_text=normalized,
            output=output,
            language=result.language,
            timings_ms=timings,
        )

    async def transcribe_only(
        self,
        audio: bytes,
        *,
        request_id: str,
        language: str | None = None,
    ) -> TranscribeResponse:
        """Transcribe ``audio`` without touching the LLM. Raises EmptyTranscriptError."""
        started = time.perf_counter()
        async with self._semaphore:
            stt_started = time.perf_counter()
            result = await asyncio.to_thread(self._transcriber.transcribe, audio, language)
            transcription_ms = _elapsed_ms(stt_started)
        transcript = result.text.strip()
        if not transcript:
            raise EmptyTranscriptError("speech recognition produced an empty transcript")
        timings = TimingsMs(transcription=transcription_ms, llm=0, total=_elapsed_ms(started))
        self._log(request_id, "transcribe", result.audio_duration_s, timings, transcript, "")
        return TranscribeResponse(
            request_id=request_id,
            transcript=transcript,
            language=result.language,
            timings_ms=timings,
        )

    async def transform_only(
        self, text: str, mode_name: str, *, request_id: str
    ) -> TransformResponse:
        """Run ``text`` through a mode's LLM prompt and wrapper, skipping speech recognition.

        Empty or whitespace-only ``text`` raises EmptyTranscriptError.
        """
        mode = self._modes.get(mode_name)
        source = text.strip()
        if not source:
            raise EmptyTranscriptError("no text to transform")
        started = time.perf_counter()
        async with self._semaphore:
            llm_started = time.perf_counter()
            normalized = await self._normalize(mode, source)
            llm_ms = _elapsed_ms(llm_started) if mode.requires_llm else 0
            output = mode.render_wrapper(normalized)
        timings = TimingsMs(transcription=0, llm=llm_ms, total=_elapsed_ms(started))
        self._log(request_id, mode.name, 0.0, timings, source, output)
        return TransformResponse(
            request_id=request_id,
            mode=mode.name,
            normalized_text=normalized,
            output=output,
            timings_ms=timings,
        )

    async def _normalize(self, mode: Mode, text: str) -> str:
        if not mode.requires_llm:
            normalized = text
        else:
            raw = await self._llm.chat(
                mode.system_prompt,
                mode.render_user(text, self._glossary_block),
                temperature=mode.temperature,
            )
            normalized = clean_llm_output(raw)
        normalized = normalized.strip()
        if not normalized and mode.fallback_to_transcript:
            normalized = text
        if not normalized:
            raise EmptyOutputError(f"mode '{mode.name}' produced no text")
        return normalized

    def _log(
        self,
        request_id: str,
        mode_name: str,
        audio_seconds: float,
        timings: TimingsMs,
        transcript: str,
        output: str,
    ) -> None:
        logger.info(
            "request_id=%s mode=%s audio_s=%.2f stt_ms=%d llm_ms=%d total_ms=%d out_chars=%d",
            request_id,
            mode_name,
            audio_seconds,
            timings.transcription,
            timings.llm,
            timings.total,
            len(output),
        )
        if self._settings.log_text:
            logger.debug("request_id=%s transcript=%r output=%r", request_id, transcript, output)
