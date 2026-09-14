from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .analysis import Analysis


class TimingsMs(BaseModel):
    transcription: int = 0
    llm: int = 0
    total: int = 0


class TranscriptionResult(BaseModel):
    text: str
    language: str
    language_probability: float = 0.0
    audio_duration_s: float = 0.0
    duration_ms: int = 0


class ProcessResponse(BaseModel):
    request_id: str
    transcript: str
    output: str
    language: str
    timings_ms: TimingsMs
    analysis: Analysis | None = None


class TranscribeResponse(BaseModel):
    request_id: str
    transcript: str
    language: str
    timings_ms: TimingsMs


class TransformRequest(BaseModel):
    text: str
    dictation: bool = False
    project: str | None = None
    context: str | None = None
    language: str | None = None


class TransformResponse(BaseModel):
    request_id: str
    output: str
    timings_ms: TimingsMs
    analysis: Analysis | None = None


class SttHealth(BaseModel):
    ready: bool
    model: str
    device: str
    compute_type: str
    error: str | None = None


class LlmHealth(BaseModel):
    ready: bool
    model: str
    base_url: str
    error: str | None = None


class GpuHealth(BaseModel):
    cuda_available: bool
    device_count: int


class HealthResponse(BaseModel):
    status: Literal["ready", "warming", "degraded"]
    version: str
    uptime_s: float
    stt: SttHealth
    llm: LlmHealth
    gpu: GpuHealth


class LlmConfig(BaseModel):
    """The LLM connection as it stands. The API key is reported as set or not, never echoed."""

    base_url: str
    model: str
    api_key_set: bool
    warmup: bool
    overridden: bool
    ready: bool
    error: str | None = None


class LlmConfigUpdate(BaseModel):
    """A partial change. An omitted field is left alone; an empty api_key clears it.

    ``api_key`` is the one field where "" and ``None`` differ: a local server wants no bearer
    token at all, and that has to be expressible without deleting the whole override.
    """

    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    warmup: bool | None = None


class ErrorBody(BaseModel):
    error: str
    detail: str | None = None
