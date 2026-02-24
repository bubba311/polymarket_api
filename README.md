# polymarket-api

Python client for Polymarket Gamma market-data endpoints documented at:
https://docs.polymarket.com/market-data/fetching-markets

## Install

```bash
pip install -e .
```

## Quick start

```python
from polymarket_api import PolymarketClient

client = PolymarketClient()

# Fetch active events
events = client.list_events(active=True, closed=False, limit=10)

# Fetch a market by slug
market = client.get_market_by_slug("trump-wins-2028")

# Iterate all open markets with pagination
for item in client.iter_markets(active=True, closed=False, page_size=100):
    print(item["question"])
```

## Supported endpoints

- `GET /events`
- `GET /events/slug/{slug}`
- `GET /markets`
- `GET /markets/slug/{slug}`
- `GET /tags`
- `GET /sports`

## Live orderbook stream (websocket)

This package includes a terminal stream for CLOB market books:

```bash
polymarket-live-orderbook \
  --event-url "https://polymarket.com/event/us-strikes-iran-by" \
  --date-text "February 28, 2026"
```

It resolves the event slug, finds the market whose question includes the date text, then subscribes to:
`wss://ws-subscriptions-clob.polymarket.com/ws/market`

The view uses a colorized, continuously refreshing terminal dashboard.
