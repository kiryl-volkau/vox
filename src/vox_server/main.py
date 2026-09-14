from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from . import __version__, runtime_config
from .api import register_exception_handlers, register_routes
from .config import Settings, get_settings, redacted_base_url, set_settings
from .glossary import Glossary
from .health import ServerState, refresh_llm
from .llm import OpenAICompatibleClient
from .processor import Processor
from .prompt import Prompt, load_prompt
from .trace import TraceWriter
from .transcription import Transcriber

logger = logging.getLogger(__name__)

_UVICORN_LOG_LEVELS = frozenset({"critical", "error", "warning", "info", "debug", "trace"})


def configure_logging(level: str) -> None:
    """Configure root logging at ``level`` (a name such as "INFO"; unknown names mean INFO).

    Replaces any handlers installed earlier, so calling it twice is safe.
    """
    resolved = logging.getLevelNamesMapping().get(level.strip().upper(), logging.INFO)
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        force=True,
    )
    # These emit one INFO line per HTTP request and per model shard, which buries the
    # per-request timing lines this server exists to make readable.
    for noisy in ("httpx", "httpcore", "huggingface_hub", "urllib3", "filelock"):
        logging.getLogger(noisy).setLevel(max(resolved, logging.WARNING))


LLM_WARMUP_TIMEOUT_S = 180.0


async def _warm_llm(llm: OpenAICompatibleClient) -> None:
    """Send one throwaway completion so the server has the weights resident.

    Ollama and LM Studio load a model lazily on first use, which can take far longer than
    LLM_TIMEOUT_SECONDS for a multi-GB model; paying that here keeps the first real voice
    request fast. Failures are logged and ignored - /health already reports LLM reachability.
    """
    started = time.perf_counter()
    try:
        # A generous budget, not LLM_TIMEOUT_SECONDS: this call is what forces a multi-GB
        # model off disk and into VRAM, which legitimately takes longer than any later request.
        await llm.chat("", "ping", timeout_seconds=LLM_WARMUP_TIMEOUT_S)
    except Exception as exc:
        logger.warning(
            "llm warmup failed (%s: %s); first request may be slow", type(exc).__name__, exc
        )
        return
    logger.info("llm warm in %d ms", int((time.perf_counter() - started) * 1000))


def build_trace_writer(settings: Settings) -> TraceWriter | None:
    """Return the request-trace writer, or None when ``trace_dir`` is unset.

    Logs one INFO line when tracing is on and nothing when it is off: the files hold
    transcripts, prompts and model output, so the startup log has to say so out loud.
    """
    if settings.trace_dir is None:
        return None
    logger.info(
        "request tracing is ON: the newest %d traces go to %s, "
        "each holding the transcript, the prompts and the model output",
        settings.trace_keep,
        settings.trace_dir,
    )
    return TraceWriter(settings.trace_dir, settings.trace_keep)


async def _warm_up(
    state: ServerState,
    transcriber: Transcriber,
    llm: OpenAICompatibleClient,
    prompt: Prompt,
    dictation_prompt: Prompt,
    analysis_prompt: Prompt,
    glossary: Glossary,
    settings: Settings,
    trace_writer: TraceWriter | None,
) -> None:
    """Load STT and probe the LLM, then publish the processor and clear the warming flag.

    Never raises: an STT failure is recorded on ``state`` so /health reports it while the
    process keeps serving. The processor is published either way so LLM-only endpoints
    (/v1/transform) still work when speech recognition is unavailable.
    """
    try:
        await asyncio.to_thread(transcriber.load)
        await asyncio.to_thread(transcriber.warmup)
        logger.info(
            "stt ready: model=%s device=%s compute_type=%s",
            transcriber.model_name,
            transcriber.device,
            transcriber.compute_type,
        )
    except Exception as exc:
        state.stt_error = str(exc)
        logger.exception("stt failed to start; /health will report degraded")

    await refresh_llm(state)
    # The live client, not the settings: a stored override may have repointed it at startup.
    logger.info(
        "llm %s at %s: %s",
        llm.model,
        redacted_base_url(llm.base_url),
        "ready" if state.llm_ready else f"unavailable ({state.llm_error})",
    )
    if state.llm_ready and state.llm_override.applied_to(settings).warmup:
        await _warm_llm(llm)
    elif state.llm_ready:
        logger.info("llm warmup is off; the first request pays whatever the endpoint charges it")

    state.processor = Processor(
        transcriber,
        llm,
        prompt,
        dictation_prompt,
        glossary,
        settings,
        trace_writer,
        analysis_prompt=analysis_prompt,
    )
    state.warming = False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load glossary, both prompts, STT and the LLM client, then publish them on ``app.state``.

    A prompt failure is fatal: without them the server has nothing to serve. An STT failure is
    not - it is recorded on the state so /health reports it while the process keeps
    answering.
    """
    settings = get_settings()
    configure_logging(settings.log_level)

    glossary = Glossary.load(settings.glossary_path)
    prompt = load_prompt(settings.prompt_path)
    dictation_prompt = load_prompt(settings.dictation_prompt_path)
    analysis_prompt = load_prompt(settings.analysis_prompt_path)
    override = runtime_config.load(settings)
    effective = override.applied_to(settings)
    state = ServerState(
        settings=settings,
        started_at=time.monotonic(),
        prompt=prompt,
        dictation_prompt=dictation_prompt,
        glossary=glossary,
        llm_override=override,
    )
    app.state.server_state = state
    logger.info("loaded %d glossary entries", len(glossary))

    transcriber = Transcriber(settings, hotwords=glossary.as_stt_prompt() or None)
    state.transcriber = transcriber
    if effective.overridden:
        logger.info(
            "llm settings overridden at runtime: %s model=%s",
            redacted_base_url(effective.base_url),
            effective.model,
        )
    llm = OpenAICompatibleClient(
        base_url=effective.base_url,
        model=effective.model,
        api_key=effective.api_key,
        timeout_seconds=settings.llm_timeout_seconds,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )
    state.llm = llm

    # Loading Whisper takes tens of seconds (minutes on a cold model cache). Warming in the
    # background lets /health answer "warming" straight away instead of refusing connections,
    # which is what the companion and start.ps1 report progress from.
    trace_writer = build_trace_writer(settings)
    warmup_task = asyncio.create_task(
        _warm_up(
            state,
            transcriber,
            llm,
            prompt,
            dictation_prompt,
            analysis_prompt,
            glossary,
            settings,
            trace_writer,
        )
    )
    logger.info("listening on %s:%d (warming up in the background)", settings.host, settings.port)
    try:
        yield
    finally:
        warmup_task.cancel()
        with suppress(asyncio.CancelledError):
            await warmup_task
        await llm.aclose()
        transcriber.close()


def create_app() -> FastAPI:
    """Build the FastAPI application: routes, error handlers and the startup lifespan."""
    app = FastAPI(title="vox", version=__version__, lifespan=lifespan)
    register_routes(app)
    register_exception_handlers(app)
    return app


app = create_app()


def build_parser() -> argparse.ArgumentParser:
    """Command-line overrides for every setting that is worth changing at launch.

    Anything left unset falls back to the environment (or .env), then to the built-in
    default, so the container keeps working with env vars alone while a native run can be
    configured entirely from the command line.
    """
    parser = argparse.ArgumentParser(
        prog="vox-server",
        description="Vox backend: Whisper speech-to-text plus a local OpenAI-compatible LLM.",
    )
    parser.add_argument("--host", help="interface to bind (default 0.0.0.0)")
    parser.add_argument("--port", type=int, help="port to bind (default 8765)")
    parser.add_argument("--stt-model", help="Whisper model, e.g. turbo, large-v3, small")
    parser.add_argument("--stt-language", help="forced language code, e.g. ru; empty to autodetect")
    parser.add_argument("--stt-device", choices=("auto", "cuda", "cpu"), help="STT device")
    parser.add_argument("--stt-compute-type", help="e.g. float16, int8_float16, int8")
    parser.add_argument("--stt-beam-size", type=int, help="Whisper beam size (default 1)")
    parser.add_argument("--llm-base-url", help="OpenAI-compatible base URL ending in /v1")
    parser.add_argument("--llm-model", help="model name as the LLM server reports it")
    parser.add_argument("--llm-timeout-seconds", type=float, help="per-request LLM timeout")
    parser.add_argument("--llm-temperature", type=float, help="sampling temperature")
    parser.add_argument("--llm-max-tokens", type=int, help="maximum tokens to generate")
    parser.add_argument("--prompt-path", type=Path, help="markdown file holding the task prompt")
    parser.add_argument(
        "--dictation-prompt-path", type=Path, help="markdown file holding the dictation prompt"
    )
    parser.add_argument(
        "--analysis-prompt-path", type=Path, help="markdown file holding the analysis prompt"
    )
    parser.add_argument("--glossary-path", type=Path, help="glossary YAML file")
    parser.add_argument("--processing-concurrency", type=int, help="concurrent AI pipelines")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument(
        "--log-text",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="include transcripts and output in DEBUG logs",
    )
    parser.add_argument(
        "--trace-dir",
        type=Path,
        help="write a JSON trace of every request here; off unless set, and the files "
        "contain transcripts, prompts and model output",
    )
    parser.add_argument("--trace-keep", type=int, help="trace files to keep (default 200)")
    parser.add_argument("--version", action="version", version=f"vox-server {__version__}")
    return parser


def settings_from_args(argv: Sequence[str] | None = None) -> Settings:
    """Build Settings from the environment, overridden by any options actually passed."""
    namespace = build_parser().parse_args(argv)
    overrides = {key: value for key, value in vars(namespace).items() if value is not None}
    return Settings(**overrides)


def main(argv: Sequence[str] | None = None) -> None:
    """Serve the backend with uvicorn on the configured host and port."""
    settings = settings_from_args(argv)
    set_settings(settings)
    configure_logging(settings.log_level)
    level = settings.log_level.strip().lower()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=level if level in _UVICORN_LOG_LEVELS else "info",
    )


if __name__ == "__main__":
    main()
