from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


JsonDict = Dict[str, Any]


@dataclass
class APIError(Exception):
    status_code: int
    message: str
    body: Optional[str] = None

    def __str__(self) -> str:
        return f"Polymarket API error {self.status_code}: {self.message}"


class PolymarketClient:
    """
    Client for Polymarket Gamma market-data endpoints.

    Docs:
    - https://docs.polymarket.com/market-data/fetching-markets
    """

    def __init__(
        self,
        base_url: str = "https://gamma-api.polymarket.com",
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or self._build_default_session()

    @staticmethod
    def _build_default_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            backoff_factor=0.2,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _request(self, path: str, params: Optional[JsonDict] = None) -> Any:
        url = f"{self.base_url}{path}"
        response = self.session.get(
            url,
            params=self._clean_params(params or {}),
            timeout=self.timeout,
        )
        if not response.ok:
            body = response.text
            raise APIError(
                status_code=response.status_code,
                message=response.reason or "request failed",
                body=body,
            )
        return response.json()

    @staticmethod
    def _clean_params(params: JsonDict) -> JsonDict:
        clean: JsonDict = {}
        for key, value in params.items():
            if value is None:
                continue
            clean[key] = value
        return clean

    def list_events(self, **filters: Any) -> list[JsonDict]:
        """
        GET /events
        Supports filters documented by Polymarket, including:
        limit, offset, order, ascending, active, closed, archived,
        slug, tag_id, related_tags, liquidity_num_min, volume_num_min, etc.
        """
        return self._request("/events", params=filters)

    def get_event_by_slug(self, slug: str) -> JsonDict:
        """GET /events/slug/{slug}"""
        return self._request(f"/events/slug/{slug}")

    def list_markets(self, **filters: Any) -> list[JsonDict]:
        """
        GET /markets
        Supports filters documented by Polymarket, including:
        limit, offset, order, ascending, active, closed, archived,
        slug, tag_id, related_tags, end_date_min, end_date_max,
        clob_token_ids, condition_ids, etc.
        """
        return self._request("/markets", params=filters)

    def get_market_by_slug(self, slug: str) -> JsonDict:
        """GET /markets/slug/{slug}"""
        return self._request(f"/markets/slug/{slug}")

    def list_tags(self, **filters: Any) -> list[JsonDict]:
        """GET /tags"""
        return self._request("/tags", params=filters)

    def list_sports(self) -> list[JsonDict]:
        """GET /sports"""
        return self._request("/sports")

    def iter_events(self, page_size: int = 100, **filters: Any) -> Iterator[JsonDict]:
        """
        Iterates through /events using offset pagination.
        Stops when a page returns fewer items than page_size.
        """
        offset = int(filters.pop("offset", 0))
        while True:
            page_filters = dict(filters)
            page_filters["limit"] = page_size
            page_filters["offset"] = offset
            page = self.list_events(**page_filters)
            if not page:
                return
            for item in page:
                yield item
            if len(page) < page_size:
                return
            offset += page_size

    def iter_markets(self, page_size: int = 100, **filters: Any) -> Iterator[JsonDict]:
        """
        Iterates through /markets using offset pagination.
        Stops when a page returns fewer items than page_size.
        """
        offset = int(filters.pop("offset", 0))
        while True:
            page_filters = dict(filters)
            page_filters["limit"] = page_size
            page_filters["offset"] = offset
            page = self.list_markets(**page_filters)
            if not page:
                return
            for item in page:
                yield item
            if len(page) < page_size:
                return
            offset += page_size
