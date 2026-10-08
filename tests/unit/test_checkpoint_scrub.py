"""P2-S2 shared checkpoint scrubber."""

from __future__ import annotations

from src.nl2sql.observability.content_policy import scrub_text, scrub_value

CONFIGURED_SECRET = "sk-live-AAAABBBBCCCCDDDD"


def test_scrub_value_removes_configured_values_and_shapes() -> None:
    payload = {
        "authorization_revision": "rev-A",
        "model_receipt": {
            "content": "revenue is 100 and password = 'hunter2'",
            "matched_categories": [],
        },
        "business": ["team A revenue", {"note": "Bearer abcdefghijkl"}],
        "configured": "value " + CONFIGURED_SECRET,
    }

    scrubbed = scrub_value(payload, secret_values=[CONFIGURED_SECRET])
    rendered = str(scrubbed)

    assert CONFIGURED_SECRET not in rendered
    assert "hunter2" not in rendered
    assert "abcdefghijkl" not in rendered
    assert scrubbed["business"][0] == "team A revenue"
    assert "revenue is 100" in scrubbed["model_receipt"]["content"]
    assert scrubbed["authorization_revision"] == "rev-A"


def test_scrub_value_preserves_structure() -> None:
    payload = {"a": [1, "password: x"], "b": ("token=y",), "c": {"d": None}}

    scrubbed = scrub_value(payload)

    assert set(scrubbed) == set(payload)
    assert isinstance(scrubbed["b"], tuple)
    assert scrubbed["c"] == {"d": None}


def test_pii_shaped_business_values_are_not_scrubbed() -> None:
    text = "id 110101199001011234 phone 13800138000 team A revenue 1200"

    assert scrub_text(text) == text
