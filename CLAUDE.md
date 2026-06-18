# Polymarket Consensus Finder — Claude Context

## What this project does

Single-file Python CLI (`polymarket_consensus.py`) that finds Polymarket prediction market bets where the top-ranked traders (by all-time PnL) share the same position. Intended as a signal for high-conviction consensus trades.

## Architecture

Everything lives in one file. No framework, no database, no config files.

```
polymarket_consensus.py   # the whole program
requirements.txt          # aiohttp, rich
```

### Data flow

```
fetch_leaderboard()           # GET /v1/leaderboard?orderBy=PNL
    → list[Trader]

fetch_positions() × N         # GET /positions?user=<proxyWallet>  (concurrent)
    → list[(Trader, list[Position])]

find_consensus()              # group by (conditionId, outcome), filter by threshold
    → list[ConsensusResult]

display_rich() / display_plain()
```

### Key data types

- `Trader` — rank, proxyWallet, username, pnl, vol
- `Position` — conditionId, title, outcome, outcomeIndex, size, curPrice, currentValue, cashPnl, slug, endDate
- `ConsensusResult` — aggregated view: holder_count, top_n (denominator), holders list, avg_value, avg_cash_pnl

## API

Base URL: `https://data-api.polymarket.com` — fully public, no auth.

- `GET /v1/leaderboard` — params: `orderBy`, `timePeriod`, `limit` (max 50), `offset`
- `GET /positions` — params: `user` (proxyWallet address), `sizeThreshold`, `limit` (max 500), `offset`, `sortBy`

Rate limit: 150 req/10s. The script uses `asyncio.Semaphore(10)` to stay safe.

## Important behavior notes

- **Denominator choice** — `--active-only` sets the denominator to traders who have *any* open position. Without it, many top traders have no positions (e.g. 9/25), making it nearly impossible to hit a 50% threshold. The `ConsensusResult.top_n` field always reflects the denominator actually used.
- **Pagination** — leaderboard pages at 50/request; positions page at 500/request. Both are handled automatically.
- **Position filtering** — positions below `--min-value` USD are skipped before consensus calculation.

## Running

```bash
pip install -r requirements.txt
python polymarket_consensus.py --top-n 25 --threshold 0.3 --active-only
```

## Extending

- To filter by market category, add `--category` and pass it to the leaderboard query (API supports: POLITICS, SPORTS, CRYPTO, etc.)
- To export results as JSON, add `--json` flag and `json.dumps([dataclasses.asdict(r) for r in results])`
- To schedule periodic runs, wrap `main()` in a cron or use Windows Task Scheduler
