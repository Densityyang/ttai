"""P2-S2 ModelInputPolicy: destination, secret denial, checksum, readiness."""

from __future__ import annotations

import pytest

from src.nl2sql.contracts import ModelRequest
from src.nl2sql.infra.llm.model_input_policy import (
    MODEL_INPUT_SECRET_DETECTED,
    MODEL_TARGET_NOT_APPROVED,
    ApprovedDestination,
    ModelInputPolicy,
    ModelInputPolicyUncalibrated,
    bootstrap_model_input_policy,
    calibrated_model_input_policy,
)
from src.nl2sql.infra.llm.profiles import ModelProfile, ModelTarget

PRIMARY = ModelTarget("deepseek", "deepseek-v4-flash", "small")
FALLBACK = ModelTarget("nvidia", "nvidia-small", "small")
CONFIGURED_SECRET = "fixture-fixture-fixture"


def _request(
    *,
    messages: list[dict[str, object]] | None = None,
    tool_schema: dict[str, object] | None = None,
) -> ModelRequest:
    return ModelRequest(
        alias="fast.default",
        stage="answer",
        messages=messages or [{"role": "user", "content": "show revenue"}],
        tool_schema=tool_schema,
        deadline_ms=1_000,
        token_budget=100,
        cost_budget=1,
        data_classification="internal",
        prompt_version="test-prompt-v1",
    )


def _policy(
    *targets: ModelTarget, secret_values: tuple[str, ...] = (), state: str = "calibrated"
) -> ModelInputPolicy:
    if state == "bootstrap":
        return bootstrap_model_input_policy(targets, secret_values=secret_values)
    return calibrated_model_input_policy(targets, secret_values=secret_values)


def test_bootstrap_and_calibrated_policies_approve_only_their_known_targets() -> None:
    policy = _policy(PRIMARY, FALLBACK)

    assert policy.evaluate(PRIMARY, _request()).outcome == "allow"
    assert policy.evaluate(FALLBACK, _request()).outcome == "allow"

    unknown_provider = ModelTarget("other-provider", "deepseek-v4-flash", "small")
    unknown_model = ModelTarget("deepseek", "unlisted-model", "small")
    for unlisted in (unknown_provider, unknown_model):
        decision = policy.evaluate(unlisted, _request())
        assert decision.outcome == "deny"
        assert decision.reason == MODEL_TARGET_NOT_APPROVED
        assert decision.matched_categories == ()


def test_primary_and_fallback_are_evaluated_independently_per_target() -> None:
    # A permitted primary must NOT imply a permitted fallback.
    policy = _policy(PRIMARY)
    request = _request()

    assert policy.evaluate(PRIMARY, request).outcome == "allow"
    assert policy.evaluate(FALLBACK, request).outcome == "deny"


def test_policy_approves_only_exact_pairs_never_a_provider_model_cross_product() -> None:
    # R1 FALSIFICATION: the policy approves exactly provider-a/model-x and
    # provider-b/model-y, so the cross-product pairs must be DENIED.
    a_x = ModelTarget("provider-a", "model-x", "small")
    b_y = ModelTarget("provider-b", "model-y", "small")
    a_y = ModelTarget("provider-a", "model-y", "small")
    b_x = ModelTarget("provider-b", "model-x", "small")
    policy = _policy(a_x, b_y)
    request = _request()

    assert policy.evaluate(a_x, request).outcome == "allow"
    assert policy.evaluate(b_y, request).outcome == "allow"

    for cross_product in (a_y, b_x):
        decision = policy.evaluate(cross_product, request)
        assert decision.outcome == "deny"
        assert decision.reason == MODEL_TARGET_NOT_APPROVED


def test_checksum_preserves_pairing_for_identical_provider_and_model_sets() -> None:
    # R1 FALSIFICATION: identical provider SETS and identical model SETS with a
    # different pairing must not collapse to the same approval or checksum.
    a_x = ModelTarget("provider-a", "model-x", "small")
    b_y = ModelTarget("provider-b", "model-y", "small")
    a_y = ModelTarget("provider-a", "model-y", "small")
    b_x = ModelTarget("provider-b", "model-x", "small")
    forward = _policy(a_x, b_y)
    swapped = _policy(a_y, b_x)

    assert {item.provider for item in forward.approved_destinations} == {
        item.provider for item in swapped.approved_destinations
    }
    assert {item.model for item in forward.approved_destinations} == {
        item.model for item in swapped.approved_destinations
    }
    assert forward.checksum != swapped.checksum

    # Approval semantics differ, so the two policies are NOT interchangeable.
    assert forward.evaluate(a_y, _request()).outcome == "deny"
    assert swapped.evaluate(a_y, _request()).outcome == "allow"
    assert swapped.evaluate(a_x, _request()).outcome == "deny"

    # ...but the SAME approvals supplied in a different order are one policy.
    reordered = _policy(b_y, a_x)
    assert reordered.checksum == forward.checksum
    assert reordered.approved_destinations == forward.approved_destinations


def test_approved_destinations_are_an_immutable_exact_pair_identity() -> None:
    policy = _policy(PRIMARY, FALLBACK)

    assert policy.approved_destinations == frozenset(
        {
            ApprovedDestination(provider="deepseek", model="deepseek-v4-flash"),
            ApprovedDestination(provider="nvidia", model="nvidia-small"),
        }
    )
    with pytest.raises(AttributeError):
        getattr(policy.approved_destinations, "add")(
            ApprovedDestination(provider="other", model="other")
        )


def test_configured_target_absent_from_the_approved_pairs_is_denied() -> None:
    # R2 D: dispatch configuration confers no approval.  FALLBACK is a real
    # configured target but is absent from the explicitly approved pair set.
    approved = calibrated_model_input_policy((PRIMARY,))

    assert approved.evaluate(PRIMARY, _request()).outcome == "allow"
    fallback_decision = approved.evaluate(FALLBACK, _request())
    assert fallback_decision.outcome == "deny"
    assert fallback_decision.reason == MODEL_TARGET_NOT_APPROVED


@pytest.mark.parametrize(
    ("content", "category"),
    [
        ("Authorization: Bearer sk-abcdefghijklmnop", "bearer_token"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private_key"),
        ("postgresql://svc:hunter2@db:5432/app", "credential_dsn"),
        ("password = 'hunter2'", "credential_assignment"),
        ("api_key: apikey-apikey-apikey", "credential_assignment"),
        ("Authorization: Basic YWFhYTpiYmJiYmJi", "basic_auth"),
        ("Authorization: Basic YTpi", "basic_auth"),
    ],
)
def test_layer_two_secret_shapes_are_denied(content: str, category: str) -> None:
    decision = _policy(PRIMARY).evaluate(
        PRIMARY, _request(messages=[{"role": "user", "content": content}])
    )

    assert decision.outcome == "deny"
    assert decision.reason == MODEL_INPUT_SECRET_DETECTED
    assert category in decision.matched_categories


def test_layer_one_configured_secret_value_is_denied_regardless_of_length() -> None:
    policy = _policy(PRIMARY, secret_values=(CONFIGURED_SECRET,))
    decision = policy.evaluate(
        PRIMARY,
        _request(messages=[{"role": "user", "content": "use " + CONFIGURED_SECRET}]),
    )
    assert decision.outcome == "deny"
    assert decision.matched_categories == ("configured_secret",)

    # R11: length is NOT a security boundary; a short real configured value is
    # still a hard deny.
    short_policy = _policy(PRIMARY, secret_values=("x7K9pQ",))
    assert short_policy.evaluate(
        PRIMARY, _request(messages=[{"role": "user", "content": "x7K9pQ is the key"}])
    ).outcome == "deny"

    # Known non-secret placeholder defaults are not turned into a global rule.
    sentinel_policy = _policy(PRIMARY, secret_values=("not-needed",))
    assert sentinel_policy.evaluate(
        PRIMARY, _request(messages=[{"role": "user", "content": "not-needed is a word"}])
    ).outcome == "allow"


def test_policy_without_explicit_secrets_still_covers_the_deployment_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R10-A/B/C: an approved policy built with NO explicit secret_values still
    # denies the deployment's own configured credentials.
    monkeypatch.setenv("EMBEDDING_API_KEY", "embed-embed-embed")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "langfuse-langfuse-langfuse")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://svc:dbpass-dbpass-dbpass@db:5432/app"
    )
    policy = calibrated_model_input_policy((PRIMARY,))

    for secret in (
        "embed-embed-embed",
        "langfuse-langfuse-langfuse",
        "dbpass-dbpass-dbpass",
    ):
        decision = policy.evaluate(
            PRIMARY, _request(messages=[{"role": "user", "content": "x " + secret}])
        )
        assert decision.outcome == "deny"
        assert decision.reason == MODEL_INPUT_SECRET_DETECTED
        assert "configured_secret" in decision.matched_categories


def test_explicit_policy_secrets_union_with_the_deployment_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R10-D: explicit policy values ADD to (never replace) the deployment source.
    monkeypatch.setenv("EMBEDDING_API_KEY", "embed-embed-embed")
    extra = "extra-extra-extra"
    policy = calibrated_model_input_policy((PRIMARY,), secret_values=(extra,))

    assert policy.evaluate(
        PRIMARY, _request(messages=[{"role": "user", "content": "x " + extra}])
    ).outcome == "deny"
    assert policy.evaluate(
        PRIMARY,
        _request(messages=[{"role": "user", "content": "x embed-embed-embed"}]),
    ).outcome == "deny"


def test_dsn_password_percent_encoding_is_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R12: urlsplit keeps the percent-encoded password; the runtime uses the
    # decoded one, so BOTH representations must be denied.
    from urllib.parse import quote

    from src.nl2sql.observability.secret_source import observability_secret_values

    password = "x7K9pQ:/@#"
    encoded = quote(password, safe="")
    monkeypatch.setenv("DATABASE_URL", f"postgresql://svc:{encoded}@db:5432/app")

    values = observability_secret_values()
    assert encoded in values
    assert password in values

    policy = calibrated_model_input_policy((PRIMARY,))
    for form in (encoded, password):
        decision = policy.evaluate(
            PRIMARY, _request(messages=[{"role": "user", "content": "x " + form}])
        )
        assert decision.outcome == "deny"
        assert decision.reason == MODEL_INPUT_SECRET_DETECTED


def test_secret_scan_walks_messages_and_tool_schema_recursively() -> None:
    policy = _policy(PRIMARY)
    request = _request(
        messages=[
            {"role": "system", "content": "policy text"},
            {"role": "user", "content": [{"nested": "Bearer abcdefghijkl"}]},
        ],
        tool_schema={"title": "Answer", "properties": {"note": {"default": "token=x-secret"}}},
    )

    decision = policy.evaluate(PRIMARY, request)

    assert decision.outcome == "deny"
    assert "bearer_token" in decision.matched_categories


def test_ordinary_authorized_business_content_is_available_to_an_approved_model() -> None:
    policy = _policy(PRIMARY)
    request = _request(
        messages=[
            {
                "role": "user",
                "content": "team A revenue 1200; contact 13800138000; id 110101199001011234",
            }
        ]
    )

    decision = policy.evaluate(PRIMARY, request)

    assert decision.outcome == "allow"
    assert decision.reason is None
    assert decision.matched_categories == ()


def test_checksum_is_stable_and_excludes_configured_secret_values() -> None:
    first = _policy(PRIMARY, FALLBACK, secret_values=(CONFIGURED_SECRET,))
    second = _policy(PRIMARY, FALLBACK, secret_values=("another-secret-value",))
    changed_model = _policy(ModelTarget("deepseek", "different", "small"))

    assert first.checksum == second.checksum
    assert len(first.checksum) == 64
    assert first.checksum != changed_model.checksum
    assert CONFIGURED_SECRET not in repr(first)


def test_content_sha256_is_stable_for_the_evaluated_payload() -> None:
    policy = _policy(PRIMARY)
    request = _request()

    assert (
        policy.evaluate(PRIMARY, request).content_sha256
        == policy.evaluate(PRIMARY, request).content_sha256
    )
    assert policy.evaluate(PRIMARY, request).content_sha256 != policy.evaluate(
        PRIMARY, _request(messages=[{"role": "user", "content": "different"}])
    ).content_sha256


def test_production_readiness_rejects_bootstrap_and_empty_destinations() -> None:
    bootstrap = _policy(PRIMARY, state="bootstrap")
    with pytest.raises(ModelInputPolicyUncalibrated, match="bootstrap"):
        bootstrap.require_production_ready()

    calibrated = _policy(PRIMARY)
    calibrated.require_production_ready()

    with pytest.raises(ModelInputPolicyUncalibrated, match="no approved destinations"):
        calibrated_model_input_policy(()).require_production_ready()

def test_zero_arg_gateway_is_bootstrap_and_never_calibrated_from_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R2 A: configured profile targets are dispatch configuration, NOT approval,
    # so a zero-arg gateway must not arrive calibrated merely from profiles.
    from types import SimpleNamespace

    from src.nl2sql.infra.llm import gateway as gateway_module

    monkeypatch.setattr(
        gateway_module,
        "get_settings",
        lambda: SimpleNamespace(service_mode="infra-dev", openai_api_key=""),
    )

    gateway = gateway_module.build_model_gateway()

    assert gateway.model_input_policy.state == "bootstrap"
    assert gateway.model_input_policy.version == "model-input.bootstrap.v1"
    with pytest.raises(ModelInputPolicyUncalibrated, match="bootstrap"):
        gateway.model_input_policy.require_production_ready()


def test_product_mode_without_an_explicit_policy_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R2 B: with no real deployment-approved policy source, product is NOT READY.
    from types import SimpleNamespace

    from src.nl2sql.infra.llm import gateway as gateway_module

    monkeypatch.setattr(
        gateway_module,
        "get_settings",
        lambda: SimpleNamespace(service_mode="product", openai_api_key=""),
    )

    with pytest.raises(ModelInputPolicyUncalibrated, match="bootstrap"):
        gateway_module.build_model_gateway()


def test_product_mode_accepts_an_explicit_approved_exact_pair_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R2 C: an injected deployment-approved exact-pair policy is ready.
    from types import SimpleNamespace

    from src.nl2sql.infra.llm import gateway as gateway_module

    monkeypatch.setattr(
        gateway_module,
        "get_settings",
        lambda: SimpleNamespace(service_mode="product", openai_api_key=""),
    )
    approved = calibrated_model_input_policy((PRIMARY, FALLBACK))

    gateway = gateway_module.build_model_gateway(model_input_policy=approved)

    assert gateway.model_input_policy is approved
    gateway.require_model_input_policy_ready(product_mode=True)


def test_build_model_gateway_rejects_a_bootstrap_policy_in_product_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from src.nl2sql.infra.llm import gateway as gateway_module

    monkeypatch.setattr(
        gateway_module,
        "get_settings",
        lambda: SimpleNamespace(service_mode="product", openai_api_key=""),
    )
    policy = bootstrap_model_input_policy((PRIMARY,))

    with pytest.raises(ModelInputPolicyUncalibrated, match="bootstrap"):
        gateway_module.build_model_gateway(model_input_policy=policy)


def test_gateway_production_readiness_is_gated_by_product_mode() -> None:
    from src.nl2sql.infra.llm.gateway import ModelGateway

    gateway = ModelGateway(
        providers={},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                PRIMARY,
                None,
            )
        },
    )

    gateway.require_model_input_policy_ready(product_mode=False)
    with pytest.raises(ModelInputPolicyUncalibrated, match="bootstrap"):
        gateway.require_model_input_policy_ready(product_mode=True)
