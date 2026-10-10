from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Page:
    page: int = 1
    page_size: int = 20

    def __post_init__(self) -> None:
        if self.page < 1 or not 1 <= self.page_size <= 100:
            raise ValueError("page 从 1 开始，page_size 必须在 1—100 之间")


@dataclass(frozen=True)
class UserCreate:
    username: str
    display_name: str
    password_hash: str
    role: str = "viewer"


@dataclass(frozen=True)
class SessionCreate:
    id_hash: str
    user_id: str
    expires_at: datetime


@dataclass(frozen=True)
class EntityCreate:
    id: str
    name: str
    type: str
    region_code: str
    longitude: Decimal | None = None
    latitude: Decimal | None = None
    parent_entity_id: str | None = None
    location_note: str | None = None


@dataclass(frozen=True)
class MetricCreate:
    code: str
    display_name: str
    definition: str
    unit: str
    supported_entity_types: list[str]
    aggregation_rule: str
    comparability_note: str
    dictionary_version: str = "v0.1.0"


@dataclass(frozen=True)
class SourceCreate:
    id: str
    publisher: str
    title: str
    url: str
    usage_note: str
    update_frequency: str
    adapter_key: str = "standard_csv"
    native_grain: str | None = None
    verification_status: str = "pending_review"
    coverage_start: date | None = None
    coverage_end: date | None = None
    adapter_config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = False


@dataclass(frozen=True)
class AssetCreate:
    source_id: str
    url: str
    title: str
    retrieved_at: datetime
    relative_path: str
    sha256: str
    media_type: str
    byte_size: int
    usage_note: str
    published_at: datetime | None = None


@dataclass(frozen=True)
class ObservationInput:
    entity_id: str
    metric_code: str
    period_start: date
    period_end: date
    grain: str
    value: Decimal
    unit: str
    original_value: str
    original_unit: str
    conversion_factor: Decimal
    source_id: str
    asset_id: str
    source_locator: str
    methodology_version: str
    published_at: datetime | None = None
    quality_status: str = "verified"


@dataclass(frozen=True)
class SeriesQuery:
    entity_id: str
    metric: str
    grain: str
    start: date
    end: date
    quality: str = "verified"


@dataclass(frozen=True)
class JobCreate:
    type: str
    idempotency_key: str
    payload: dict[str, Any]
    reason: str
    requested_by: str | None = None
    payload_version: str = "v1"


@dataclass(frozen=True)
class JobLease:
    job_id: str
    attempt_no: int
    lease_token: str


@dataclass(frozen=True)
class CountInput:
    window_start: datetime
    window_end: datetime
    entries: int
    exits: int
    quality_status: str = "verified"


def dto_dict(value: Any) -> dict[str, Any]:
    """只用于内部测试和示例；D 仍负责正式 HTTP 序列化。"""
    return asdict(value)
