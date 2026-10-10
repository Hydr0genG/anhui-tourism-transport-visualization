from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedMixin, IdMixin, MutableMixin, utcnow_naive


ID = String(36)
CODE = String(48)
HASH = String(64)
MONEY = Numeric(20, 4)


class User(MutableMixin, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(CODE, nullable=False)
    status: Mapped[str] = mapped_column(CODE, default="active", nullable=False)


class Entity(MutableMixin, Base):
    __tablename__ = "entities"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(CODE, nullable=False)
    region_code: Mapped[str] = mapped_column(String(12), nullable=False)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    parent_entity_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"))
    status: Mapped[str] = mapped_column(CODE, default="active", nullable=False)
    location_note: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        Index("ix_entities_type_region_status", "type", "region_code", "status"),
        CheckConstraint("longitude IS NULL OR (longitude >= -180 AND longitude <= 180)", name="longitude_range"),
        CheckConstraint("latitude IS NULL OR (latitude >= -90 AND latitude <= 90)", name="latitude_range"),
    )


class Metric(Base):
    __tablename__ = "metrics"
    code: Mapped[str] = mapped_column(CODE, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(String(24), nullable=False)
    supported_entity_types: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    aggregation_rule: Mapped[str] = mapped_column(CODE, nullable=False)
    comparability_note: Mapped[str] = mapped_column(Text, nullable=False)
    dictionary_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, nullable=False)


class Source(MutableMixin, Base):
    __tablename__ = "sources"
    publisher: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    native_grain: Mapped[str | None] = mapped_column(CODE)
    usage_note: Mapped[str] = mapped_column(Text, nullable=False)
    verification_status: Mapped[str] = mapped_column(CODE, nullable=False)
    coverage_start: Mapped[date | None] = mapped_column(Date)
    coverage_end: Mapped[date | None] = mapped_column(Date)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime)
    update_frequency: Mapped[str] = mapped_column(CODE, nullable=False)
    adapter_key: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (Index("ix_sources_enabled_last_checked", "enabled", "last_checked_at"),)


class Job(IdMixin, CreatedMixin, Base):
    __tablename__ = "jobs"
    type: Mapped[str] = mapped_column(CODE, nullable=False)
    status: Mapped[str] = mapped_column(CODE, default="queued", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(HASH, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_version: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    processed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    imported_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result_refs: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_summary: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("type", "idempotency_key", name="type_idempotency"),
        Index("ix_jobs_claim", "status", "available_at", "created_at"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint("processed_count >= 0", name="processed_count_nonnegative"),
        CheckConstraint("imported_count >= 0", name="imported_count_nonnegative"),
    )


class SessionRecord(CreatedMixin, Base):
    __tablename__ = "sessions"
    id_hash: Mapped[str] = mapped_column(HASH, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (Index("ix_sessions_user_expires", "user_id", "expires_at"),)


class JobAttempt(IdMixin, CreatedMixin, Base):
    __tablename__ = "job_attempts"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(100), nullable=False)
    lease_token: Mapped[str] = mapped_column(ID, unique=True, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    outcome: Mapped[str | None] = mapped_column(CODE)
    error_summary: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no", name="job_attempt_no"),
        CheckConstraint("attempt_no > 0", name="attempt_no_positive"),
    )


class SourceAsset(IdMixin, CreatedMixin, Base):
    __tablename__ = "source_assets"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(HASH, nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usage_note: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        UniqueConstraint("source_id", "sha256", name="source_sha256"),
        CheckConstraint("byte_size >= 0", name="byte_size_nonnegative"),
    )


class IngestionRun(IdMixin, CreatedMixin, Base):
    __tablename__ = "ingestion_runs"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("job_attempts.id"))
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("source_assets.id"))
    status: Mapped[str] = mapped_column(CODE, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    normalizer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    processed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    imported_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revised_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quarantined_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        Index("ix_ingestion_runs_source_started", "source_id", "started_at"),
        CheckConstraint("processed_count >= 0", name="processed_count_nonnegative"),
        CheckConstraint("imported_count >= 0", name="imported_count_nonnegative"),
        CheckConstraint("revised_count >= 0", name="revised_count_nonnegative"),
        CheckConstraint("skipped_count >= 0", name="skipped_count_nonnegative"),
        CheckConstraint("quarantined_count >= 0", name="quarantined_count_nonnegative"),
    )


class Observation(MutableMixin, Base):
    __tablename__ = "observations"
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), nullable=False)
    metric_code: Mapped[str] = mapped_column(ForeignKey("metrics.code"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    grain: Mapped[str] = mapped_column(CODE, nullable=False)
    value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    unit: Mapped[str] = mapped_column(String(24), nullable=False)
    original_value: Mapped[str] = mapped_column(String(100), nullable=False)
    original_unit: Mapped[str] = mapped_column(String(24), nullable=False)
    conversion_factor: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("source_assets.id"), nullable=False)
    ingestion_run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id"), nullable=False)
    source_locator: Mapped[str] = mapped_column(String(500), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    quality_status: Mapped[str] = mapped_column(CODE, nullable=False)
    methodology_version: Mapped[str] = mapped_column(String(64), nullable=False)
    value_kind: Mapped[str] = mapped_column(CODE, default="observed", nullable=False)
    derivation_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint(
            "entity_id", "metric_code", "period_start", "period_end", "grain", "source_id",
            name="observation_business_key",
        ),
        Index("ix_observations_series", "entity_id", "metric_code", "grain", "period_start"),
        CheckConstraint("value >= 0", name="value_nonnegative"),
        CheckConstraint("period_end >= period_start", name="period_order"),
    )


class ObservationRevision(IdMixin, CreatedMixin, Base):
    __tablename__ = "observation_revisions"
    observation_id: Mapped[str] = mapped_column(ForeignKey("observations.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    superseded_by_run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id"), nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    __table_args__ = (UniqueConstraint("observation_id", "version", name="observation_version"),)


class SeriesPolicy(IdMixin, CreatedMixin, Base):
    __tablename__ = "series_policies"
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), nullable=False)
    metric_code: Mapped[str] = mapped_column(ForeignKey("metrics.code"), nullable=False)
    grain: Mapped[str] = mapped_column(CODE, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[date] = mapped_column(Date, nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    __table_args__ = (
        Index("ix_series_policies_lookup", "entity_id", "metric_code", "grain", "valid_from", "valid_until"),
        CheckConstraint("valid_until >= valid_from", name="valid_period_order"),
    )


class QualityIssue(MutableMixin, Base):
    __tablename__ = "quality_issues"
    run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id"), nullable=False)
    observation_id: Mapped[str | None] = mapped_column(ForeignKey("observations.id"))
    row_locator: Mapped[str | None] = mapped_column(String(300))
    code: Mapped[str] = mapped_column(CODE, nullable=False)
    severity: Mapped[str] = mapped_column(CODE, nullable=False)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(CODE, default="open", nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution_note: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("ix_quality_issues_run_status", "run_id", "status"),)


class Holiday(MutableMixin, Base):
    __tablename__ = "holidays"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(CODE, nullable=False)
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    days_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    region_code: Mapped[str] = mapped_column(String(12), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(200), nullable=False)
    definition_source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    definition_asset_id: Mapped[str] = mapped_column(ForeignKey("source_assets.id"), nullable=False)
    __table_args__ = (
        UniqueConstraint("kind", "year", "region_code", "scope_key", name="holiday_scope"),
        CheckConstraint("end_date >= start_date", name="date_order"),
        CheckConstraint("days_count > 0", name="days_positive"),
    )


class Event(MutableMixin, Base):
    __tablename__ = "events"
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(CODE, nullable=False)
    authority: Mapped[str] = mapped_column(CODE, nullable=False)
    severity: Mapped[str] = mapped_column(CODE, nullable=False)
    region_code: Mapped[str] = mapped_column(String(12), nullable=False)
    status: Mapped[str] = mapped_column(CODE, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("source_assets.id"), nullable=False)
    operator_note: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        Index("ix_events_region_status_starts", "region_code", "status", "starts_at"),
        CheckConstraint("ends_at IS NULL OR ends_at >= starts_at", name="time_order"),
    )


class EventEntity(Base):
    __tablename__ = "event_entities"
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), primary_key=True)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), primary_key=True)
    __table_args__ = (Index("ix_event_entities_entity_event", "entity_id", "event_id"),)


class EventHistory(IdMixin, CreatedMixin, Base):
    __tablename__ = "event_history"
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    change_note: Mapped[str] = mapped_column(Text, nullable=False)
    operator_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    __table_args__ = (UniqueConstraint("event_id", "version", name="event_version"),)


class KnowledgeDocument(MutableMixin, Base):
    __tablename__ = "knowledge_documents"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("source_assets.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    publisher: Mapped[str] = mapped_column(String(200), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime)
    permission_note: Mapped[str] = mapped_column(Text, nullable=False)
    document_version: Mapped[str] = mapped_column(String(64), nullable=False)
    index_status: Mapped[str] = mapped_column(CODE, default="pending", nullable=False)
    active_index_version: Mapped[str | None] = mapped_column(String(64))


class DocumentEntity(Base):
    __tablename__ = "document_entities"
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id"), primary_key=True)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), primary_key=True)


class KnowledgeIndex(IdMixin, CreatedMixin, Base):
    __tablename__ = "knowledge_indexes"
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id"), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    status: Mapped[str] = mapped_column(CODE, nullable=False)
    artifact_path: Mapped[str | None] = mapped_column(String(500))
    artifact_sha256: Mapped[str | None] = mapped_column(HASH)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="document_index_version"),
        CheckConstraint("chunk_count >= 0", name="chunk_count_nonnegative"),
    )


class KnowledgeChunk(IdMixin, CreatedMixin, Base):
    __tablename__ = "knowledge_chunks"
    index_id: Mapped[str] = mapped_column(ForeignKey("knowledge_indexes.id"), nullable=False)
    chunk_no: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    text_sha256: Mapped[str] = mapped_column(HASH, nullable=False)
    __table_args__ = (
        UniqueConstraint("index_id", "chunk_no", name="index_chunk_no"),
        CheckConstraint("chunk_no >= 0", name="chunk_no_nonnegative"),
    )


class PermissionRecord(MutableMixin, Base):
    __tablename__ = "permission_records"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    grantor: Mapped[str] = mapped_column(String(200), nullable=False)
    evidence_path: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(HASH, nullable=False)
    allowed_uses: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime)
    playback_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(CODE, nullable=False)


class Video(MutableMixin, Base):
    __tablename__ = "videos"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    asset_id: Mapped[str] = mapped_column(ForeignKey("source_assets.id"), nullable=False)
    scenic_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), nullable=False)
    camera_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), nullable=False)
    permission_record_id: Mapped[str] = mapped_column(ForeignKey("permission_records.id"), nullable=False)
    recorded_start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    recorded_end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(CODE, default="uploaded", nullable=False)
    camera_location_note: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        Index("ix_videos_location_time", "scenic_id", "camera_id", "recorded_start_at"),
        CheckConstraint("recorded_end_at > recorded_start_at", name="recorded_time_order"),
    )


class VideoAnalysis(IdMixin, CreatedMixin, Base):
    __tablename__ = "video_analyses"
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True, nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(64), nullable=False)
    detector_model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    detector_sha256: Mapped[str] = mapped_column(HASH, nullable=False)
    openvino_version: Mapped[str] = mapped_column(String(64), nullable=False)
    device: Mapped[str] = mapped_column(CODE, nullable=False)
    tracker_config_id: Mapped[str | None] = mapped_column(String(100))
    tracker_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    count_line: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    entry_side: Mapped[str] = mapped_column(CODE, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    config_sha256: Mapped[str] = mapped_column(HASH, nullable=False)
    status: Mapped[str] = mapped_column(CODE, nullable=False)
    manual_validation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("video_id", "config_sha256", name="video_analysis_config"),
        CheckConstraint("interval_minutes IN (1, 5, 15)", name="interval_allowed"),
    )


class CameraCount(IdMixin, CreatedMixin, Base):
    __tablename__ = "camera_counts"
    analysis_id: Mapped[str] = mapped_column(ForeignKey("video_analyses.id"), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    entries: Mapped[int] = mapped_column(Integer, nullable=False)
    exits: Mapped[int] = mapped_column(Integer, nullable=False)
    quality_status: Mapped[str] = mapped_column(CODE, nullable=False)
    __table_args__ = (
        UniqueConstraint("analysis_id", "window_start", "window_end", name="analysis_window"),
        Index("ix_camera_counts_analysis_start", "analysis_id", "window_start"),
        CheckConstraint("window_end > window_start", name="window_order"),
        CheckConstraint("entries >= 0", name="entries_nonnegative"),
        CheckConstraint("exits >= 0", name="exits_nonnegative"),
    )


class ManualAnnotation(IdMixin, CreatedMixin, Base):
    __tablename__ = "manual_annotations"
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    camera_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), nullable=False)
    sample_key: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    entries: Mapped[int] = mapped_column(Integer, nullable=False)
    exits: Mapped[int] = mapped_column(Integer, nullable=False)
    annotator: Mapped[str] = mapped_column(String(100), nullable=False)
    direction_note: Mapped[str] = mapped_column(Text, nullable=False)
    review_status: Mapped[str] = mapped_column(CODE, nullable=False)
    __table_args__ = (
        UniqueConstraint("video_id", "sample_key", "version", name="video_sample_version"),
        CheckConstraint("window_end > window_start", name="annotation_window_order"),
        CheckConstraint("entries >= 0", name="annotation_entries_nonnegative"),
        CheckConstraint("exits >= 0", name="annotation_exits_nonnegative"),
    )


class ForecastRun(IdMixin, CreatedMixin, Base):
    __tablename__ = "forecast_runs"
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), nullable=False)
    metric_code: Mapped[str] = mapped_column(ForeignKey("metrics.code"), nullable=False)
    grain: Mapped[str] = mapped_column(CODE, nullable=False)
    training_start: Mapped[date] = mapped_column(Date, nullable=False)
    training_end: Mapped[date] = mapped_column(Date, nullable=False)
    horizon: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_sha256: Mapped[str] = mapped_column(HASH, nullable=False)
    backtest: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(CODE, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("training_end >= training_start", name="training_period_order"),
        CheckConstraint("horizon > 0", name="horizon_positive"),
    )


class ForecastPoint(IdMixin, CreatedMixin, Base):
    __tablename__ = "forecast_points"
    run_id: Mapped[str] = mapped_column(ForeignKey("forecast_runs.id"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    lower_bound: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    upper_bound: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    __table_args__ = (
        UniqueConstraint("run_id", "period_start", "period_end", name="forecast_period"),
        CheckConstraint("period_end >= period_start", name="forecast_period_order"),
        CheckConstraint("lower_bound <= value AND value <= upper_bound", name="forecast_bounds"),
        CheckConstraint("lower_bound >= 0", name="forecast_nonnegative"),
    )


class ShortForecastRun(IdMixin, CreatedMixin, Base):
    __tablename__ = "short_forecast_runs"
    analysis_id: Mapped[str] = mapped_column(ForeignKey("video_analyses.id"), nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    direction: Mapped[str] = mapped_column(CODE, nullable=False)
    horizon_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    step_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_sha256: Mapped[str] = mapped_column(HASH, nullable=False)
    validation_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(CODE, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("horizon_minutes IN (15, 30, 60)", name="short_horizon_allowed"),
        CheckConstraint("step_minutes IN (5, 15)", name="short_step_allowed"),
    )


class ShortForecastPoint(IdMixin, CreatedMixin, Base):
    __tablename__ = "short_forecast_points"
    run_id: Mapped[str] = mapped_column(ForeignKey("short_forecast_runs.id"), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    lower_bound: Mapped[Decimal | None] = mapped_column(MONEY)
    upper_bound: Mapped[Decimal | None] = mapped_column(MONEY)
    __table_args__ = (
        UniqueConstraint("run_id", "window_start", "window_end", name="short_forecast_window"),
        CheckConstraint("window_end > window_start", name="short_window_order"),
        CheckConstraint("value >= 0", name="short_value_nonnegative"),
        CheckConstraint(
            "(lower_bound IS NULL AND upper_bound IS NULL) OR "
            "(lower_bound IS NOT NULL AND upper_bound IS NOT NULL AND lower_bound <= value AND value <= upper_bound)",
            name="short_bounds",
        ),
    )
