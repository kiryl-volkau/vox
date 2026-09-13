from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


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
    mode: str
    transcript: str
    normalized_text: str
    output: str
    language: str
    timings_ms: TimingsMs


class TranscribeResponse(BaseModel):
    request_id: str
    transcript: str
    language: str
    timings_ms: TimingsMs


class TransformRequest(BaseModel):
    text: str
    mode: str = "clean"
    project: str | None = None


class TransformResponse(BaseModel):
    request_id: str
    mode: str
    normalized_text: str
    output: str
    timings_ms: TimingsMs


class ModeInfo(BaseModel):
    name: str
    label: str
    description: str
    requires_llm: bool
    wrap_for_claude: bool


class ModesResponse(BaseModel):
    modes: list[ModeInfo]


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


class ErrorBody(BaseModel):
    error: str
    detail: str | None = None
