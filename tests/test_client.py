from __future__ import annotations

from typing import Any
from unittest.mock import Mock
import unittest

from polymarket_api import APIError, PolymarketClient


def _mock_response(
    *,
    ok: bool = True,
    status_code: int = 200,
    json_data: Any = None,
    reason: str = "OK",
    text: str = "",
) -> Mock:
    response = Mock()
    response.ok = ok
    response.status_code = status_code
    response.reason = reason
    response.text = text
    response.json.return_value = json_data
    return response


class TestPolymarketClient(unittest.TestCase):
    def test_get_event_by_slug_uses_expected_path(self) -> None:
        session = Mock()
        session.get.return_value = _mock_response(json_data={"slug": "my-event"})
        client = PolymarketClient(session=session)

        result = client.get_event_by_slug("my-event")

        self.assertEqual(result["slug"], "my-event")
        session.get.assert_called_once_with(
            "https://gamma-api.polymarket.com/events/slug/my-event",
            params={},
            timeout=15.0,
        )

    def test_list_events_passes_filters(self) -> None:
        session = Mock()
        session.get.return_value = _mock_response(json_data=[])
        client = PolymarketClient(session=session)

        client.list_events(active=True, closed=False, limit=25, offset=50, slug=None)

        session.get.assert_called_once_with(
            "https://gamma-api.polymarket.com/events",
            params={"active": True, "closed": False, "limit": 25, "offset": 50},
            timeout=15.0,
        )

    def test_iter_events_paginates_until_short_page(self) -> None:
        session = Mock()
        session.get.side_effect = [
            _mock_response(json_data=[{"id": 1}, {"id": 2}]),
            _mock_response(json_data=[{"id": 3}]),
        ]
        client = PolymarketClient(session=session)

        result = list(client.iter_events(page_size=2, active=True))

        self.assertEqual([row["id"] for row in result], [1, 2, 3])
        self.assertEqual(session.get.call_count, 2)

    def test_request_error_raises_api_error(self) -> None:
        session = Mock()
        session.get.return_value = _mock_response(
            ok=False, status_code=500, reason="Server Error", text="boom"
        )
        client = PolymarketClient(session=session)

        with self.assertRaises(APIError) as exc:
            client.list_markets(limit=1)

        self.assertEqual(exc.exception.status_code, 500)
        self.assertEqual(exc.exception.body, "boom")
