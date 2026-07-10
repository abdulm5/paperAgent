import argparse
import json
import time
from dataclasses import asdict, dataclass

import httpx

PAYMENT_SEQUENCE = ["card", "card", "bank_transfer", "card", "digital_wallet"]


@dataclass
class TrafficSummary:
    request_count: int
    successful_request_count: int
    failed_request_count: int


def request_payload(index: int) -> dict[str, str | int]:
    return {
        "user_id": f"synthetic-user-{index:04d}",
        "cart_total_cents": 2500 + (index % 4) * 750,
        "payment_method": PAYMENT_SEQUENCE[(index - 1) % len(PAYMENT_SEQUENCE)],
    }


def run_traffic(
    client: httpx.Client,
    base_url: str,
    request_count: int,
    delay_seconds: float,
    run_id: str,
) -> TrafficSummary:
    successful = 0
    failed = 0
    for index in range(1, request_count + 1):
        response = client.post(
            f"{base_url}/checkout",
            json=request_payload(index),
            headers={
                "X-Request-ID": f"{run_id}-traffic-{index:06d}",
                "X-Trace-ID": f"{run_id}-trace-{index:06d}",
            },
        )
        if response.status_code == 201:
            successful += 1
        elif response.status_code == 500:
            failed += 1
        else:
            response.raise_for_status()
        if delay_seconds:
            time.sleep(delay_seconds)

    return TrafficSummary(
        request_count=request_count,
        successful_request_count=successful,
        failed_request_count=failed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Send deterministic traffic to checkout-api.")
    parser.add_argument("--base-url", default="http://localhost:8100")
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--delay", type=float, default=0.02)
    parser.add_argument("--run-id", default="demo")
    args = parser.parse_args()
    if args.requests <= 0:
        parser.error("--requests must be greater than zero")
    if args.delay < 0:
        parser.error("--delay cannot be negative")

    with httpx.Client(timeout=10) as client:
        summary = run_traffic(
            client,
            args.base_url.rstrip("/"),
            args.requests,
            args.delay,
            args.run_id,
        )
    print(json.dumps(asdict(summary), indent=2))


if __name__ == "__main__":
    main()
