"""Bounded deployment secret VALUES for observability egress scrubbing.

P2-S2's technical-secret primitive needs the deployment's OWN configured
credential values to substring-match against.  This module returns a BOUNDED,
fixed allowlist -- provider API keys plus the passwords embedded in the
configured database DSNs -- resolved from BOTH the env/*_FILE channel AND the
application's supported pydantic Settings/AgentConfig env_file channel (which
does not necessarily populate os.environ).  It is not a secret scanner and not
a business DLP source.

The returned values are only ever fed to content_policy for redaction.  They
are never logged, never emitted to a sink, and never included in a policy
fingerprint/checksum.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import unquote, urlsplit

from src.core.secrets import SecretProvider

logger = logging.getLogger(__name__)

# A settings-resolution failure is one CONDITION, not an event per egress, so it
# is reported once per process instead of on every scrub call.
_settings_failure_reported = False


def _report_settings_failure(source: str, exc: BaseException) -> None:
    """Surface a settings-read failure once, without ever logging a value."""

    global _settings_failure_reported
    if _settings_failure_reported:
        return
    _settings_failure_reported = True
    logger.warning(
        "observability secret source could not read %s (%s); only the "
        "environment and *_FILE channels apply to this process",
        source,
        type(exc).__name__,
    )

# The bounded set of configured credential values this deployment may hold.
_API_KEY_ENV_NAMES = (
    "DEEPSEEK_API_KEY",
    "NVIDIA_API_KEY",
    "OPENAI_API_KEY",
    "LANGFUSE_SECRET_KEY",
    "EMBEDDING_API_KEY",
)
_DSN_ENV_NAMES = (
    "DATABASE_URL",
    "CONTROL_DATABASE_URL",
    "CHECKPOINT_DATABASE_URL",
    # The release image's migrate/ops services also hold these DSNs
    # (docker/alembic/{control,checkpoint}/env.py, checkpoint_migrate.py).
    "CONTROL_MIGRATOR_DATABASE_URL",
    "CHECKPOINT_MIGRATOR_DATABASE_URL",
)

# Known non-secret placeholder/default values from the bounded settings
# sources.  Only these are filtered at the source: a short REAL credential is
# never exempted by a length heuristic (R11).
NON_SECRET_SENTINELS = frozenset({"not-needed"})


def observability_secret_values() -> tuple[str, ...]:
    """Return the deployment's bounded configured secret values (never logged).

    Resolution covers every supported configuration channel for the bounded
    credential set: environment variables, *_FILE secrets, and the live
    application settings objects whose pydantic env_file values need not be
    present in os.environ.  The field set is a fixed allowlist; arbitrary
    environment or settings values are never enumerated.
    """

    provider = SecretProvider()
    values: list[str] = []
    for name in _API_KEY_ENV_NAMES:
        values.append(_resolve(provider, name))
    for name in _DSN_ENV_NAMES:
        values.extend(_dsn_passwords(_resolve(provider, name)))
    values.extend(_settings_secret_values())
    return _bounded(values)


def _settings_secret_values() -> tuple[str, ...]:
    """Bounded credential values held by the live runtime settings objects.

    Settings and AgentConfig load their pydantic env_file channel, which a
    deployment may use without the values ever reaching os.environ.  Only the
    fixed allowlist below is read.  A settings failure must never break the
    scrubber, and values are never logged or returned to any other caller.
    """

    values: list[str] = []
    try:
        from src.core.settings import get_settings

        settings = get_settings()
        values.append(settings.openai_api_key or "")
        values.append(settings.langfuse_secret_key or "")
        for dsn in (
            settings.database_url,
            settings.control_database_url,
            settings.checkpoint_database_url,
        ):
            values.extend(_dsn_passwords(dsn or ""))
    except Exception as exc:
        # Settings may legitimately be unavailable or invalid outside a real
        # deployment; the env/*_FILE channel above still applies.  The failure
        # is still surfaced once so an operator is not left guessing.
        _report_settings_failure("src.core.settings", exc)
    try:
        from src.nl2sql.config.settings import get_agent_config

        values.append(get_agent_config().embedding_api_key.get_secret_value() or "")
    except Exception as exc:
        _report_settings_failure("src.nl2sql.config.settings", exc)
    return tuple(values)


def _resolve(provider: SecretProvider, name: str) -> str:
    try:
        return provider.get(name) or ""
    except (ValueError, OSError):
        # A malformed or unreadable *_FILE peer must not disable scrubbing for
        # every other value; fall back to the plain environment value.
        return os.environ.get(name) or ""


def _bounded(values: list[str]) -> tuple[str, ...]:
    """De-duplicate and drop empty or known non-secret placeholder values."""

    return tuple(
        dict.fromkeys(
            value
            for value in values
            if isinstance(value, str) and value and value not in NON_SECRET_SENTINELS
        )
    )


def _dsn_passwords(dsn: str) -> tuple[str, ...]:
    """Both credential representations a DSN can carry.

    urlsplit keeps the percent-ENCODED password, while the runtime connects with
    the DECODED value, so both must be matched to avoid a bare-credential leak.
    """

    if not dsn:
        return ()
    try:
        encoded = urlsplit(dsn).password or ""
    except ValueError:
        return ()
    decoded = unquote(encoded)
    return tuple(dict.fromkeys(value for value in (encoded, decoded) if value))
