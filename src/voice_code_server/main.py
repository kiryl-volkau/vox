from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI

from . import __version__
from .api import register_exception_handlers, register_routes
from .config import Settings, get_settings, redacted_base_url
from .glossary import Glossary
from .health import ServerState, refresh_llm
from .llm import OpenAICompatibleClient
from .modes import ModeRegistry
from .processor import Processor
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


async def _warm_llm(llm: OpenAICompatibleClient) -> None:
    """Send one throwaway completion so the server has the weights resident.

    Ollama and LM Studio load a model lazily on first use, which can take far longer than
    LLM_TIMEOUT_SECONDS for a multi-GB model; paying that here keeps the first real voice
    request fast. Failures are logged and ignored - /health already reports LLM reachability.
    """
    started = time.perf_counter()
    try:
        await llm.chat("", "ping")
    except Exception as exc:
        logger.warning(
            "llm warmup failed (%s: %s); first request may be slow", type(exc).__name__, exc
        )
        return
    logger.info("llm warm in %d ms", int((time.perf_counter() - started) * 1000))


async def _warm_up(
    state: ServerState,
    transcriber: Transcriber,
    llm: OpenAICompatibleClient,
    modes: ModeRegistry,
    glossary: Glossary,
    settings: Settings,
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
    logger.info(
        "llm %s at %s: %s",
        settings.llm_model,
        redacted_base_url(settings.llm_base_url),
        "ready" if state.llm_ready else f"unavailable ({state.llm_error})",
    )
    if state.llm_ready:
        await _warm_llm(llm)

    state.processor = Processor(transcriber, llm, modes, glossary, settings)
    state.warming = False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load glossary, modes, STT and the LLM client, then publish them on ``app.state``.

    A modes failure is fatal: without prompts the server has nothing to serve. An STT
    failure is not - it is recorded on the state so /health reports it while the process
    keeps answering.
    """
    settings = get_settings()
    configure_logging(settings.log_level)

    glossary = Glossary.load(settings.glossary_path)
    modes = ModeRegistry.load(settings.modes_dir)
    state = ServerState(
        settings=settings,
        started_at=time.monotonic(),
        modes=modes,
        glossary=glossary,
    )
    app.state.server_state = state
    logger.info("loaded %d modes and %d glossary entries", len(modes), len(glossary))

    transcriber = Transcriber(settings, hotwords=glossary.as_stt_prompt() or None)
    state.transcriber = transcriber
    llm = OpenAICompatibleClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key.get_secret_value(),
        timeout_seconds=settings.llm_timeout_seconds,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )
    state.llm = llm

    # Loading Whisper takes tens of seconds (minutes on a cold model cache). Warming in the
    # background lets /health answer "warming" straight away instead of refusing connections,
    # which is what the companion and start.ps1 report progress from.
    warmup_task = asyncio.create_task(_warm_up(state, transcriber, llm, modes, glossary, settings))
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
    app = FastAPI(title="voice-code", version=__version__, lifespan=lifespan)
    register_routes(app)
    register_exception_handlers(app)
    return app


app = create_app()


def main() -> None:
    """Serve the backend with uvicorn on the configured host and port."""
    settings = get_settings()
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
