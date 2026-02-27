from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import requests
import websockets
from rich.columns import Columns
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

GAMMA_BASE = "https://gamma-api.polymarket.com"
MARKET_WSS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _parse_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            return []
    return []


def _event_slug_from_url(url: str) -> str:
    part = url.split("/event/", 1)[-1]
    return part.strip("/").split("?")[0]


def _select_market(
    event: dict[str, Any],
    *,
    date_text: str | None,
    market_slug: str | None,
) -> dict[str, Any]:
    markets = event.get("markets") or []
    if not markets:
        raise ValueError("No markets found in event")

    if market_slug:
        needle_slug = market_slug.strip().lower()
        for market in markets:
            slug = str(market.get("slug") or "").lower()
            if slug == needle_slug:
                return market
        raise ValueError(f"No market found in event for market slug: {market_slug}")

    if date_text:
        needle = date_text.lower()
        for market in markets:
            q = str(market.get("question") or "").lower()
            if needle in q:
                return market
        raise ValueError(f"No market found in event for date text: {date_text}")

    if len(markets) == 1:
        return markets[0]

    options = ", ".join(str(m.get("slug") or "") for m in markets[:8])
    raise ValueError(
        "Event has multiple markets; pass --date-text or --market-slug. "
        f"Example market slugs: {options}"
    )


def _fetch_event_by_slug(session: requests.Session, slug: str) -> dict[str, Any]:
    resp = session.get(f"{GAMMA_BASE}/events", params={"slug": slug, "limit": 1}, timeout=20)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        raise ValueError(f"Event not found for slug: {slug}")
    return rows[0]


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _price_key(value: Any) -> str:
    return format(_to_decimal(value).quantize(Decimal("0.0001")), "f")


def _fmt_price_cents(price: Any) -> str:
    p = _to_decimal(price)
    if p <= Decimal("1"):
        p = p * Decimal("100")
    p = p.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if p == p.to_integral_value():
        return f"{int(p):>5}¢"
    return f"{p:>5}¢"


def _fmt_size(size: Any) -> str:
    s = _to_decimal(size).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{int(s):,} contracts"


def _size_bar(size: Decimal, max_size: Decimal, *, width: int = 18, style: str) -> Text:
    if size <= 0 or max_size <= 0:
        return Text("." * width, style="grey27")

    filled = int((size * Decimal(width) / max_size).to_integral_value(rounding=ROUND_HALF_UP))
    filled = max(1, min(width, filled))

    bar = Text()
    bar.append("#" * filled, style=style)
    bar.append("." * (width - filled), style="grey27")
    return bar


def _iter_levels(levels: Any) -> list[dict[str, Any]]:
    if not isinstance(levels, list):
        return []
    return [x for x in levels if isinstance(x, dict)]


def _levels_desc(level_map: dict[str, Decimal], depth: int) -> list[tuple[Decimal, Decimal]]:
    rows = [(_to_decimal(price), size) for price, size in level_map.items() if size > 0]
    rows.sort(key=lambda t: t[0], reverse=True)
    return rows[:depth]


def _levels_asc(level_map: dict[str, Decimal], depth: int) -> list[tuple[Decimal, Decimal]]:
    rows = [(_to_decimal(price), size) for price, size in level_map.items() if size > 0]
    rows.sort(key=lambda t: t[0])
    return rows[:depth]


def _build_side_table(
    *,
    outcome: str,
    asks: list[tuple[Decimal, Decimal]],
    bids: list[tuple[Decimal, Decimal]],
    best_bid: Decimal | None,
    best_ask: Decimal | None,
) -> Table:
    t = Table(show_header=False, box=None, padding=(0, 1), expand=True)
    t.add_column("price", justify="right", style="bold bright_red", no_wrap=True)
    t.add_column("size", justify="right", style="white", no_wrap=True)
    t.add_column("bar", justify="left", no_wrap=True)

    all_sizes = [size for _, size in asks] + [size for _, size in bids]
    max_size = max(all_sizes) if all_sizes else Decimal("0")

    for price, size in asks:
        style = "bold bright_red" if best_ask is not None and price == best_ask else "red3"
        t.add_row(
            f"[{style}]{_fmt_price_cents(price)}[/{style}]",
            f"[white]{_fmt_size(size)}[/white]",
            _size_bar(size, max_size, style=style),
        )

    t.add_row(f"[bold cyan]-- {outcome.upper()} Bids --[/bold cyan]", "", "")

    for price, size in bids:
        style = "bold bright_green" if best_bid is not None and price == best_bid else "green3"
        t.add_row(
            f"[{style}]{_fmt_price_cents(price)}[/{style}]",
            f"[white]{_fmt_size(size)}[/white]",
            _size_bar(size, max_size, style=style),
        )

    return t


def _build_outcome_panel(
    *,
    outcome: str,
    token_id: str,
    state: dict[str, Any],
    depth: int,
) -> Panel:
    # Display asks from far-to-near so the levels closest to mid sit near the bids divider.
    asks = list(reversed(_levels_asc(state["asks"], depth)))
    bids = _levels_desc(state["bids"], depth)
    best_bid: Decimal | None = state.get("best_bid")
    best_ask: Decimal | None = state.get("best_ask")

    spread_text = "-"
    mid_text = "-"
    if best_bid is not None and best_ask is not None and best_ask >= best_bid:
        spread = (best_ask - best_bid) * Decimal("100")
        mid = ((best_ask + best_bid) / Decimal("2")) * Decimal("100")
        spread_text = f"{spread.quantize(Decimal('0.1'))}¢"
        mid_text = f"{mid.quantize(Decimal('0.1'))}¢"

    subtitle = Text()
    subtitle.append(f"token {token_id[:10]}...", style="dim")
    subtitle.append("  •  ", style="dim")
    subtitle.append(f"mid {mid_text}", style="bright_cyan")
    subtitle.append("  •  ", style="dim")
    subtitle.append(f"spread {spread_text}", style="magenta")

    content = Group(
        subtitle,
        Rule(style="grey27"),
        _build_side_table(outcome=outcome, asks=asks, bids=bids, best_bid=best_bid, best_ask=best_ask),
    )

    return Panel(content, border_style="grey27", title=f"[bold yellow]{outcome}[/bold yellow]")


def _render_dashboard(
    *,
    event_title: str,
    market_question: str,
    last_update_text: str,
    books: dict[str, dict[str, Any]],
    token_to_outcome: dict[str, str],
    depth: int,
) -> Panel:
    header = Text()
    header.append(event_title + "\n", style="bold white")
    if market_question and market_question != event_title:
        header.append(market_question + "\n", style="grey82")
    header.append(f"Updated {last_update_text}", style="grey58")

    ordered_tokens = sorted(token_to_outcome.items(), key=lambda kv: kv[1].lower())
    panels: list[Panel] = []
    for token_id, outcome in ordered_tokens:
        state = books[token_id]
        panels.append(_build_outcome_panel(outcome=outcome, token_id=token_id, state=state, depth=depth))

    body = Group(header, Rule(style="grey27"), Columns(panels, equal=True, expand=True))
    return Panel(body, border_style="grey27", title="[bold white]Polymarket CLOB[/bold white]")


def _apply_book_snapshot(state: dict[str, Any], msg: dict[str, Any]) -> None:
    bids_map: dict[str, Decimal] = {}
    asks_map: dict[str, Decimal] = {}

    for lvl in _iter_levels(msg.get("bids") or msg.get("buys")):
        price = _to_decimal(lvl.get("price"))
        size = _to_decimal(lvl.get("size"))
        if size > 0:
            bids_map[_price_key(price)] = size

    for lvl in _iter_levels(msg.get("asks") or msg.get("sells")):
        price = _to_decimal(lvl.get("price"))
        size = _to_decimal(lvl.get("size"))
        if size > 0:
            asks_map[_price_key(price)] = size

    state["bids"] = bids_map
    state["asks"] = asks_map

    if bids_map:
        state["best_bid"] = max(_to_decimal(p) for p in bids_map)
    if asks_map:
        state["best_ask"] = min(_to_decimal(p) for p in asks_map)


def _apply_price_change(books: dict[str, dict[str, Any]], msg: dict[str, Any]) -> bool:
    changed = False
    for item in _iter_levels(msg.get("price_changes")):
        token_id = str(item.get("asset_id") or "")
        if token_id not in books:
            continue
        state = books[token_id]

        best_bid_raw = item.get("best_bid")
        best_ask_raw = item.get("best_ask")
        if best_bid_raw is not None:
            state["best_bid"] = _to_decimal(best_bid_raw)
            changed = True
        if best_ask_raw is not None:
            state["best_ask"] = _to_decimal(best_ask_raw)
            changed = True

    return changed


def _timestamp_text(msg: dict[str, Any]) -> str:
    ts = msg.get("timestamp")
    if ts:
        try:
            return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).isoformat()
        except (ValueError, TypeError):
            return str(ts)
    return datetime.now(timezone.utc).isoformat()


def _fit_depth_to_terminal(requested_depth: int) -> int:
    # Approximate available rows for both sides after headers/panel chrome.
    rows = shutil.get_terminal_size((120, 40)).lines
    max_per_side = max(4, (rows - 14) // 2)
    return max(1, min(requested_depth, max_per_side))


async def stream_market_orderbook(
    *,
    event_title: str,
    market_question: str,
    token_ids: list[str],
    token_to_outcome: dict[str, str],
    depth: int,
) -> None:
    subscription = {
        "assets_ids": token_ids,
        "type": "market",
        "custom_feature_enabled": True,
    }

    books: dict[str, dict[str, Any]] = {
        token_id: {"asks": {}, "bids": {}, "best_bid": None, "best_ask": None}
        for token_id in token_ids
    }
    last_update_text = "waiting for feed..."

    with Live(
        _render_dashboard(
            event_title=event_title,
            market_question=market_question,
            last_update_text=last_update_text,
            books=books,
            token_to_outcome=token_to_outcome,
            depth=depth,
        ),
        refresh_per_second=10,
        screen=True,
    ) as live:
        reconnect_delay = 1.0
        while True:
            try:
                async with websockets.connect(MARKET_WSS, ping_interval=20, ping_timeout=40) as ws:
                    await ws.send(json.dumps(subscription))
                    reconnect_delay = 1.0

                    while True:
                        raw = await ws.recv()
                        payload = json.loads(raw)

                        if isinstance(payload, list):
                            items = [x for x in payload if isinstance(x, dict)]
                        elif isinstance(payload, dict):
                            items = [payload]
                        else:
                            continue

                        changed = False
                        for msg in items:
                            event_type = str(msg.get("event_type") or "")

                            if event_type == "book" or ("asset_id" in msg and ("bids" in msg or "asks" in msg)):
                                token_id = str(msg.get("asset_id") or "")
                                if token_id in books:
                                    _apply_book_snapshot(books[token_id], msg)
                                    changed = True

                            elif event_type == "price_change":
                                if _apply_price_change(books, msg):
                                    changed = True

                            if changed:
                                last_update_text = _timestamp_text(msg)

                        if changed:
                            live.update(
                                _render_dashboard(
                                    event_title=event_title,
                                    market_question=market_question,
                                    last_update_text=last_update_text,
                                    books=books,
                                    token_to_outcome=token_to_outcome,
                                    depth=depth,
                                )
                            )
            except (websockets.exceptions.ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                now = datetime.now(timezone.utc).isoformat()
                last_update_text = (
                    f"feed disconnected ({exc}); reconnecting in {reconnect_delay:.1f}s at {now}"
                )
                live.update(
                    _render_dashboard(
                        event_title=event_title,
                        market_question=market_question,
                        last_update_text=last_update_text,
                        books=books,
                        token_to_outcome=token_to_outcome,
                        depth=depth,
                    )
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(30.0, reconnect_delay * 2.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream live Polymarket orderbook for a selected market.")
    parser.add_argument(
        "--event-url",
        default="https://polymarket.com/event/us-strikes-iran-by",
        help="Event URL (must contain /event/<slug>).",
    )
    parser.add_argument(
        "--date-text",
        default=None,
        help="Optional date text to match in market question.",
    )
    parser.add_argument(
        "--market-slug",
        default=None,
        help="Optional exact market slug to select (useful for multi-market events).",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=16,
        help="Max levels per side; auto-clamped to terminal height.",
    )
    args = parser.parse_args()

    event_slug = _event_slug_from_url(args.event_url)

    with requests.Session() as session:
        event = _fetch_event_by_slug(session, event_slug)
    market = _select_market(event, date_text=args.date_text, market_slug=args.market_slug)

    token_ids = [str(x) for x in _parse_list(market.get("clobTokenIds"))]
    outcomes = [str(x) for x in _parse_list(market.get("outcomes"))]
    if not token_ids:
        raise ValueError("No clobTokenIds found for selected market")

    token_to_outcome: dict[str, str] = {}
    for i, token_id in enumerate(token_ids):
        label = outcomes[i] if i < len(outcomes) else f"token_{i}"
        token_to_outcome[token_id] = label

    asyncio.run(
        stream_market_orderbook(
            event_title=str(event.get("title") or ""),
            market_question=str(market.get("question") or ""),
            token_ids=token_ids,
            token_to_outcome=token_to_outcome,
            depth=_fit_depth_to_terminal(max(1, args.depth)),
        )
    )


if __name__ == "__main__":
    main()
