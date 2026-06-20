import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from load_tester.images import build_submit_payload, encode_image_bytes
from load_tester.run import send, target_rps


def test_target_rps_at_start() -> None:
    assert target_rps(0.0, 100.0, 1.0, 20.0) == pytest.approx(1.0)


def test_target_rps_at_peak() -> None:
    assert target_rps(50.0, 100.0, 1.0, 20.0) == pytest.approx(20.0)


def test_target_rps_at_end() -> None:
    assert target_rps(100.0, 100.0, 1.0, 20.0) == pytest.approx(1.0)


def test_build_submit_payload_structure() -> None:
    payload = build_submit_payload("abc123")
    assert payload == {"data": "abc123"}


def test_encode_image_bytes() -> None:
    encoded = encode_image_bytes(b"hello")
    assert encoded == "aGVsbG8="


@pytest.mark.asyncio
async def test_send_posts_to_submit_and_records_metrics() -> None:
    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=mock_response)

    writer = MagicMock()
    writer.writerow = MagicMock()
    csv_lock = asyncio.Lock()

    await send(mock_client, "http://localhost:8002", "img-data", writer, csv_lock)

    mock_client.post.assert_awaited_once()
    call_args = mock_client.post.await_args
    assert call_args.args[0] == "http://localhost:8002/submit"
    assert call_args.kwargs["json"] == {"data": "img-data"}
    writer.writerow.assert_called_once()
    row = writer.writerow.call_args.args[0]
    assert row[1] == 200
    assert row[2] >= 0


@pytest.mark.asyncio
async def test_send_records_error_on_exception() -> None:
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

    writer = MagicMock()
    writer.writerow = MagicMock()
    csv_lock = asyncio.Lock()

    await send(mock_client, "http://localhost:8002", "img-data", writer, csv_lock)

    row = writer.writerow.call_args.args[0]
    assert row[1] == -1
