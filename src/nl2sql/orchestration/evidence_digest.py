"""Canonical, type-preserving digests for request-local execution evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


def _canonical_value(value: Any) -> Any:
    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite float cannot be hashed")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal cannot be hashed")
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, UUID):
        return {"$uuid": str(value)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("mapping evidence keys must be strings")
        return {
            key: _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise TypeError(f"unsupported evidence value: {type(value).__name__}")


def canonical_json_digest(value: Any) -> str:
    payload = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_rowset_digest(
    rows: Sequence[Mapping[str, Any]],
    *,
    result_columns: Sequence[str] | None = None,
    logical_columns: Mapping[str, str] | None = None,
) -> str:
    """Hash columns and rows in returned order; never sort business rows."""

    if any(not isinstance(row, Mapping) for row in rows):
        raise TypeError("rowset must contain mappings")
    if any(
        any(not isinstance(key, str) for key in row)
        for row in rows
    ):
        raise ValueError("rowset columns must be strings")
    columns = tuple(result_columns or (str(key) for key in rows[0])) if rows else tuple(result_columns or ())
    if any(tuple(str(key) for key in row) != columns for row in rows):
        raise ValueError("rowset columns or order do not match result columns")
    payload = {
        "columns": columns,
        "logical_columns": dict(sorted((logical_columns or {}).items())),
        "rows": [
            [_canonical_row_value(row[column]) for column in columns]
            for row in rows
        ],
    }
    return canonical_json_digest(payload)


def _canonical_row_value(value: Any) -> Any:
    """Retain scalar/container type boundaries inside row cells."""

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal cannot be hashed")
        return {"$row_decimal": str(value)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("mapping evidence keys must be strings")
        return {
            "$row_mapping": {
                key: _canonical_row_value(item)
                for key, item in sorted(value.items())
            }
        }
    if isinstance(value, (list, tuple)):
        return {"$row_list": [_canonical_row_value(item) for item in value]}
    return _canonical_value(value)

