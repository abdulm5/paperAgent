import argparse
import json
import time
from typing import Any

import httpx


def should_alert(snapshot: dict[str, Any], threshold: float, minimum_requests: int) -> bool:
    return (
        snapshot["request_count"] >= minimum_requests
        and snapshot["error_rate"] > threshold
        and snapshot["first_failure_at"] is not None
    )


def build_alert(
    snapshot: dict[str, Any],
    threshold: float,
    checkout_api_url: str,
) -> dict[str, Any]:
    release = snapshot["current_release"]
    error_rate = snapshot["error_rate"]
    scenario_id = str(snapshot.get("scenario_id") or "unclassified")
    metric_name = {
        "payment-provider-timeout": "upstream_timeout_rate",
        "checkout-feature-flag-regression": "checkout_feature_error_rate",
    }.get(scenario_id, "http_server_error_rate")
    return {
        "fingerprint": f"checkout-api:{metric_name}:{scenario_id}",
        "source": "simulated-threshold-evaluator",
        "service": "checkout-api",
        "severity": "critical" if error_rate >= 0.1 else "high",
        "summary": (
            f"Checkout API {metric_name.replace('_', ' ')} is {error_rate:.1%}, "
            f"above the {threshold:.1%} threshold."
        ),
        "started_at": snapshot["first_failure_at"],
        "detected_at": snapshot["observed_at"],
        "metric": {
            "name": metric_name,
            "value": error_rate,
            "threshold": threshold,
            "window_seconds": snapshot["window_seconds"],
            "request_count": snapshot["request_count"],
            "failed_request_count": snapshot["failed_request_count"],
        },
        "release": release,
        "telemetry_url": f"{checkout_api_url.rstrip('/')}/telemetry",
    }


class AlertMonitor:
    def __init__(
        self,
        client: httpx.Client,
        checkout_api_url: str,
        pageragent_api_url: str,
        ingest_api_key: str,
        threshold: float,
        minimum_requests: int,
        window_seconds: int,
    ) -> None:
        self.client = client
        self.checkout_api_url = checkout_api_url.rstrip("/")
        self.pageragent_api_url = pageragent_api_url.rstrip("/")
        self.ingest_api_key = ingest_api_key
        self.threshold = threshold
        self.minimum_requests = minimum_requests
        self.window_seconds = window_seconds
        self.active_fingerprint: str | None = None

    def evaluate_once(self) -> str:
        telemetry_response = self.client.get(
            f"{self.checkout_api_url}/telemetry",
            params={"window_seconds": self.window_seconds},
        )
        telemetry_response.raise_for_status()
        snapshot = telemetry_response.json()

        if not should_alert(snapshot, self.threshold, self.minimum_requests):
            self.active_fingerprint = None
            return "healthy"

        alert = build_alert(snapshot, self.threshold, self.checkout_api_url)
        if alert["fingerprint"] == self.active_fingerprint:
            return "already-alerting"

        alert_response = self.client.post(
            f"{self.pageragent_api_url}/api/v1/alerts",
            json=alert,
            headers={"X-PagerAgent-Ingest-Key": self.ingest_api_key},
        )
        alert_response.raise_for_status()
        self.active_fingerprint = alert["fingerprint"]
        print(json.dumps({"event": "alert.delivered", **alert_response.json()}, indent=2))
        return "alert-delivered"


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate checkout telemetry and emit alerts.")
    parser.add_argument("--checkout-api-url", default="http://localhost:8100")
    parser.add_argument("--pageragent-api-url", default="http://localhost:8000")
    parser.add_argument(
        "--ingest-api-key",
        default="pageragent-local-ingest-key",
        help="Tenant-bound machine credential used to deliver monitoring alerts.",
    )
    parser.add_argument("--threshold", type=float, default=0.05)
    parser.add_argument("--minimum-requests", type=int, default=20)
    parser.add_argument("--window-seconds", type=int, default=300)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    with httpx.Client(timeout=10) as client:
        monitor = AlertMonitor(
            client=client,
            checkout_api_url=args.checkout_api_url,
            pageragent_api_url=args.pageragent_api_url,
            ingest_api_key=args.ingest_api_key,
            threshold=args.threshold,
            minimum_requests=args.minimum_requests,
            window_seconds=args.window_seconds,
        )
        while True:
            try:
                outcome = monitor.evaluate_once()
                print(json.dumps({"event": "monitor.evaluated", "outcome": outcome}))
            except httpx.HTTPError as error:
                print(json.dumps({"event": "monitor.unavailable", "error": str(error)}))
            if args.once:
                break
            time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
