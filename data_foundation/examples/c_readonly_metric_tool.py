"""C-owned reference adapter for D to bind to E; not an HTTP implementation.

Only one caller-authorized entity and complete native calendar periods are
accepted. C SDK/package/schema remain unchanged. D owns user authorization,
context mapping (public station -> C airport/rail_station), and HTTP wiring.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from data_foundation import DataFoundationError, SeriesQuery, UnsupportedGrain

BUSINESS_ZONE = timezone(timedelta(hours=8))
CATALOG = {"airport_throughput": "airport", "rail_departures": "rail_station", "scenic_visits": "scenic"}
QUERY_KEYS = {"metric", "entity_type", "entity_ids", "grain", "period_start", "period_end"}


def unavailable(reason):
    return {"status": "metric_tool_unavailable", "reason": reason,
            "queried_at": datetime.now(timezone.utc).isoformat()}


def calendar_bounds(query):
    """External interval is [start,end); C SeriesQuery uses inclusive dates."""
    bounds = []
    for field in ("period_start", "period_end"):
        value = query.get(field)
        if not isinstance(value, str):
            raise ValueError("Timezone-aware ISO bounds required")
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is None:
            raise ValueError("Timezone required")
        local = stamp.astimezone(BUSINESS_ZONE)
        if any((local.hour, local.minute, local.second, local.microsecond)):
            raise ValueError("Complete business calendar periods required")
        bounds.append(local)
    start, end = bounds
    if not start < end or end - start > timedelta(days=366 * 5):
        raise ValueError("Invalid or excessive period")
    grain = query["grain"]
    if grain == "month" and (start.day != 1 or end.day != 1):
        raise ValueError("Partial month refused")
    if grain == "week" and (start.weekday() != 0 or end.weekday() != 0):
        raise ValueError("Partial week refused")
    return start, end


def build_metric_tool(store, *, authorized_entity_ids):
    """Return metric_tool(query)->dict with an explicit server-side allowlist.

    Zero verified values are valid data; missing coverage is not zero. Never
    falls back to another grain, candidate facts, documents or old snapshots.
    """
    allowed = frozenset(str(UUID(str(value))) for value in authorized_entity_ids)

    def metric_tool(query):
        if not isinstance(query, dict) or set(query) - QUERY_KEYS:
            return unavailable("invalid_query")
        metric = query.get("metric")
        if not isinstance(metric, str) or metric not in CATALOG:
            return unavailable("unsupported_metric")
        if not isinstance(query.get("grain"), str) or query["grain"] not in {"day", "week", "month"}:
            return unavailable("unsupported_grain")
        ids = query.get("entity_ids")
        if not isinstance(ids, list) or len(ids) != 1:
            return unavailable("explicit_single_entity_required")
        try:
            entity_id = str(UUID(str(ids[0])))
        except (ValueError, TypeError, AttributeError):
            return unavailable("invalid_entity")
        if entity_id not in allowed:
            return unavailable("invalid_entity")  # Do not disclose existence.
        # D's public 'station' is a category, not C's entity type.
        expected = CATALOG[metric]
        accepted_types = {expected, "station"} if expected in {"airport", "rail_station"} else {expected}
        if not isinstance(query.get("entity_type"), str) or query["entity_type"] not in accepted_types:
            return unavailable("invalid_entity")
        try:
            start, end = calendar_bounds(query)
        except (ValueError, TypeError, OverflowError, KeyError):
            return unavailable("invalid_period")
        try:
            with store.read() as uow:
                result = uow.observations.query_series(SeriesQuery(entity_id, metric, query["grain"],
                    start.date(), end.date() - timedelta(days=1)))
        except UnsupportedGrain:
            return unavailable("unsupported_grain")
        except (DataFoundationError, SQLAlchemyError):
            return unavailable("data_source_unavailable")
        if result["entity"]["type"] != expected:
            return unavailable("invalid_entity")
        if result["missing_periods"] or result["coverage_ratio"] != 1 or not result["points"]:
            return unavailable("data_insufficient")
        points = result["points"]
        if any(p["unit"] != "人次" or not p["source_ids"] or p["quality_status"] != "verified" for p in points):
            return unavailable("data_source_unavailable")
        sources = sorted({sid for p in points for sid in p["source_ids"]})
        return {"status": "answered", "metric": metric,
            "value": str(sum((Decimal(p["value"]) for p in points), Decimal(0))),
            "unit": "人次", "entity_type": expected, "entity_ids": [entity_id],
            "grain": query["grain"], "period_start": start.isoformat(), "period_end": end.isoformat(),
            "queried_at": datetime.now(timezone.utc).isoformat(), "source_ids": sources,
            "aggregation_rule": "sum_complete_non_overlapping_native_periods_single_entity",
            "sample_count": len(points)}

    return metric_tool
