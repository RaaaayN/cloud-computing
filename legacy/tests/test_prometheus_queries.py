from typing import Any
import pytest
import requests
from autoscaler.prometheus_client import PrometheusApiClient


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError("http error")

    def json(self) -> dict[str, Any]:
        return self._payload


def test_query_scalar_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*args: Any, **kwargs: Any) -> FakeResponse:
        return FakeResponse(
            {
                "status": "success",
                "data": {"result": [{"value": [1717335000, "12.5"]}]},
            }
        )

    monkeypatch.setattr("autoscaler.prometheus_client.requests.get", fake_get)
    client = PrometheusApiClient(base_url="http://prometheus:9090")
    assert client.query_scalar("dispatcher_queue_depth") == 12.5


def test_query_scalar_empty_result_defaults_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*args: Any, **kwargs: Any) -> FakeResponse:
        return FakeResponse({"status": "success", "data": {"result": []}})

    monkeypatch.setattr("autoscaler.prometheus_client.requests.get", fake_get)
    client = PrometheusApiClient(base_url="http://prometheus:9090")
    assert client.query_scalar("dispatcher_queue_depth") == 0.0


def test_query_scalar_raises_when_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*args: Any, **kwargs: Any) -> FakeResponse:
        return FakeResponse({"status": "error", "data": {"result": []}})

    monkeypatch.setattr("autoscaler.prometheus_client.requests.get", fake_get)
    client = PrometheusApiClient(base_url="http://prometheus:9090")

    with pytest.raises(ValueError):
        client.query_scalar("dispatcher_queue_depth")

