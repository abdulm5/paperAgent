from typing import Any, Protocol

import httpx


class TelemetryCollector(Protocol):
    version: str

    def collect(self, source_uri: str) -> dict[str, Any]: ...


class HttpTelemetryCollector:
    version = "http-telemetry-v1"

    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self.timeout_seconds = timeout_seconds

    def collect(self, source_uri: str) -> dict[str, Any]:
        response = httpx.get(source_uri, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Telemetry source must return a JSON object")
        return payload


class StaticTelemetryCollector:
    version = "static-telemetry-v1"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def collect(self, source_uri: str) -> dict[str, Any]:
        del source_uri
        return self.payload
