from __future__ import annotations

from typing import Any


class DataFoundationError(Exception):
    code = "data_foundation_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class InvalidArgument(DataFoundationError):
    code = "invalid_argument"


class NotFound(DataFoundationError):
    code = "not_found"


class Conflict(DataFoundationError):
    code = "conflict"


class UnsupportedGrain(DataFoundationError):
    code = "unsupported_grain"


class MetricEntityMismatch(DataFoundationError):
    code = "metric_entity_mismatch"


class InvalidPermission(DataFoundationError):
    code = "invalid_permission"


class AnalysisNotReady(DataFoundationError):
    code = "analysis_not_ready"


class QueryLimitExceeded(DataFoundationError):
    code = "query_limit_exceeded"


class LeaseLost(DataFoundationError):
    code = "lease_lost"
