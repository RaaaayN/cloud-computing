from dataclasses import dataclass
import logging
from typing import Any
import requests


@dataclass(frozen=True)
class PrometheusQueries:
    queue_depth: str
    p99_latency: str
    arrival_rate: str


class PrometheusApiClient:
    def __init__(self, base_url: str, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.logger = logging.getLogger(self.__class__.__name__)

    def query_scalar(self, query: str) -> float:
        endpoint = f"{self.base_url}/api/v1/query"
        response = requests.get(
            endpoint,
            params={"query": query},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()

        if data.get("status") != "success":
            raise ValueError(f"Prometheus query failed for query: {query}")

        result = data.get("data", {}).get("result", [])
        if not result:
            self.logger.warning("No result for query '%s', defaulting to 0.0", query)
            return 0.0

        value = result[0].get("value", [0, "0"])
        return float(value[1])

