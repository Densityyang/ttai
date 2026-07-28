"""Deterministic, capped sampling for non-executing shadow plans."""

from __future__ import annotations

import hashlib
from uuid import UUID


def is_shadow_sample(request_id: UUID, *, percentage: int) -> bool:
    """Select at most ``percentage`` percent of requests without mutable counters."""
    if not 0 <= percentage <= 5:
        raise ValueError("shadow traffic percentage must be between 0 and 5")
    bucket = int.from_bytes(hashlib.sha256(request_id.bytes).digest()[:4], "big") % 100
    return bucket < percentage
