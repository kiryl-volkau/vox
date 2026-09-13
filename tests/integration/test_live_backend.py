"""End-to-end checks against a backend that is actually running.

Skipped unless VOICE_CODE_E2E=1, and deselected by default through the ``integration``
marker, so a normal ``pytest`` run never needs Docker, a GPU or the network.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import httpx
import pytest

from vox_client.api_client import ApiError, VoiceCodeClient
from vox_client.config import ServerConfig

BASE_URL = os.environ.get("VOICE_CODE_E2E_URL", "http://127.0.0.1:8765")
MODE = os.environ.get("VOICE_CODE_E2E_MODE", "dictation")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("VOICE_CODE_E2E") != "1",
        reason="set VOICE_CODE_E2E=1 and start the backend to run the end-to-end tests",
    ),
]


@pytest.fixture
def live_client() -> Iterator[VoiceCodeClient]:
    client = VoiceCodeClient(ServerConfig(base_url=BASE_URL, timeout_seconds=180.0))
    try:
        yield client
    finally:
        client.close()


def test_health_reports_a_ready_backend() -> None:
    response = httpx.get(f"{BASE_URL}/health", timeout=10.0)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ready", body
    assert body["stt"]["ready"] is True
    assert body["llm"]["ready"] is True
    assert "@" not in body["llm"]["base_url"]


def test_modes_are_served() -> None:
    body = httpx.get(f"{BASE_URL}/v1/modes", timeout=10.0).json()
    names = {mode["name"] for mode in body["modes"]}

    assert {"clean", "context", "dictation", "task"} <= names


def test_a_generated_wav_survives_the_whole_pipeline(
    live_client: VoiceCodeClient, wav_factory: Callable[..., bytes]
) -> None:
    """A synthetic tone must travel the whole upload -> decode -> STT path.

    A tone carries no speech, so the VAD legitimately leaves nothing to transcribe and the
    backend answers 422 empty_transcript. Either that or a real result proves the pipeline
    ran; only a different failure means something is broken. Asserting a transcript here
    would need a speech fixture, which this repository deliberately does not ship.
    """
    wav = wav_factory(seconds=2.0, sample_rate=16000, frequency=180.0, amplitude=0.2)

    try:
        result = live_client.process(wav, MODE, audio_seconds=2.0)
    except ApiError as exc:
        assert exc.code == "empty_transcript", f"unexpected backend failure: {exc.code}"
        return

    assert result.request_id
    assert result.mode == MODE
    assert isinstance(result.output, str)
    assert result.server_ms.get("total", 0) > 0


def test_an_unknown_mode_is_refused(live_client: VoiceCodeClient) -> None:
    wav_header = b"RIFF----WAVEfmt "

    with pytest.raises(ApiError) as excinfo:
        live_client.process(wav_header, "definitely-not-a-mode", audio_seconds=0.1)

    assert excinfo.value.code in {"unknown_mode", "empty_transcript", "stt_unavailable"}
