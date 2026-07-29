from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_yaml(relative_path: str) -> dict[str, Any]:
    loaded = yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError(f"expected mapping in {relative_path}")
    return cast(dict[str, Any], loaded)


def test_base_compose_exposes_only_nginx_and_uses_readiness_healthchecks() -> None:
    base = _load_yaml("docker/compose.base.yml")
    services = base["services"]
    assert {"api-a", "api-b", "nginx"}.issubset(services)
    assert services["nginx"]["networks"] == {"edge_net": None}
    assert "ports" in services["nginx"]

    for service_name in ("api-a", "api-b"):
        service = services[service_name]
        assert "ports" not in service
        assert "env_file" not in service
        assert service["expose"] == ["8000"]
        assert service["environment"]["API_PORT"] == "8000"
        assert "/readyz" in " ".join(service["healthcheck"]["test"])
        assert set(service["networks"]) == {"edge_net", "control_net", "egress_net"}

    assert base["networks"]["control_net"]["internal"] is True
    assert base["networks"]["edge_net"].get("internal") is not True


def test_product_profile_uses_external_business_network_and_file_secrets() -> None:
    product = _load_yaml("docker/compose.prod.yml")
    assert product["networks"]["business_external_net"]["external"] is True
    assert "business-postgres" not in product["services"]

    sensitive_names = {
        "DATABASE_URL",
        "CONTROL_DATABASE_URL",
        "CHECKPOINT_DATABASE_URL",
        "DEEPSEEK_API_KEY",
        "NVIDIA_API_KEY",
        "EMBEDDING_API_KEY",
    }
    for service_name in ("api-a", "api-b"):
        service = product["services"][service_name]
        environment = service["environment"]
        assert environment["CODEACT_MODE"] == "disabled"
        assert environment["ENABLE_DYNAMIC_CALC"] == "false"
        assert environment["MEMORY_BACKEND"] == "postgresql"
        assert not sensitive_names.intersection(environment)
        assert all(environment[f"{name}_FILE"].startswith("/run/secrets/") for name in sensitive_names)
        assert "business_external_net" in service["networks"]
        assert "data_net" not in service["networks"]
        assert set(service["secrets"]) == {
            "business_ro_database_url",
            "control_app_database_url",
            "checkpoint_app_database_url",
            "deepseek_api_key",
            "nvidia_nim_api_key",
            "embedding_api_key",
        }

    compose_text = (ROOT / "docker/compose.base.yml").read_text(encoding="utf-8") + (
        ROOT / "docker/compose.prod.yml"
    ).read_text(encoding="utf-8")
    assert "/var/run/docker.sock" not in compose_text
    assert "privileged:" not in compose_text
    assert "network_mode: host" not in compose_text
    assert "ssh_password" not in compose_text
    assert "ssh_private" not in compose_text


def test_dev_databases_are_healthy_isolated_and_use_distinct_named_volumes() -> None:
    dev = _load_yaml("docker/compose.dev.yml")
    services = dev["services"]
    assert services["control-postgres"]["networks"] == {"control_net": None}
    assert services["checkpoint-postgres"]["networks"] == {"control_net": None}
    assert services["business-postgres"]["networks"] == {"data_net": None}
    for service_name in ("control-postgres", "checkpoint-postgres", "business-postgres"):
        assert "pg_isready" in " ".join(services[service_name]["healthcheck"]["test"])
        assert "ports" not in services[service_name]

    volume_names = {
        dev["volumes"]["control_pgdata"]["name"],
        dev["volumes"]["checkpoint_pgdata"]["name"],
        dev["volumes"]["business_pgdata"]["name"],
    }
    assert len(volume_names) == 3

    test_profile = _load_yaml("docker/compose.test.yml")
    test_volume_names = {
        test_profile["volumes"]["control_pgdata"]["name"],
        test_profile["volumes"]["checkpoint_pgdata"]["name"],
        test_profile["volumes"]["business_pgdata"]["name"],
    }
    assert len(test_volume_names) == 3
    assert volume_names.isdisjoint(test_volume_names)


def test_benchmark_is_an_explicit_one_shot_compose_profile() -> None:
    dev = _load_yaml("docker/compose.dev.yml")
    benchmark = dev["services"]["benchmark"]
    assert benchmark["profiles"] == ["benchmark"]
    assert benchmark["command"] == [
        "python",
        "-m",
        "benchmarks.runner",
        "--dataset",
        "enterprise",
        "--stub",
    ]
    assert benchmark["environment"]["CONTROL_DATABASE_URL_FILE"].startswith("/run/secrets/")


def test_release_compose_pins_app_image_and_keeps_ops_profiles() -> None:
    release = _load_yaml("docker/compose.release.yml")
    for service_name in ("api-a", "api-b", "indexer"):
        assert "TTAI_IMAGE_REF" in release["services"][service_name]["image"]
    assert release["services"]["migrate"]["profiles"] == ["ops"]
    assert release["services"]["backup"]["volumes"][0].endswith(":/backups")
    assert release["networks"]["business_external_net"]["external"] is True


def test_nginx_contract_has_failover_limits_safe_logs_and_sse_headers() -> None:
    config = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
    assert "client_max_body_size 64k" in config
    assert "log_format ttai_json escape=json" in config
    assert '"request_id":"$request_id"' in config
    assert "add_header X-Request-ID $request_id always" in config
    assert "$http_authorization" not in config
    assert "$request_body" not in config
    assert "limit_req zone=queries" in config
    assert "limit_conn connections" in config
    assert "proxy_buffering off" in config
    assert "proxy_request_buffering off" in config
    assert "X-Accel-Buffering no always" in config
    assert "proxy_next_upstream_tries 2" in config
    assert "proxy_set_header Connection \"\"" in config
    assert "proxy_set_header X-User-ID \"\"" in config
    assert "proxy_set_header X-Permissions \"\"" in config
    assert "server api-a:8000" in config
    assert "server api-b:8000" in config
    assert "location = /healthz" in config
    assert "location = /readyz" in config
