from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vox_server.config import Settings
from vox_server.glossary import Glossary
from vox_server.health import ServerState
from vox_server.llm import LlmTimeoutError
from vox_server.main import build_trace_writer
from vox_server.processor import EmptyTranscriptError, Processor
from vox_server.prompt import Prompt
from vox_server.trace import RequestTrace, TraceWriter

AUDIO = b"RIFF-not-really-decoded-by-the-fake"
WAV = b"RIFF----WAVEfmt "
API_KEY = "sk-super-secret-key"
CREDENTIALED_URL = f"http://user:{API_KEY}@ollama:11434/v1"
TRANSCRIPT = "посмотри мембершип"
REPLY = "Проверь membership."
PROJECT_MARKER = "ПРОЕКТ-МАРКЕР-7"
PROJECT = f"{PROJECT_MARKER}: модуль billing не трогаем.\nДжигвард -> Jigward."
PROJECT_HEADER = "<project_context>"
SYSTEM_WITH_PROJECT = "Ты редактор инженерных запросов.\n\n{project}"

TRACE_KEYS = {
    "request_id",
    "started_at",
    "endpoint",
    "status",
    "error",
    "client",
    "audio",
    "stt",
    "language",
    "project",
    "context",
    "glossary",
    "prompt",
    "llm",
    "analysis",
    "output",
    "timings_ms",
}
TRACE_FILE_NAME = re.compile(r"^\d{8}-\d{6}-\d{3}-[A-Za-z0-9_-]+\.json$")

PromptFactory = Callable[..., Prompt]
ProcessorFactory = Callable[..., Processor]
StateFactory = Callable[..., ServerState]
AppFactory = Callable[..., FastAPI]
Fake = Callable[..., Any]


@pytest.fixture
def trace_dir(tmp_path: Path) -> Path:
    """A directory the writer has not created yet, as a fresh install would have it."""
    return tmp_path / "traces"


def written(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


def only_trace(directory: Path) -> dict[str, Any]:
    files = written(directory)
    assert len(files) == 1, files
    document: dict[str, Any] = json.loads(files[0].read_text(encoding="utf-8"))
    return document


def make_trace(request_id: str, *, second: int) -> RequestTrace:
    return RequestTrace(
        request_id=request_id,
        endpoint="process",
        started_at=datetime(2026, 3, 4, 10, 0, second, tzinfo=UTC),
    )


def denied(self: Path, target: Any) -> None:
    raise OSError(13, "Permission denied")


def test_tracing_is_off_until_a_directory_is_configured(
    settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture
) -> None:
    """The default must stay off: a trace file holds transcripts, prompts and model output."""
    settings = settings_factory()

    with caplog.at_level(logging.DEBUG):
        writer = build_trace_writer(settings)

    assert settings.trace_dir is None
    assert writer is None
    assert caplog.records == []


async def test_a_processor_without_a_writer_puts_nothing_on_disk(
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No trace_dir means no file anywhere, and the pipeline behaves exactly as before."""
    processor = processor_factory(transcriber_factory(TRANSCRIPT), llm_factory(REPLY))
    monkeypatch.chdir(tmp_path)

    response = await processor.process(AUDIO, request_id="req-off", project=PROJECT)

    assert response.transcript == TRANSCRIPT
    assert response.output == REPLY
    assert list(tmp_path.rglob("*")) == []


async def test_a_successful_process_writes_one_trace_named_after_the_request(
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    trace_dir: Path,
) -> None:
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(REPLY),
        trace_writer=TraceWriter(trace_dir, 10),
    )

    await processor.process(AUDIO, request_id="req-name-1")

    files = written(trace_dir)
    assert len(files) == 1
    assert TRACE_FILE_NAME.match(files[0].name), files[0].name
    assert files[0].name.endswith("-req-name-1.json")
    assert list(trace_dir.iterdir()) == files


async def test_the_trace_carries_every_documented_section(
    prompt_factory: PromptFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    settings_factory: Callable[..., Settings],
    trace_dir: Path,
) -> None:
    prompt = prompt_factory(user_template="Словарь:\n{glossary}\n\nРасшифровка:\n{transcript}")
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(REPLY),
        prompt,
        glossary=Glossary.from_mapping({"мембершип": "membership"}),
        settings=settings_factory(llm_temperature=0.35),
        trace_writer=TraceWriter(trace_dir, 10),
    )

    await processor.process(
        AUDIO,
        request_id="req-full",
        client_id="vox-idea",
        client_version="0.1.0",
        audio_seconds=1.25,
    )
    doc = only_trace(trace_dir)

    assert set(doc) == TRACE_KEYS
    assert doc["request_id"] == "req-full"
    assert datetime.fromisoformat(doc["started_at"]).utcoffset() == UTC.utcoffset(None)
    assert doc["endpoint"] == "process"
    assert doc["status"] == "ok"
    assert doc["error"] is None
    assert doc["client"] == {"id": "vox-idea", "version": "0.1.0", "audio_seconds": 1.25}
    assert doc["audio"] == {"bytes": len(AUDIO), "decoded_seconds": 1.5}
    assert doc["stt"]["model"] == "fake-whisper"
    assert doc["stt"]["device"] == "cpu"
    assert doc["stt"]["compute_type"] == "int8"
    assert doc["stt"]["language"] == "ru"
    assert doc["stt"]["language_probability"] == pytest.approx(0.99)
    assert doc["stt"]["duration_ms"] >= 0
    assert doc["stt"]["transcript"] == TRANSCRIPT
    assert doc["glossary"] == {"entries": 1, "prompt_block_chars": len("мембершип -> membership")}
    assert doc["prompt"]["kind"] == "task"
    assert doc["prompt"]["system"] == "Ты редактор."
    assert doc["prompt"]["system_chars"] == len(doc["prompt"]["system"])
    assert doc["prompt"]["user_chars"] == len(doc["prompt"]["user"])
    assert TRANSCRIPT in doc["prompt"]["user"]
    assert doc["llm"]["model"] == "fake-llm"
    assert doc["llm"]["base_url"] == "http://llm.test/v1"
    assert doc["llm"]["temperature"] == pytest.approx(0.35)
    assert doc["llm"]["duration_ms"] >= 0
    assert doc["llm"]["raw_output"] == REPLY
    assert doc["llm"]["cleaned_output"] == REPLY
    assert doc["output"] == {"chars": len(REPLY), "fell_back_to_transcript": False, "text": REPLY}
    assert set(doc["timings_ms"]) == {"transcription", "llm", "total"}
    assert doc["timings_ms"]["total"] >= doc["timings_ms"]["transcription"]


async def test_the_trace_records_dictation_as_the_prompt_kind(
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    trace_dir: Path,
) -> None:
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(REPLY),
        trace_writer=TraceWriter(trace_dir, 10),
    )

    await processor.process(AUDIO, request_id="req-dict-kind", dictation=True)
    doc = only_trace(trace_dir)

    assert doc["prompt"]["kind"] == "dictation"


async def test_the_trace_records_a_fallback_to_the_transcript(
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    trace_dir: Path,
) -> None:
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(""),
        trace_writer=TraceWriter(trace_dir, 10),
    )

    await processor.process(AUDIO, request_id="req-fallback")
    doc = only_trace(trace_dir)

    assert doc["status"] == "ok"
    assert doc["output"]["text"] == TRANSCRIPT
    assert doc["output"]["fell_back_to_transcript"] is True


async def test_the_trace_is_utf8_json_that_keeps_russian_readable(
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    trace_dir: Path,
) -> None:
    """These files are opened by hand, so Cyrillic stays Cyrillic instead of escaping."""
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(REPLY),
        trace_writer=TraceWriter(trace_dir, 10),
    )

    await processor.process(AUDIO, request_id="req-utf8")
    raw = written(trace_dir)[0].read_text(encoding="utf-8")

    assert TRANSCRIPT in raw
    assert REPLY in raw
    assert "\\u04" not in raw
    assert raw.startswith("{\n  ")


async def test_the_trace_never_contains_the_api_key(
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    settings_factory: Callable[..., Settings],
    trace_dir: Path,
) -> None:
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(REPLY, base_url=CREDENTIALED_URL),
        settings=settings_factory(llm_api_key=API_KEY),
        trace_writer=TraceWriter(trace_dir, 10),
    )

    await processor.process(AUDIO, request_id="req-secret")
    raw = written(trace_dir)[0].read_text(encoding="utf-8")
    doc = json.loads(raw)

    assert API_KEY not in raw
    assert doc["llm"]["base_url"] == "http://ollama:11434/v1"
    assert "@" not in doc["llm"]["base_url"]


async def test_a_failing_request_is_still_traced(
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    trace_dir: Path,
) -> None:
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(raises=LlmTimeoutError("too slow")),
        trace_writer=TraceWriter(trace_dir, 10),
    )

    with pytest.raises(LlmTimeoutError):
        await processor.process(AUDIO, request_id="req-boom")
    doc = only_trace(trace_dir)

    assert doc["status"] == "error"
    assert doc["error"] == {"type": "LlmTimeoutError", "message": "too slow"}
    assert doc["stt"]["transcript"] == TRANSCRIPT
    assert doc["prompt"]["system"]
    assert doc["llm"]["raw_output"] == ""
    assert doc["output"] is None
    assert doc["timings_ms"]["total"] >= 0


async def test_an_error_trace_keeps_only_the_stages_that_ran(
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    trace_dir: Path,
) -> None:
    processor = processor_factory(
        transcriber_factory("   \n  "),
        llm_factory(REPLY),
        trace_writer=TraceWriter(trace_dir, 10),
    )

    with pytest.raises(EmptyTranscriptError):
        await processor.process(AUDIO, request_id="req-empty")
    doc = only_trace(trace_dir)

    assert doc["error"]["type"] == "EmptyTranscriptError"
    assert doc["stt"]["transcript"] == ""
    assert doc["prompt"] is None
    assert doc["llm"] is None
    assert doc["output"] is None


async def test_a_trace_that_cannot_be_written_never_fails_the_request(
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    trace_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A denied trace directory is a diagnostic problem, never a dictation problem."""
    monkeypatch.setattr(Path, "replace", denied)
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(REPLY),
        trace_writer=TraceWriter(trace_dir, 10),
    )

    with caplog.at_level(logging.WARNING):
        response = await processor.process(AUDIO, request_id="req-denied")

    assert response.output == REPLY
    assert written(trace_dir) == []
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert "could not write trace" in caplog.text


def test_the_writer_creates_its_directory_on_demand(tmp_path: Path) -> None:
    directory = tmp_path / "deep" / "traces"
    writer = TraceWriter(directory, 10)

    path = writer.write(make_trace("req-mkdir", second=1))

    assert path is not None
    assert path.parent == directory
    assert writer.directory == directory
    assert json.loads(path.read_text(encoding="utf-8"))["request_id"] == "req-mkdir"


def test_a_failed_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A reader watching the directory must never find a half-written trace."""
    monkeypatch.setattr(Path, "replace", denied)
    writer = TraceWriter(tmp_path, 10)

    with caplog.at_level(logging.WARNING):
        path = writer.write(make_trace("req-partial", second=2))

    assert path is None
    assert list(tmp_path.iterdir()) == []
    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 1


def test_pruning_keeps_the_newest_files_only(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path, 3)

    for index in range(1, 6):
        writer.write(make_trace(f"req-{index}", second=index))

    kept = [path.name for path in written(tmp_path)]
    assert len(kept) == 3
    assert [name.rpartition("-")[2] for name in kept] == ["3.json", "4.json", "5.json"]
    assert kept == sorted(kept)


def test_the_file_just_written_always_survives_the_prune(tmp_path: Path) -> None:
    writer = TraceWriter(tmp_path, 0)

    path = writer.write(make_trace("req-lonely", second=3))

    assert path is not None
    assert written(tmp_path) == [path]


@pytest.mark.parametrize(
    ("reply", "cleaned", "changed"),
    [
        ("<think>подумаю</think>\n```\nГотовый текст\n```", "Готовый текст", True),
        (REPLY, REPLY, False),
    ],
)
async def test_cleanup_changed_reports_whether_the_reply_was_rewritten(
    reply: str,
    cleaned: str,
    changed: bool,
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    trace_dir: Path,
) -> None:
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(reply),
        trace_writer=TraceWriter(trace_dir, 10),
    )

    await processor.process(AUDIO, request_id="req-clean")
    doc = only_trace(trace_dir)

    assert doc["llm"]["raw_output"] == reply
    assert doc["llm"]["cleaned_output"] == cleaned
    assert doc["llm"]["cleanup_changed"] is changed


async def test_an_oversized_project_is_traced_as_truncated(
    prompt_factory: PromptFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    settings_factory: Callable[..., Settings],
    trace_dir: Path,
) -> None:
    prompt = prompt_factory(system_prompt=SYSTEM_WITH_PROJECT)
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(REPLY),
        prompt,
        settings=settings_factory(max_project_bytes=64),
        trace_writer=TraceWriter(trace_dir, 10),
    )

    await processor.process(AUDIO, request_id="req-big", project=PROJECT, project_name="jigward")
    project = only_trace(trace_dir)["project"]

    assert project["name"] == "jigward"
    assert project["received_bytes"] == len(PROJECT.encode("utf-8"))
    assert project["used_bytes"] == len(project["text"].encode("utf-8"))
    assert project["used_bytes"] <= 64
    assert project["used_bytes"] < project["received_bytes"]
    assert project["truncated"] is True
    assert PROJECT.startswith(project["text"])


async def test_a_project_that_fits_is_not_flagged_as_truncated(
    prompt_factory: PromptFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    trace_dir: Path,
) -> None:
    prompt = prompt_factory(system_prompt=SYSTEM_WITH_PROJECT)
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(REPLY),
        prompt,
        trace_writer=TraceWriter(trace_dir, 10),
    )

    await processor.process(AUDIO, request_id="req-fits", project=PROJECT, project_name="jigward")
    project = only_trace(trace_dir)["project"]

    assert project["text"] == PROJECT
    assert project["received_bytes"] == project["used_bytes"]
    assert project["truncated"] is False


async def test_the_traced_prompt_keeps_the_project_in_the_system_message(
    prompt_factory: PromptFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    trace_dir: Path,
) -> None:
    """The trace must show the prompt split the prefix cache depends on, not a tidied one.

    Project context lives in the system prompt and the transcript stays last in the user
    message; a trace that showed them the other way round would send a reader chasing a cache
    regression that is not there.
    """
    prompt = prompt_factory(
        system_prompt=SYSTEM_WITH_PROJECT,
        user_template="Словарь:\n{glossary}\n\nРасшифровка:\n{transcript}",
    )
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(REPLY),
        prompt,
        trace_writer=TraceWriter(trace_dir, 10),
    )

    await processor.process(AUDIO, request_id="req-cache", project=PROJECT)
    prompt_doc = only_trace(trace_dir)["prompt"]

    assert PROJECT in prompt_doc["system"]
    assert PROJECT_HEADER in prompt_doc["system"]
    assert PROJECT_MARKER not in prompt_doc["user"]
    assert prompt_doc["user"].rstrip().endswith(TRANSCRIPT)


async def test_a_transform_trace_has_no_audio_or_stt_section(
    prompt_factory: PromptFactory,
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    trace_dir: Path,
) -> None:
    prompt = prompt_factory(system_prompt=SYSTEM_WITH_PROJECT)
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(REPLY),
        prompt,
        trace_writer=TraceWriter(trace_dir, 10),
    )

    await processor.transform_only(TRANSCRIPT, request_id="req-tx", project=PROJECT)
    doc = only_trace(trace_dir)

    assert set(doc) == TRACE_KEYS
    assert doc["endpoint"] == "transform"
    assert doc["audio"] is None
    assert doc["stt"] is None
    assert doc["project"]["text"] == PROJECT
    assert doc["prompt"]["kind"] == "task"
    assert doc["prompt"]["user"] == TRANSCRIPT
    assert doc["output"]["text"] == REPLY
    assert doc["timings_ms"]["transcription"] == 0


async def test_the_info_line_names_the_trace_file_it_wrote(
    processor_factory: ProcessorFactory,
    transcriber_factory: Fake,
    llm_factory: Fake,
    trace_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    processor = processor_factory(
        transcriber_factory(TRANSCRIPT),
        llm_factory(REPLY),
        trace_writer=TraceWriter(trace_dir, 10),
    )

    with caplog.at_level(logging.INFO):
        await processor.process(AUDIO, request_id="req-log")

    assert f"trace={written(trace_dir)[0].name}" in caplog.text
    assert TRANSCRIPT not in caplog.text


def test_a_configured_trace_directory_announces_itself_at_startup(
    settings_factory: Callable[..., Settings],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Writing transcripts to disk is loud by design, so the startup log has to say so."""
    settings = settings_factory(trace_dir=tmp_path / "traces", trace_keep=7)

    with caplog.at_level(logging.INFO):
        writer = build_trace_writer(settings)

    assert writer is not None
    assert writer.directory == tmp_path / "traces"
    assert len(caplog.records) == 1
    assert str(tmp_path / "traces") in caplog.text
    assert "7" in caplog.text


def test_the_trace_records_who_called_and_which_project_file(
    state_factory: StateFactory,
    app_factory: AppFactory,
    settings_factory: Callable[..., Settings],
    transcriber_factory: Fake,
    llm_factory: Fake,
    prompt_factory: PromptFactory,
    processor_factory: ProcessorFactory,
    trace_dir: Path,
) -> None:
    """End to end: what the route was told about the caller reaches the trace file."""
    settings = settings_factory(llm_api_key=API_KEY)
    transcriber = transcriber_factory(TRANSCRIPT, ready=True)
    llm = llm_factory(REPLY, base_url=CREDENTIALED_URL)
    prompt = prompt_factory(system_prompt=SYSTEM_WITH_PROJECT)
    state = state_factory(
        settings=settings,
        transcriber=transcriber,
        llm=llm,
        prompt=prompt,
        processor=processor_factory(
            transcriber,
            llm,
            prompt,
            settings=settings,
            trace_writer=TraceWriter(trace_dir, 10),
        ),
        llm_ready=True,
    )
    client = TestClient(app_factory(state))

    response = client.post(
        "/v1/process",
        files={"audio": ("audio.wav", WAV, "audio/wav")},
        data={
            "project": PROJECT,
            "project_name": "jigward",
            "client_id": "vox-idea",
            "client_version": "0.1.0",
            "audio_seconds": "1.25",
        },
    )
    doc = only_trace(trace_dir)

    assert response.status_code == 200
    assert doc["request_id"] == response.json()["request_id"]
    assert doc["client"] == {"id": "vox-idea", "version": "0.1.0", "audio_seconds": 1.25}
    assert doc["audio"]["bytes"] == len(WAV)
    assert doc["project"]["name"] == "jigward"
    assert doc["project"]["text"] == PROJECT
    assert PROJECT in doc["prompt"]["system"]
    assert doc["output"]["text"] == REPLY
    assert API_KEY not in written(trace_dir)[0].read_text(encoding="utf-8")
