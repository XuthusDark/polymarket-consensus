# Polymarket Consensus Finder

Fetches the top N Polymarket traders by all-time profit (PnL) and surfaces markets where a configurable fraction of them all hold the same position — a simple signal for high-conviction, consensus bets.

Uses Polymarket's fully public [Data API](https://data-api.polymarket.com) — no API key required.

## How it works

1. Pulls the top N traders from the Polymarket leaderboard (ranked by PnL)
2. Concurrently fetches each trader's open positions
3. Groups positions by market + outcome
4. Returns markets where ≥ threshold % of traders hold the same side

## Requirements

- Python 3.10+
- pip packages: `aiohttp`, `rich`

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Default: top 25 traders, 50% consensus threshold, all-time leaderboard
python polymarket_consensus.py

# Most useful combo — 30% of currently-active top traders agree
python polymarket_consensus.py --top-n 25 --threshold 0.3 --active-only

# Top 50 traders, 40% threshold, weekly leaderboard
python polymarket_consensus.py --top-n 50 --threshold 0.4 --time-period WEEK

# Only count large positions ($100+ current value), plain text output
python polymarket_consensus.py --min-value 100 --plain
```

## Options

| Flag | Default | Description |
|---|---|---|
| `--top-n N` | 25 | Number of top traders to analyze |
| `--threshold FLOAT` | 0.5 | Fraction of traders that must hold the same position (e.g. `0.5` = 50%) |
| `--time-period` | `ALL` | Leaderboard window: `DAY`, `WEEK`, `MONTH`, `ALL` |
| `--min-value USD` | 10 | Minimum position current value in USD |
| `--active-only` | off | Score consensus against traders who have *any* open position rather than all top-N. Recommended — many top traders have no open positions at a given moment |
| `--plain` | off | Plain text output (no rich color formatting) |

## Understanding the output

```
Will Czechia win on 2026-06-18?
  Outcome:   Yes  |  Price: 52%  |  Consensus: 44% (4/9)
  Holders:   swisstony, RN1, BreakTheBank, GamblingIsAllYouNeed
  Avg value: $51,140  |  Avg P&L: $155
```

- **Price** — current market probability (e.g. 52% = market implies 52% chance of YES)
- **Consensus** — fraction of top traders holding this outcome (4 of 9 active traders)
- **Avg value** — average USD value of the position across holders
- **Avg P&L** — average unrealized profit/loss on this specific position

## Practical tips

- Run with `--active-only` — on any given day only ~30–50% of top traders have open positions, so the raw top-N denominator is too harsh
- Start with `--threshold 0.3` and tighten from there
- High-price outcomes (>80%) with consensus are near-certainty plays; mid-range outcomes (40–60%) with consensus are where top traders disagree with the market
- Re-run periodically — positions change as markets resolve and traders open new ones

## Rate limits

The Data API allows 150 requests per 10 seconds. This script caps concurrent fetches at 10, well within that limit.
