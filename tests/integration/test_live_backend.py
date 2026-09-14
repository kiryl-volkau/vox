"""End-to-end checks against a backend that is actually running.

Skipped unless VOICE_CODE_E2E=1, and deselected by default through the ``integration``
marker, so a normal ``pytest`` run never needs Docker, a GPU or the network.

These talk to the HTTP API directly rather than through a client library, because the API is
the contract the plugin depends on and the only thing worth pinning here.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import httpx
import pytest

BASE_URL = os.environ.get("VOICE_CODE_E2E_URL", "http://127.0.0.1:8765").rstrip("/")
TIMEOUT = 180.0

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("VOICE_CODE_E2E") != "1",
        reason="set VOICE_CODE_E2E=1 and start the backend to run the end-to-end tests",
    ),
]


def test_health_reports_a_ready_backend() -> None:
    response = httpx.get(f"{BASE_URL}/health", timeout=10.0)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ready", body
    assert body["stt"]["ready"] is True
    assert body["llm"]["ready"] is True
    assert "@" not in body["llm"]["base_url"]


def test_a_generated_wav_survives_the_whole_pipeline(
    wav_factory: Callable[..., bytes],
) -> None:
    """A synthetic tone must travel the whole upload -> decode -> STT -> prompt path.

    A tone carries no speech, so the VAD legitimately leaves nothing to transcribe and the
    backend answers 422 empty_transcript. Either that or a real result proves the pipeline
    ran; only a different failure means something is broken. Asserting a transcript here
    would need a speech fixture, which this repository deliberately does not ship.
    """
    wav = wav_factory(seconds=2.0, sample_rate=16000, frequency=180.0, amplitude=0.2)

    response = httpx.post(
        f"{BASE_URL}/v1/process",
        files={"audio": ("speech.wav", wav, "audio/wav")},
        data={"client_id": "integration-test", "audio_seconds": "2.0"},
        timeout=TIMEOUT,
    )
    body = response.json()

    if response.status_code == 422:
        assert body.get("error") == "empty_transcript", f"unexpected failure: {body}"
        return

    assert response.status_code == 200, body
    assert body["request_id"]
    assert isinstance(body["output"], str)
    assert body["timings_ms"]["total"] > 0


def test_replaying_text_runs_the_prompt_without_a_microphone() -> None:
    """/v1/transform is what the plugin's bench uses, and the only LLM-only entry point."""
    response = httpx.post(
        f"{BASE_URL}/v1/transform",
        json={"text": "посмотри этот сервис тут мембершип второй раз достается"},
        timeout=TIMEOUT,
    )
    body = response.json()

    assert response.status_code == 200, body
    assert body["output"].strip()
    assert body["timings_ms"]["llm"] > 0
