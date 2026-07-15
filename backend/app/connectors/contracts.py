from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, SecretStr, ValidationError

from app.core.config import Settings, settings
from app.domain.connectors import (
    ConnectorProvider,
    GithubConfiguration,
    GithubCredentials,
    PrometheusConfiguration,
    PrometheusCredentials,
    SlackConfiguration,
    SlackCredentials,
)

CONFIGURATION_MODELS: dict[ConnectorProvider, type[BaseModel]] = {
    ConnectorProvider.GITHUB: GithubConfiguration,
    ConnectorProvider.PROMETHEUS: PrometheusConfiguration,
    ConnectorProvider.SLACK: SlackConfiguration,
}

CREDENTIAL_MODELS: dict[ConnectorProvider, type[BaseModel]] = {
    ConnectorProvider.GITHUB: GithubCredentials,
    ConnectorProvider.PROMETHEUS: PrometheusCredentials,
    ConnectorProvider.SLACK: SlackCredentials,
}

PROVIDER_URL_FIELDS: dict[ConnectorProvider, tuple[str, ...]] = {
    ConnectorProvider.GITHUB: ("api_url",),
    ConnectorProvider.PROMETHEUS: ("base_url",),
    ConnectorProvider.SLACK: ("api_url",),
}


class ConnectorContractError(Exception):
    """A generic contract error that never embeds submitted configuration or secrets."""


def validate_configuration(
    provider: ConnectorProvider,
    configuration: Mapping[str, Any],
    runtime_settings: Settings = settings,
) -> dict[str, Any]:
    try:
        validated = CONFIGURATION_MODELS[provider].model_validate(dict(configuration))
    except (TypeError, ValueError, ValidationError) as error:
        raise ConnectorContractError(
            f"Configuration does not match the {provider.value} connector contract"
        ) from error

    normalized = validated.model_dump(mode="json", exclude_none=True)
    for field in PROVIDER_URL_FIELDS[provider]:
        value = normalized.get(field)
        if value is not None:
            _validate_provider_url(str(value), runtime_settings)
    return normalized


def validate_credentials(
    provider: ConnectorProvider,
    credentials: Mapping[str, SecretStr | str],
) -> dict[str, str]:
    try:
        validated = CREDENTIAL_MODELS[provider].model_validate(dict(credentials))
    except (TypeError, ValueError, ValidationError) as error:
        raise ConnectorContractError(
            f"Credentials do not match the {provider.value} connector contract"
        ) from error
    return {
        field: value.get_secret_value()
        for field, value in validated.model_dump().items()
    }


def _validate_provider_url(url: str, runtime_settings: Settings) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ConnectorContractError("Connector URL is invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConnectorContractError("Connector URL is invalid")
    if runtime_settings.environment not in {"local", "test"} and parsed.scheme != "https":
        raise ConnectorContractError("Connector URLs must use HTTPS")

    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    origin = f"{parsed.scheme.lower()}://{host}"
    if port is not None:
        origin += f":{port}"
    allowed_origins = {
        item.strip().rstrip("/").lower()
        for item in runtime_settings.connector_allowed_origins.split(",")
        if item.strip()
    }
    if origin not in allowed_origins:
        raise ConnectorContractError("Connector URL origin is not allowlisted")
