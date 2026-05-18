"""
schemas.py — Pydantic models for API request/response validation.

RCAReport and related models are carried forward unchanged from
rca_2/backend/schemas.py — they define the canonical shape of an RCA report
stored in pgvector and displayed in the UI.

New models (RCAProcessRequest, BatchProcessRequest, etc.) are added for the
new FastAPI endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_serializer, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Existing RCA report models (unchanged from rca_2/backend/schemas.py)
# ─────────────────────────────────────────────────────────────────────────────

class RCAMetadata(BaseModel):
    category: str = "unknown"
    error_code: int = 500
    tags: List[str] = Field(default_factory=list)


class ResolutionSteps(BaseModel):
    immediate_mitigation: List[str] = Field(default_factory=list)
    preventative_action: List[str] = Field(default_factory=list)


class RCAReport(BaseModel):
    timestamp: datetime
    raw_error: str
    root_cause: str
    fix_steps: List[str] = Field(default_factory=list)
    also_try_steps: List[str] = Field(default_factory=list)
    metadata: RCAMetadata
    incident_summary: Optional[str] = None
    causal_map: Optional[List[str]] = None
    verified_in_code: Optional[bool] = None
    resolution_steps: Optional[ResolutionSteps] = None
    topic: Optional[str] = None
    app_id: Optional[str] = None
    error_type: Optional[str] = None
    cleaned_log: Optional[str] = None
    log_pill: Optional[Dict[str, Any]] = None
    # Updated: "splunk" replaces "elk" as a valid source type
    source_type: Optional[str] = None  # splunk | loki | unified
    timestamp_window: Optional[str] = None

    @field_serializer("timestamp")
    def _serialize_timestamp(self, v: datetime) -> str:
        return v.isoformat(timespec="milliseconds")

    @model_validator(mode="after")
    def ensure_fix_steps(self) -> "RCAReport":
        if not self.fix_steps and self.resolution_steps:
            return self.model_copy(update={
                "fix_steps": list(self.resolution_steps.immediate_mitigation)
                + list(self.resolution_steps.preventative_action)
            })
        return self


# ─────────────────────────────────────────────────────────────────────────────
# New request/response models for v2 API
# ─────────────────────────────────────────────────────────────────────────────

class RCAProcessRequest(BaseModel):
    """Request body for POST /api/rca/process (interactive RCA pipeline)."""
    app_id: str
    since_seconds: int = Field(default=3600, ge=60, le=86400)
    source: str = Field(default="splunk")  # "splunk" | "loki" | "unified"
    skip_vector_check: bool = False         # True → bypass similarity check, force new LLM analysis


class RCAAcceptRequest(BaseModel):
    """Request body for POST /api/rca/accept."""
    report: Dict[str, Any]
    embedding: List[float]
    app_id: str = "default"
    embed_source: str = ""


class RCARejectRequest(BaseModel):
    """Request body for POST /api/rca/reject."""
    incident_id: str
    app_id: str = "default"
    reason: str = ""


class RCARerunRequest(BaseModel):
    """Rerun LLM on existing cleaned logs, optionally excluding previous steps."""
    incident_id: str
    cleaned_lines: List[str]
    app_id: str = "default"
    excluded_steps: List[str] = Field(default_factory=list)


class BatchProcessRequest(BaseModel):
    """Request body for POST /api/batch/process."""
    app_id: str
    since_seconds: int = Field(default=3600, ge=60)
    threads: int = Field(default=8, ge=1, le=32)
    max_chunks: int = Field(default=40, ge=1)
    source: str = "splunk"


class AppUpsertRequest(BaseModel):
    """Request body for POST /api/apps."""
    app_id: str
    app_name: str
    service_name: str
    port: Optional[int] = None
    container_name: Optional[str] = None
    vector_category: str = "default"
    source_config: Dict[str, Any] = Field(default_factory=dict)
    enabled_sources: List[str] = Field(default_factory=lambda: ["splunk"])


class LogsRequest(BaseModel):
    """Query parameters model for GET /api/logs."""
    app_id: str
    since_seconds: int = 3600
    source: str = "splunk"
    max_events: int = 1000


class VectorSearchRequest(BaseModel):
    """Query parameters for GET /api/vectordb/search."""
    query: str
    app_id: str = "default"
    n_results: int = Field(default=5, ge=1, le=50)


class ErrorTriggerRequest(BaseModel):
    """Request body for POST /api/errors/trigger."""
    app_id: str
    error_type: str = "DB_AUTH"
    count: int = Field(default=1, ge=1, le=50)
