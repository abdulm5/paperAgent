from collections import defaultdict
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.investigation.text import canonical_hash


class ClusterResult(BaseModel):
    signature: str
    error_type: str
    endpoint: str
    affected_attributes: dict[str, Any]
    failure_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    sample_request_ids: list[str]


class ErrorClusterer:
    version = "error-cluster-v1"

    def cluster(self, telemetry: dict[str, Any]) -> list[ClusterResult]:
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for event in telemetry.get("recent_events", []):
            if event.get("outcome") != "failure":
                continue
            key = (
                str(event.get("error_type") or "UnknownError"),
                str(event.get("endpoint") or "unknown-endpoint"),
                str(event.get("release") or "unknown-release"),
            )
            grouped[key].append(event)

        results: list[ClusterResult] = []
        for (error_type, endpoint, release), events in grouped.items():
            timestamps = sorted(
                datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00"))
                for event in events
            )
            payment_methods = sorted({str(event.get("payment_method")) for event in events})
            signature = canonical_hash(
                {"error_type": error_type, "endpoint": endpoint, "release": release}
            )[:16]
            results.append(
                ClusterResult(
                    signature=signature,
                    error_type=error_type,
                    endpoint=endpoint,
                    affected_attributes={
                        "payment_methods": payment_methods,
                        "releases": [release],
                    },
                    failure_count=len(events),
                    first_seen_at=timestamps[0],
                    last_seen_at=timestamps[-1],
                    sample_request_ids=[str(event.get("request_id")) for event in events[:5]],
                )
            )
        return sorted(results, key=lambda cluster: cluster.failure_count, reverse=True)
