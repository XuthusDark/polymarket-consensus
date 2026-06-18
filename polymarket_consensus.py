#!/usr/bin/env python3
"""
Polymarket Consensus Finder

Fetches the top N Polymarket traders by all-time PnL and finds markets
where a configurable fraction of them all hold the same position.
"""

import asyncio
import argparse
import sys
from dataclasses import dataclass
from collections import defaultdict
from typing import Optional

try:
    import aiohttp
except ImportError:
    print("Missing dependency: pip install aiohttp")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

BASE_URL = "https://data-api.polymarket.com"
POLYMARKET_URL = "https://polymarket.com/event"

# Stay well within the 150 req/10s limit
MAX_CONCURRENT_FETCHES = 10


@dataclass
class Trader:
    rank: str
    proxy_wallet: str
    username: str
    pnl: float
    vol: float


@dataclass
class Position:
    condition_id: str
    title: str
    outcome: str
    outcome_index: int
    size: float
    cur_price: float
    current_value: float
    cash_pnl: float
    slug: str
    end_date: str


@dataclass
class ConsensusResult:
    condition_id: str
    title: str
    outcome: str
    cur_price: float
    holder_count: int
    top_n: int
    holders: list
    avg_value: float
    avg_cash_pnl: float
    end_date: str
    slug: str

    @property
    def consensus_pct(self) -> float:
        return self.holder_count / self.top_n if self.top_n > 0 else 0.0


async def fetch_leaderboard(
    session: aiohttp.ClientSession,
    top_n: int,
    time_period: str,
) -> list:
    """Fetch top N traders by PnL, paginating as needed (API max is 50/page)."""
    traders = []
    page_size = 50  # API maximum

    while len(traders) < top_n:
        need = top_n - len(traders)
        params = {
            "orderBy": "PNL",
            "timePeriod": time_period,
            "limit": min(page_size, need),
            "offset": len(traders),
        }

        async with session.get(f"{BASE_URL}/v1/leaderboard", params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if not data:
            break

        for entry in data:
            wallet = entry.get("proxyWallet", "")
            name = entry.get("userName") or (wallet[:10] + "..." if wallet else "unknown")
            traders.append(Trader(
                rank=str(entry.get("rank", "?")),
                proxy_wallet=wallet,
                username=name,
                pnl=entry.get("pnl", 0) or 0,
                vol=entry.get("vol", 0) or 0,
            ))

        if len(data) < params["limit"]:
            break  # No more pages

    return traders[:top_n]


async def fetch_positions(
    session: aiohttp.ClientSession,
    trader: Trader,
    min_value: float,
    semaphore: asyncio.Semaphore,
) -> tuple:
    """Fetch all open positions for a single trader above the value threshold."""
    async with semaphore:
        all_positions = []
        offset = 0
        page_size = 500  # API max

        while True:
            params = {
                "user": trader.proxy_wallet,
                "sizeThreshold": 1,
                "limit": page_size,
                "offset": offset,
                "sortBy": "CURRENT",
                "sortDirection": "DESC",
            }

            try:
                async with session.get(f"{BASE_URL}/positions", params=params) as resp:
                    if resp.status == 404:
                        break
                    resp.raise_for_status()
                    data = await resp.json()
            except Exception as exc:
                print(f"  Warning: failed to fetch positions for {trader.username}: {exc}",
                      file=sys.stderr)
                break

            if not data:
                break

            for p in data:
                current_value = p.get("currentValue") or 0
                if current_value < min_value:
                    continue
                condition_id = p.get("conditionId", "")
                if not condition_id:
                    continue
                all_positions.append(Position(
                    condition_id=condition_id,
                    title=p.get("title", "Unknown market"),
                    outcome=p.get("outcome", ""),
                    outcome_index=p.get("outcomeIndex", 0),
                    size=p.get("size") or 0,
                    cur_price=p.get("curPrice") or 0,
                    current_value=current_value,
                    cash_pnl=p.get("cashPnl") or 0,
                    slug=p.get("slug", ""),
                    end_date=p.get("endDate", ""),
                ))

            if len(data) < page_size:
                break  # Last page
            offset += page_size

        return trader, all_positions


def find_consensus(
    trader_positions: list,
    threshold: float,
    top_n: int,
    active_only: bool,
) -> tuple:
    """
    Return markets where >= threshold fraction of traders hold the same outcome.

    active_only=True  → denominator is traders who have any open position
    active_only=False → denominator is all top_n traders (more conservative)
    """
    # (condition_id, outcome) -> [(trader, position), ...]
    market_holders: dict = defaultdict(list)
    market_meta: dict = {}

    active_traders = sum(1 for _, positions in trader_positions if positions)
    denominator = active_traders if active_only else top_n

    for trader, positions in trader_positions:
        for pos in positions:
            key = (pos.condition_id, pos.outcome)
            market_holders[key].append((trader, pos))
            market_meta[key] = pos

    results = []
    for (condition_id, outcome), holders in market_holders.items():
        pct = len(holders) / denominator if denominator else 0
        if pct < threshold:
            continue

        meta = market_meta[(condition_id, outcome)]
        avg_value = sum(p.current_value for _, p in holders) / len(holders)
        avg_pnl = sum(p.cash_pnl for _, p in holders) / len(holders)

        results.append(ConsensusResult(
            condition_id=condition_id,
            title=meta.title,
            outcome=outcome,
            cur_price=meta.cur_price,
            holder_count=len(holders),
            top_n=denominator,
            holders=[t.username for t, _ in holders],
            avg_value=avg_value,
            avg_cash_pnl=avg_pnl,
            end_date=meta.end_date,
            slug=meta.slug,
        ))

    # Sort: consensus % descending, then by price proximity to 0.5
    results.sort(key=lambda r: (r.consensus_pct, -abs(r.cur_price - 0.5)), reverse=True)
    return results, active_traders


def _price_color(price: float) -> str:
    if price >= 0.75:
        return "green"
    if price >= 0.4:
        return "yellow"
    return "red"


def display_rich(results: list, top_n: int, active_traders: int, threshold: float) -> None:
    console = Console()

    denom_note = (f"[dim]{active_traders} active / {top_n} total[/dim]"
                  if active_traders != top_n else f"[dim]{top_n} traders[/dim]")

    if not results:
        console.print(
            f"\n[yellow]No consensus trades found.[/yellow] "
            f"Try lowering [bold]--threshold[/bold] (currently {threshold:.0%}) "
            f"or use [bold]--active-only[/bold] to score against active traders only."
        )
        console.print(f"  Active traders with open positions: {active_traders}/{top_n}")
        return

    console.print(f"\n[bold green]Polymarket Consensus Trades[/bold green]  "
                  f"{denom_note} · threshold {threshold:.0%} · "
                  f"{len(results)} markets\n")

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan",
                  show_lines=False)
    table.add_column("#",         style="dim",      width=3)
    table.add_column("Market",    min_width=38,      no_wrap=False)
    table.add_column("Side",      width=5)
    table.add_column("Price",     justify="right",   width=7)
    table.add_column("Consensus", justify="right",   width=10)
    table.add_column("Holders",   justify="right",   width=8)
    table.add_column("Avg $",     justify="right",   width=9)
    table.add_column("Ends",      width=11)

    for i, r in enumerate(results, 1):
        title_text = r.title if len(r.title) <= 50 else r.title[:49] + "…"
        url = f"{POLYMARKET_URL}/{r.slug}" if r.slug else ""
        title_cell = f"[link={url}]{title_text}[/link]" if url else title_text
        color = _price_color(r.cur_price)

        table.add_row(
            str(i),
            title_cell,
            r.outcome[:5],
            f"[{color}]{r.cur_price:.0%}[/{color}]",
            f"[bold]{r.consensus_pct:.0%}[/bold]",
            f"{r.holder_count}/{r.top_n}",
            f"${r.avg_value:,.0f}",
            r.end_date[:10] if r.end_date else "—",
        )

    console.print(table)

    console.print("\n[dim bold]Top 10 — holders & trade links:[/dim bold]")
    for i, r in enumerate(results[:10], 1):
        names = ", ".join(r.holders[:6])
        if len(r.holders) > 6:
            names += f" [dim]+{len(r.holders) - 6} more[/dim]"
        url = f"{POLYMARKET_URL}/{r.slug}" if r.slug else ""
        console.print(f"  [bold]{i}.[/bold] {r.title[:60]}")
        console.print(f"     Holders: [cyan]{names}[/cyan]")
        if url:
            console.print(f"     Trade:   [underline][link={url}]{url}[/link][/underline]")


def display_plain(results: list, top_n: int, active_traders: int, threshold: float) -> None:
    denom_note = (f"{active_traders} active/{top_n} total"
                  if active_traders != top_n else f"{top_n} traders")
    if not results:
        print(f"\nNo consensus trades found (threshold {threshold:.0%}).")
        print(f"Active traders with positions: {active_traders}/{top_n}")
        print("Try --threshold 0.3 or --active-only")
        return

    print(f"\n{'='*70}")
    print(f"POLYMARKET CONSENSUS TRADES")
    denom = results[0].top_n if results else top_n
    print(f"{denom_note} (denominator={denom}) | Threshold: {threshold:.0%} | {len(results)} markets found")
    print(f"{'='*70}")

    for i, r in enumerate(results, 1):
        url = f"{POLYMARKET_URL}/{r.slug}" if r.slug else "N/A"
        print(f"\n{i}. {r.title}")
        print(f"   Outcome:   {r.outcome}  |  Price: {r.cur_price:.0%}  |  "
              f"Consensus: {r.consensus_pct:.0%} ({r.holder_count}/{r.top_n})")
        print(f"   Holders:   {', '.join(r.holders[:6])}"
              + (f" +{len(r.holders)-6} more" if len(r.holders) > 6 else ""))
        print(f"   Avg value: ${r.avg_value:,.0f}  |  Avg P&L: ${r.avg_cash_pnl:,.0f}")
        print(f"   Ends:  {r.end_date[:10] if r.end_date else 'N/A'}")
        print(f"   Trade: {url}")


async def main(args: argparse.Namespace) -> None:
    print(f"Fetching top {args.top_n} traders by PnL "
          f"(time period: {args.time_period})...", flush=True)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
    headers = {"User-Agent": "polymarket-consensus/1.0"}
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        try:
            traders = await fetch_leaderboard(session, args.top_n, args.time_period)
        except aiohttp.ClientResponseError as exc:
            print(f"Error fetching leaderboard: HTTP {exc.status} – {exc.message}")
            sys.exit(1)

        if not traders:
            print("Leaderboard returned no results.")
            sys.exit(1)

        actual_n = len(traders)
        print(f"Got {actual_n} traders. Fetching positions concurrently...", flush=True)

        tasks = [
            fetch_positions(session, trader, args.min_value, semaphore)
            for trader in traders
        ]
        trader_positions = await asyncio.gather(*tasks)

    traders_with_data = sum(1 for _, p in trader_positions if p)
    total_positions = sum(len(p) for _, p in trader_positions)
    print(f"Fetched {total_positions} positions "
          f"across {traders_with_data}/{actual_n} traders.", flush=True)

    if total_positions == 0:
        print("No positions found. Try lowering --min-value.")
        sys.exit(0)

    results, active_traders = find_consensus(
        trader_positions, args.threshold, actual_n, args.active_only
    )

    if HAS_RICH and not args.plain:
        display_rich(results, actual_n, active_traders, args.threshold)
    else:
        display_plain(results, actual_n, active_traders, args.threshold)


HOW_TO_RUN = """
Usage:
  python polymarket_consensus.py --top-n N --threshold T [options]

Required:
  --top-n N        How many top traders (by PnL) to analyze  e.g. 25, 50
  --threshold T    Fraction that must hold the same position  e.g. 0.3, 0.5

Optional:
  --time-period    DAY | WEEK | MONTH | ALL  (default: ALL)
  --min-value USD  Ignore positions worth less than this in USD  (default: 10)
  --active-only    Denominate against traders with open positions only
                   (recommended — many top traders have no positions at any moment)
  --plain          Plain text output, no color

Examples:
  python polymarket_consensus.py --top-n 25 --threshold 0.3 --active-only
  python polymarket_consensus.py --top-n 50 --threshold 0.4 --time-period WEEK
  python polymarket_consensus.py --top-n 25 --threshold 0.5 --min-value 100 --plain
"""


class _HelpOnErrorParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(f"Error: {message}\n{HOW_TO_RUN}", file=sys.stderr)
        sys.exit(2)


def parse_args() -> argparse.Namespace:
    parser = _HelpOnErrorParser(
        description="Find Polymarket bets where the top N traders share the same position.",
        add_help=True,
    )
    parser.add_argument(
        "--top-n", type=int, required=True, metavar="N",
        help="Number of top traders to analyze",
    )
    parser.add_argument(
        "--threshold", type=float, required=True, metavar="FLOAT",
        help="Min fraction of top-N traders holding the same position, e.g. 0.3",
    )
    parser.add_argument(
        "--time-period", choices=["DAY", "WEEK", "MONTH", "ALL"], default="ALL",
        metavar="PERIOD",
        help="Leaderboard time period: DAY, WEEK, MONTH, ALL (default: ALL)",
    )
    parser.add_argument(
        "--min-value", type=float, default=10.0, metavar="USD",
        help="Minimum position current value in USD to include (default: $10)",
    )
    parser.add_argument(
        "--active-only", action="store_true",
        help="Score consensus against traders who have any open position rather than all top-N",
    )
    parser.add_argument(
        "--plain", action="store_true",
        help="Plain text output (no rich formatting)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.top_n < 1:
        print("--top-n must be at least 1")
        sys.exit(1)
    if not 0 < args.threshold <= 1:
        print("--threshold must be between 0 (exclusive) and 1 (inclusive)")
        sys.exit(1)

    asyncio.run(main(args))
