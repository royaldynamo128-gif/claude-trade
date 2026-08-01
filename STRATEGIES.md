# Strategy Guide

This document explains how to add, remove, compare, backtest, and optimize
strategies in ClaudeTrade. All strategies live in `strategies/`.

---

## Included Strategies

| File | Class | Description |
|---|---|---|
| `CTSampleStrategy.py` | `CTSampleStrategy` | Freqtrade built-in template — baseline for verification |

---

## Adding a Community Strategy

### Method 1: Manual Copy

1. Find a strategy at https://github.com/freqtrade/freqtrade-strategies
2. Copy the `.py` file into `strategies/`
3. Check its `class` name (e.g., `class NostalgiaForInfinity(IStrategy):`)
4. Test it compiles correctly:
   ```bash
   source venv/bin/activate
   freqtrade test-strategy \
       --strategy NostalgiaForInfinity \
       --strategy-path strategies \
       --userdir user_data \
       --config config/config.demo.json
   ```
5. Run it: `STRATEGY=NostalgiaForInfinity ./scripts/start.sh`

### Method 2: Git Submodule (for tracking upstream changes)

```bash
git submodule add https://github.com/freqtrade/freqtrade-strategies community_strategies
# Then symlink or copy specific strategies into strategies/
cp community_strategies/user_data/strategies/NostalgiaForInfinity.py strategies/
```

---

## Removing a Strategy

Simply delete the `.py` file from `strategies/`. The bot must not be currently
running that strategy. Stop the bot first, then delete.

---

## Switching Active Strategy

In `scripts/start.sh`, change the `STRATEGY` default, or pass it as an env var:
```bash
STRATEGY=NostalgiaForInfinity ./scripts/start.sh
```

---

## Running Backtests

### Single strategy
```bash
STRATEGY=CTSampleStrategy ./scripts/backtest.sh
```

### Custom date range
```bash
STRATEGY=CTSampleStrategy TIMERANGE=20240101-20240630 ./scripts/backtest.sh
```

### Compare multiple strategies side-by-side
```bash
STRATEGY_LIST="CTSampleStrategy NostalgiaForInfinity BBRSIStrategy" ./scripts/backtest.sh
```

Results are saved to `user_data/backtest_results/`.

---

## Optimizing a Strategy (Hyperopt)

```bash
# Default (100 epochs, Sharpe loss)
./scripts/hyperopt.sh

# Custom
STRATEGY=CTSampleStrategy EPOCHS=300 LOSS=CalmarHyperOptLoss ./scripts/hyperopt.sh
```

### Available Loss Functions

| Loss | Best for |
|---|---|
| `SharpeHyperOptLoss` | Risk-adjusted returns (default) |
| `CalmarHyperOptLoss` | Max drawdown minimization |
| `SortinoHyperOptLoss` | Downside risk minimization |
| `MaxDrawDownHyperOptLoss` | Pure drawdown control |
| `ProfitDrawDownHyperOptLoss` | Profit/drawdown balance |

After hyperopt, apply the best parameters:
```bash
freqtrade hyperopt-show --userdir user_data --best
```
Copy the output ROI/stoploss/trailing values into your strategy file.

---

## Strategy Requirements for Binance Futures

For a strategy to work in futures mode, it must:
1. **Not hardcode `can_short = False`** — set `can_short = True` to enable shorting
2. **Use `:USDT` pair notation** — e.g., `BTC/USDT:USDT` (not `BTC/USDT`)
3. **Handle leverage** — either set `leverage_mode = "constant"` in config, or implement `leverage()` callback in strategy

Example snippet to add to any strategy for futures compatibility:
```python
# Add inside the strategy class
can_short = True  # Enable short trading
```

---

## Popular Community Strategies to Try

| Strategy | Repo | Notes |
|---|---|---|
| NostalgiaForInfinity | freqtrade/freqtrade-strategies | Complex, many indicators, proven |
| BBRSIStrategy | freqtrade/freqtrade-strategies | Simple Bollinger Band + RSI |
| MACD Strategy | freqtrade/freqtrade-strategies | Classic MACD crossover |
| CombinedBinHAndCluc | freqtrade/freqtrade-strategies | Combined momentum |

All available at: https://github.com/freqtrade/freqtrade-strategies/tree/main/user_data/strategies

---

## Notes

- Each strategy is a standalone Python class — no shared state between strategies
- You can run different strategies in different bot instances using different configs
- Strategy files are version-controlled in this repo; backtest results are not (gitignored)
- Always backtest + dry-run a new strategy before enabling it with real funds
