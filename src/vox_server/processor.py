from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .analysis import RESPONSE_FORMAT, Analysis, parse_analysis, with_hint
from .config import Settings, redacted_base_url
from .glossary import Glossary
from .languages import (
    AUTO_LANGUAGE,
    FALLBACK_LANGUAGE,
    detect_language,
    normalise_language,
)
from .llm import LlmError, LlmResponseError, OpenAICompatibleClient, clean_llm_output
from .models import (
    ProcessResponse,
    TimingsMs,
    TranscribeResponse,
    TranscriptionResult,
    TransformResponse,
)
from .prompt import ProjectFile, Prompt, split_project_file
from .trace import RequestTrace, TraceWriter
from .transcription import Transcriber

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PromptResult:
    """One prompt run: the message to hand back, and the model's own reading of it.

    ``analysis`` is None whenever the reply could not be read as the schema - a backend that
    refused ``response_format``, a model that answered in prose, or malformed JSON. The
    message is usable either way; only the reporting is lost.
    """

    output: str
    analysis: Analysis | None


class ProcessingError(RuntimeError):
    """Base class for failures of the transcribe -> formalise pipeline."""


class EmptyTranscriptError(ProcessingError):
    """Speech recognition produced nothing but whitespace."""


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _byte_length(text: str | None) -> int | None:
    return None if text is None else len(text.encode("utf-8"))


def _truncate(text: str | None, max_bytes: int, what: str) -> str:
    """Return ``text`` cut to at most ``max_bytes`` UTF-8 bytes, on a line boundary.

    ``None`` or blank text yields an empty string. Oversized text is truncated at the last
    newline that fits, or mid-line when there is none, and never rejected; a WARNING names
    ``what`` and the original byte count. The text itself is never logged.
    """
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    head = encoded[:max_bytes].decode("utf-8", errors="ignore")
    boundary = head.rfind("\n")
    kept = head[:boundary] if boundary > 0 else head
    logger.warning(
        "%s of %d bytes exceeds the %d byte budget; truncated to %d bytes",
        what,
        len(encoded),
        max_bytes,
        len(kept.encode("utf-8")),
    )
    return kept


class Processor:
    """Runs voice requests through STT and one of the two prompts.

    ``prompt`` formalises speech into a task; ``dictation_prompt`` only punctuates it and
    strips filler. Which one a request uses is decided by the endpoint the caller chose, and
    is never part of the request body.

    The output language is per request and independent of the spoken one: speech recognition
    keeps using ``settings.stt_language``, while the prompt's language block and the glossary
    spellings follow whatever language the caller asked the answer to be written in.

    Concurrency is capped at ``settings.processing_concurrency`` (minimum 1) because a
    single GPU serialises Whisper anyway; further requests wait on ``semaphore``.

    ``trace_writer`` is the opt-in diagnostic sink: when it is None nothing is written to
    disk, which is the default and the only behaviour the product promises.

    ``analysis_prompt`` drives the second pass that reports how a request was read. Leaving it
    None turns the reporting off entirely and costs a request nothing but the report.
    """

    def __init__(
        self,
        transcriber: Transcriber,
        llm: OpenAICompatibleClient,
        prompt: Prompt,
        dictation_prompt: Prompt,
        glossary: Glossary,
        settings: Settings,
        trace_writer: TraceWriter | None = None,
        *,
        analysis_prompt: Prompt | None = None,
    ) -> None:
        self._transcriber = transcriber
        self._llm = llm
        self._prompt = prompt
        self._dictation_prompt = dictation_prompt
        self._glossary = glossary
        self._glossary_blocks: dict[str, str] = {}
        self._settings = settings
        self._trace_writer = trace_writer
        self._analysis_prompt = analysis_prompt
        self._semaphore = asyncio.Semaphore(max(1, settings.processing_concurrency))

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """The concurrency gate shared by every processing entry point."""
        return self._semaphore

    async def process(
        self,
        audio: bytes,
        *,
        request_id: str,
        dictation: bool = False,
        language: str | None = None,
        project: str | None = None,
        project_name: str | None = None,
        context: str | None = None,
        client_id: str | None = None,
        client_version: str | None = None,
        audio_seconds: float | None = None,
    ) -> ProcessResponse:
        """Transcribe ``audio`` and run it through one of the two prompts.

        ``dictation`` picks the dictation prompt - punctuation and filler removal only -
        instead of the task prompt. ``language`` is the language the answer must be written
        in; ``None`` means ``settings.default_language``, and it does not change speech
        recognition. "auto" - the default - means the language Whisper heard, so the answer
        comes back in whatever was spoken without anybody configuring anything.

        ``project`` is the caller's ``.vox.md``, whose ``## SYSTEM`` section becomes extra
        system instructions and whose remainder becomes project context, both truncated to
        ``max_project_bytes``. ``context`` is the recent conversation the caller
        chose to send, truncated to ``max_context_bytes``. ``project_name``, ``client_id``,
        ``client_version`` and ``audio_seconds`` are what the caller reported about itself;
        they are only recorded, never acted on. Raises EmptyTranscriptError when nothing was
        recognised, plus TranscriptionError / LlmError from the underlying stages; a model
        that answers with nothing falls back to the transcript rather than failing, so speech
        is never lost.
        """
        resolved = self._resolve_language(language)
        trace = RequestTrace(
            request_id=request_id,
            endpoint="process",
            prompt_kind="dictation" if dictation else "task",
            requested_language=resolved,
            client_id=client_id,
            client_version=client_version,
            client_audio_seconds=audio_seconds,
            audio_bytes=len(audio),
            project_name=project_name,
            project_received_bytes=_byte_length(project),
            context_received_bytes=_byte_length(context),
        )
        started = time.perf_counter()
        try:
            async with self._semaphore:
                stt_started = time.perf_counter()
                result = await asyncio.to_thread(self._transcriber.transcribe, audio, None)
                transcription_ms = _elapsed_ms(stt_started)
                transcript = result.text.strip()
                self._record_stt(trace, result, transcript, transcription_ms)
                if not transcript:
                    raise EmptyTranscriptError("speech recognition produced an empty transcript")
                written = self._spoken_language(resolved, result.language)
                trace.output_language = written
                llm_started = time.perf_counter()
                run = await self._run_prompt(
                    transcript, project, context, trace, dictation=dictation, language=written
                )
                timings = TimingsMs(
                    transcription=transcription_ms,
                    llm=_elapsed_ms(llm_started),
                    total=_elapsed_ms(started),
                )
            self._record_output(trace, run.output, timings)
            return ProcessResponse(
                request_id=request_id,
                transcript=transcript,
                output=run.output,
                language=result.language,
                timings_ms=timings,
                analysis=run.analysis,
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

        Here ``language`` is the *spoken* language handed to Whisper, not an output language,
        because this endpoint returns speech as recognised and never reaches a prompt.
        ``project`` is accepted so every entry point takes the same keywords, and ignored.
        """
        trace = RequestTrace(
            request_id=request_id,
            endpoint="transcribe",
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
        *,
        request_id: str,
        dictation: bool = False,
        project: str | None = None,
        context: str | None = None,
        language: str | None = None,
    ) -> TransformResponse:
        """Run already-transcribed ``text`` through a prompt, skipping speech recognition.

        Empty or whitespace-only ``text`` raises EmptyTranscriptError. ``dictation``,
        ``project``, ``context`` and ``language`` are handled exactly as in :meth:`process`.
        """
        resolved = self._resolve_language(language)
        trace = RequestTrace(
            request_id=request_id,
            endpoint="transform",
            prompt_kind="dictation" if dictation else "task",
            requested_language=resolved,
            project_received_bytes=_byte_length(project),
            context_received_bytes=_byte_length(context),
        )
        source = text.strip()
        started = time.perf_counter()
        try:
            if not source:
                raise EmptyTranscriptError("no text to transform")
            written = self._written_language(resolved, source)
            trace.output_language = written
            async with self._semaphore:
                llm_started = time.perf_counter()
                run = await self._run_prompt(
                    source, project, context, trace, dictation=dictation, language=written
                )
                llm_ms = _elapsed_ms(llm_started)
            timings = TimingsMs(transcription=0, llm=llm_ms, total=_elapsed_ms(started))
            self._record_output(trace, run.output, timings)
            return TransformResponse(
                request_id=request_id,
                output=run.output,
                timings_ms=timings,
                analysis=run.analysis,
            )
        except BaseException as exc:
            trace.record_error(exc)
            trace.total_ms = _elapsed_ms(started)
            raise
        finally:
            self._finish(trace, source)

    def _resolve_language(self, language: str | None) -> str:
        """The language the caller asked for, which may still be "auto"."""
        return normalise_language(language) or self._settings.default_language

    @staticmethod
    def _spoken_language(requested: str, detected: str) -> str:
        """Turn a requested "auto" into the language actually spoken.

        ``detected`` is Whisper's own answer for the recording, which is the best evidence
        there is and costs nothing extra. It is normalised because Whisper reports codes the
        rest of the pipeline has never seen, and an unreadable one falls back rather than
        reaching a prompt as a language nobody can write.
        """
        if requested != AUTO_LANGUAGE:
            return requested
        return normalise_language(detected) or FALLBACK_LANGUAGE

    @staticmethod
    def _written_language(requested: str, text: str) -> str:
        """Turn a requested "auto" into a language when there is no recording to ask about.

        ``/v1/transform`` replays text, so the text itself is the only evidence; see
        :func:`vox_server.languages.detect_language` for how little it claims to tell.
        """
        return detect_language(text) if requested == AUTO_LANGUAGE else requested

    def _glossary_block(self, language: str) -> str:
        block = self._glossary_blocks.get(language)
        if block is None:
            block = self._glossary.as_prompt_block(language)
            self._glossary_blocks[language] = block
        return block

    async def _run_prompt(
        self,
        text: str,
        project: str | None,
        context: str | None,
        trace: RequestTrace,
        *,
        dictation: bool,
        language: str,
    ) -> PromptResult:
        prompt = self._dictation_prompt if dictation else self._prompt
        temperature = (
            self._settings.llm_dictation_temperature
            if dictation
            else self._settings.llm_temperature
        )
        project_file = split_project_file(
            _truncate(project, self._settings.max_project_bytes, "project context")
        )
        conversation = _truncate(context, self._settings.max_context_bytes, "conversation context")
        glossary_block = self._glossary_block(language)
        system = prompt.render_system(project_file, conversation, language)
        user = prompt.render_user(text, glossary_block)
        self._record_prompt(
            trace, project_file, conversation, glossary_block, system, user, temperature
        )
        llm_started = time.perf_counter()
        raw = await self._llm.chat(system, user, temperature=temperature)
        trace.llm_duration_ms = _elapsed_ms(llm_started)
        cleaned = clean_llm_output(raw).strip()
        trace.llm_raw_output = raw
        trace.llm_cleaned_output = cleaned
        if not cleaned:
            # Losing what someone just said costs more than handing back an unpolished
            # transcript, so an empty reply degrades to the transcript instead of failing.
            logger.warning(
                "request_id=%s the model returned no usable text; falling back to the transcript",
                trace.request_id,
            )
            trace.output_fell_back_to_transcript = True
            return PromptResult(output=text, analysis=None)
        analysis = await self._analyse(text, cleaned, trace)
        trace.analysis = analysis
        # The dictation prompt hands back the speaker's own words; an instruction appended to
        # those would be text they never said, so only the task prompt takes a hint.
        if analysis is not None and not dictation:
            cleaned = with_hint(cleaned, analysis, language)
        return PromptResult(output=cleaned, analysis=analysis)

    async def _analyse(self, transcript: str, output: str, trace: RequestTrace) -> Analysis | None:
        """Ask a second time how the speech was read, and never let the answer matter too much.

        A separate call rather than extra fields on the rewrite: one 7B model asked to do both
        measurably lost the output language, and the message is what the person is waiting for.
        The price is a second round trip, which the trace reports separately from the first.

        Returns None for every way this can go wrong - no analysis prompt configured, an
        endpoint that rejects ``response_format`` even on the retry, a timeout, an unparseable
        reply. The request still succeeds with its message; only the reporting is lost.
        """
        prompt = self._analysis_prompt
        if prompt is None:
            return None
        system = prompt.render_system(ProjectFile())
        user = prompt.render_user(transcript, "", output)
        started = time.perf_counter()
        try:
            raw = await self._ask_for_analysis(system, user, trace)
        except LlmError as exc:
            logger.warning(
                "request_id=%s the analysis pass failed (%s); the request keeps its message",
                trace.request_id,
                exc,
            )
            return None
        trace.analysis_duration_ms = _elapsed_ms(started)
        trace.analysis_raw_output = raw
        return parse_analysis(raw)

    async def _ask_for_analysis(self, system: str, user: str, trace: RequestTrace) -> str:
        """Ask for a schema-constrained reply, falling back to a plain one if that is refused.

        Not every OpenAI-compatible backend knows ``response_format``; one that does not
        answers 4xx. Temperature is pinned at zero because this pass classifies rather than
        writes, and a classifier that wanders between runs cannot be evaluated.
        """
        try:
            return await self._llm.chat(
                system, user, temperature=0.0, response_format=RESPONSE_FORMAT
            )
        except LlmResponseError as exc:
            logger.warning(
                "request_id=%s the LLM refused a schema-constrained reply (%s); asking plainly",
                trace.request_id,
                exc,
            )
            trace.analysis_schema_refused = True
            return await self._llm.chat(system, user, temperature=0.0)

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
        self,
        trace: RequestTrace,
        project: ProjectFile,
        conversation: str,
        glossary_block: str,
        system: str,
        user: str,
        temperature: float,
    ) -> None:
        trace.project_used_bytes = len(project.context.encode("utf-8"))
        trace.project_text = project.context
        trace.project_instructions = project.instructions
        trace.context_used_bytes = len(conversation.encode("utf-8"))
        trace.context_text = conversation
        trace.glossary_entries = len(self._glossary)
        trace.glossary_prompt_block_chars = len(glossary_block)
        trace.prompt_system = system
        trace.prompt_user = user
        trace.llm_model = self._llm.model
        trace.llm_base_url = redacted_base_url(self._llm.base_url)
        trace.llm_temperature = temperature

    @staticmethod
    def _record_output(trace: RequestTrace, output: str, timings: TimingsMs) -> None:
        trace.output_text = output
        trace.llm_ms = timings.llm
        trace.total_ms = timings.total

    def _finish(self, trace: RequestTrace, source: str) -> None:
        written = self._trace_writer.write(trace) if self._trace_writer is not None else None
        if trace.status == "ok":
            self._log_result(trace, source, written)

    def _log_result(self, trace: RequestTrace, source: str, trace_file: Path | None) -> None:
        logger.info(
            "request_id=%s audio_s=%.2f stt_ms=%d llm_ms=%d total_ms=%d out_chars=%d lang=%s "
            "project=%s project_bytes=%d instructions_chars=%d context_bytes=%d "
            "system_chars=%d user_chars=%d trace=%s",
            trace.request_id,
            trace.audio_decoded_seconds or 0.0,
            trace.transcription_ms,
            trace.llm_ms,
            trace.total_ms,
            len(trace.output_text or ""),
            trace.output_language or "-",
            trace.project_name or "-",
            trace.project_received_bytes or 0,
            len(trace.project_instructions or ""),
            trace.context_used_bytes or 0,
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
