from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_yaml(relative_path: str) -> dict[str, object]:
    return yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))


def test_compose_profiles_have_two_api_instances_and_external_product_network() -> None:
    base = _load_yaml("docker/compose.base.yml")
    services = base["services"]
    assert {"api-a", "api-b", "nginx"}.issubset(services)
    assert base["networks"]["app_internal"]["internal"] is True

    product = _load_yaml("docker/compose.prod.yml")
    assert product["networks"]["business_external_net"]["external"] is True
    assert "business_external_net" in product["services"]["api-a"]["networks"]
    for service_name in ("api-a", "api-b"):
        environment = product["services"][service_name]["environment"]
        assert environment["CODEACT_MODE"] == "disabled"
        assert environment["ENABLE_DYNAMIC_CALC"] == "false"

    compose_text = (ROOT / "docker/compose.base.yml").read_text(encoding="utf-8") + (
        ROOT / "docker/compose.prod.yml"
    ).read_text(encoding="utf-8")
    assert "/var/run/docker.sock" not in compose_text
    assert "privileged:" not in compose_text
    assert "network_mode: host" not in compose_text


def test_benchmark_is_an_explicit_one_shot_compose_profile() -> None:
    dev = _load_yaml("docker/compose.dev.yml")
    benchmark = dev["services"]["benchmark"]
    assert benchmark["profiles"] == ["benchmark"]
    assert benchmark["command"] == ["python", "-m", "benchmarks.runner", "--dataset", "enterprise", "--stub"]
    assert "/run/secrets/control_app_database_url" in benchmark["environment"]["CONTROL_DATABASE_URL_FILE"]


def test_nginx_contract_preserves_sse_request_id_and_body_limits() -> None:
    config = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
    assert "client_max_body_size 32k" in config
    assert "proxy_buffering off" in config
    assert "X-Request-ID $request_id" in config
    assert "server api-a:8000" in config
    assert "server api-b:8000" in config
