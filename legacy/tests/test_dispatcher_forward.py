import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from dispatcher.app import (
    QUEUE_MAX_SIZE,
    REQUEST_QUEUE,
    QueuedRequest,
    build_app,
    refresh_queue_depth,
)


def _clear_queue() -> None:
    while not REQUEST_QUEUE.empty():
        item = REQUEST_QUEUE.get_nowait()
        REQUEST_QUEUE.task_done()
        if item is not None and not item.future.done():
            item.future.cancel()
    refresh_queue_depth()


@pytest.fixture
def clear_dispatcher_queue() -> None:
    _clear_queue()
    yield
    _clear_queue()


@pytest.mark.asyncio
async def test_submit_forwards_to_inference_and_returns_body(clear_dispatcher_queue) -> None:
    inference_body = b'["tabby"]'
    mock_forward = AsyncMock(return_value=(200, inference_body))

    with patch("dispatcher.app.WORKER_COUNT", 1), patch(
        "dispatcher.app.forward_to_inference", mock_forward
    ):
        app = build_app()
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/submit", json={"data": "encoded-image"})
            assert response.status == 200
            body = await response.json()

    assert body == ["tabby"]
    mock_forward.assert_awaited_once()
    assert mock_forward.await_args.args[1] == "encoded-image"
    assert REQUEST_QUEUE.qsize() == 0


@pytest.mark.asyncio
async def test_submit_returns_503_when_queue_full(clear_dispatcher_queue) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[int, bytes]] = loop.create_future()
    for _ in range(QUEUE_MAX_SIZE):
        await REQUEST_QUEUE.put(QueuedRequest(data="blocked", future=future))

    with patch("dispatcher.app.WORKER_COUNT", 0):
        app = build_app()
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/submit", json={"data": "new-request"})
            assert response.status == 503
            body = await response.json()

    assert body == {"error": "Queue is full"}


@pytest.mark.asyncio
async def test_submit_rejects_missing_data(clear_dispatcher_queue) -> None:
    with patch("dispatcher.app.WORKER_COUNT", 0):
        app = build_app()
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/submit", json={"image": "missing-field"})
            assert response.status == 400
            body = await response.json()

    assert "Missing required field" in body["error"]
