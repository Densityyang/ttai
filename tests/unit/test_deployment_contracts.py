from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_yaml(relative_path: str) -> dict[str, Any]:
    loaded = yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError(f"expected mapping in {relative_path}")
    return cast(dict[str, Any], loaded)


def _healthcheck_tokens(compose: dict[str, Any], service_name: str) -> list[str]:
    """Tokenize a service healthcheck, stripping any CMD/CMD-SHELL prefix."""
    healthcheck = compose["services"][service_name].get("healthcheck") or {}
    command = healthcheck.get("test")
    if command is None:
        return []
    parts = [str(part) for part in (command if isinstance(command, list) else [command])]
    if parts and parts[0] in {"CMD", "CMD-SHELL"}:
        parts = parts[1:]
    if len(parts) == 1:
        parts = shlex.split(parts[0])
    return parts


def _assert_pg_isready_targets_tcp(compose: dict[str, Any], service_name: str) -> None:
    """Require a pg_isready probe to connect over TCP to 127.0.0.1.

    The postgres entrypoint runs /docker-entrypoint-initdb.d scripts against a
    temporary server with an empty listen_addresses, so a host-less pg_isready
    reports healthy before the final TCP listener exists and services gated on
    service_healthy connect too early.  Parsing the command into tokens keeps
    this assertion independent of spacing and flag rendering.
    """
    tokens = _healthcheck_tokens(compose, service_name)
    assert tokens[:1] == ["pg_isready"], f"{service_name} is not healthchecked by pg_isready"
    hosts: list[str] = []
    for index, token in enumerate(tokens):
        if token in {"-h", "--host"}:
            assert index + 1 < len(tokens), f"{service_name}: {token} needs a value"
            hosts.append(tokens[index + 1])
        elif token.startswith("--host="):
            hosts.append(token.partition("=")[2])
        elif token.startswith("-h") and len(token) > 2:
            hosts.append(token[2:])
    assert hosts == ["127.0.0.1"], (
        f"{service_name} pg_isready must target 127.0.0.1 over TCP, "
        f"not the Unix socket (got {hosts or 'no host'})"
    )


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


def test_two_api_bootstrap_sql_active_concurrency_does_not_exceed_eight() -> None:
    base = _load_yaml("docker/compose.base.yml")
    active_defaults: list[int] = []
    for service_name in ("api-a", "api-b"):
        environment = base["services"][service_name]["environment"]
        active_value = environment["QUERY_GATEWAY_SQL_ACTIVE_CONCURRENCY"]
        assert active_value == "${QUERY_GATEWAY_SQL_ACTIVE_CONCURRENCY:-4}"
        assert environment["QUERY_GATEWAY_SQL_WAIT_QUEUE_SIZE"] == (
            "${QUERY_GATEWAY_SQL_WAIT_QUEUE_SIZE:-8}"
        )
        assert environment["QUERY_GATEWAY_SQL_WAIT_TIMEOUT_SECONDS"] == (
            "${QUERY_GATEWAY_SQL_WAIT_TIMEOUT_SECONDS:-3}"
        )
        active_defaults.append(int(active_value.removesuffix("}").rsplit(":-", 1)[1]))

    assert sum(active_defaults) <= 8


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
        _assert_pg_isready_targets_tcp(dev, service_name)
        assert "ports" not in services[service_name]
        assert "./initdb/roles.sh:/docker-entrypoint-initdb.d/010-roles.sh:ro" in services[
            service_name
        ]["volumes"]

    assert services["business-postgres"]["environment"]["DB_APP_PRIVILEGES"] == "readonly"
    assert services["business-postgres"]["environment"]["DB_APP_ROLE"] == "business_reader"
    for database_name in ("control", "checkpoint"):
        environment = services[f"{database_name}-postgres"]["environment"]
        assert environment["DB_APP_ROLE"] == f"{database_name}_app"
        assert environment["DB_MIGRATOR_ROLE"] == f"{database_name}_migrator"
        assert environment["DB_BACKUP_ROLE"] == f"{database_name}_backup"

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


def test_every_compose_pg_isready_healthcheck_targets_tcp_host() -> None:
    expected_services = {
        "docker/compose.dev.yml": {
            "control-postgres",
            "checkpoint-postgres",
            "business-postgres",
        },
        "docker/compose.test.yml": {
            "control-postgres",
            "checkpoint-postgres",
            "business-postgres",
        },
        "docker/compose.release.yml": {"control-postgres", "checkpoint-postgres"},
    }
    for compose_path, expected in expected_services.items():
        compose = _load_yaml(compose_path)
        probed = {
            service_name
            for service_name in compose["services"]
            if _healthcheck_tokens(compose, service_name)[:1] == ["pg_isready"]
        }
        assert probed == expected, f"{compose_path}: {probed}"
        for service_name in sorted(expected):
            _assert_pg_isready_targets_tcp(compose, service_name)


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
    for service_name in ("api-a", "api-b", "indexer", "migrate", "schema-snapshot"):
        assert "TTAI_IMAGE_REF" in release["services"][service_name]["image"]
    assert release["services"]["migrate"]["profiles"] == ["ops"]
    assert release["services"]["migrate"]["command"] == ["--phase", "expand"]
    assert release["services"]["migrate"]["user"] == "0:0"
    assert release["services"]["migrate"]["read_only"] is True
    assert release["services"]["migrate"]["cap_drop"] == ["ALL"]
    assert release["services"]["migrate"]["cap_add"] == ["DAC_READ_SEARCH"]
    assert release["services"]["migrate"]["security_opt"] == ["no-new-privileges:true"]
    assert release["services"]["migrate"]["environment"]["CHECKPOINT_SNAPSHOT_REQUIRED"] == (
        "true"
    )
    assert release["services"]["control-postgres"]["volumes"][0].startswith("control_pgdata:")
    assert release["services"]["checkpoint-postgres"]["volumes"][0].startswith(
        "checkpoint_pgdata:"
    )
    assert "business-postgres" not in release["services"]
    backup_volume = release["services"]["backup"]["volumes"][0]
    restore_volume = release["services"]["restore-test"]["volumes"][0]
    assert backup_volume == {
        "type": "bind",
        "source": "${BACKUP_HOST_DIR:-../var/backups}",
        "target": "/backups",
    }
    assert restore_volume == {**backup_volume, "read_only": True}
    assert release["networks"]["business_external_net"]["external"] is True
    assert release["services"]["backup"]["environment"]["BACKUP_RETENTION_DAYS"].endswith(
        ":-7}"
    )


def test_schema_snapshot_is_a_bounded_one_shot_ops_service() -> None:
    dev = _load_yaml("docker/compose.dev.yml")
    release = _load_yaml("docker/compose.release.yml")

    for compose, business_network in (
        (dev, "data_net"),
        (release, "business_external_net"),
    ):
        service = compose["services"]["schema-snapshot"]
        assert service["profiles"] == ["ops"]
        assert service["command"] == [
            "python",
            "-m",
            "src.nl2sql.semantic.schema_snapshot",
        ]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert set(service["secrets"]) == {
            "business_ro_database_url",
            "control_app_database_url",
        }
        assert service["environment"]["DATABASE_URL_FILE"] == (
            "/run/secrets/business_ro_database_url"
        )
        assert service["environment"]["CONTROL_DATABASE_URL_FILE"] == (
            "/run/secrets/control_app_database_url"
        )
        assert service["environment"]["SCHEMA_SNAPSHOT_MAX_RELATIONS"].endswith(
            ":-64}"
        )
        assert {"control_net", business_network}.issubset(service["networks"])

    api_source = (ROOT / "src/nl2sql/api.py").read_text(encoding="utf-8")
    assert "schema_snapshot" not in api_source


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
