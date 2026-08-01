# ClaudeTrade

A clean, minimal Freqtrade wrapper for Binance Futures trading.

**This project does NOT contain a custom trading engine.**
Freqtrade handles: order execution, position management, stop-loss, take-profit,
trailing stops, leverage, trade lifecycle, database, logging, backtesting, and hyperopt.
This repo provides: configuration, strategies, and management scripts only.

---

## Architecture

```
claude-trade/
├── config/
│   ├── config.demo.json    ← Binance Futures Demo (active)
│   └── config.live.json    ← Binance Futures Live (switch when ready)
├── strategies/
│   └── CTSampleStrategy.py   ← Starting strategy (add more here)
├── scripts/
│   ├── start.sh            ← Start the bot
│   ├── backtest.sh         ← Run backtests
│   ├── hyperopt.sh         ← Optimize strategy parameters
│   └── download_data.sh    ← Download historical candles
├── user_data/              ← Freqtrade runtime data (gitignored)
├── venv/                   ← Python 3.11 venv (gitignored)
└── .secrets                ← API keys (gitignored — never commit)
```

**Web UI**: http://localhost:8080 (FreqUI — runs while bot is running)

---

## Prerequisites

1. **Binance Demo Account**: Ensure your Demo Futures API keys are active at
   [Binance Demo](https://testnet.binancefuture.com)
   - Position Mode: **One-way Mode**
   - Asset Mode: **Single-Asset Mode**

2. **Telegram Bot**: Bot token and chat ID already configured in `.secrets`

---

## First-Time Setup

```bash
# 1. Clone the repo
git clone https://github.com/royaldynamo128-gif/claude-trade.git
cd claude-trade

# 2. Fill in credentials
cp .env.example .secrets
# Edit .secrets with your API keys

# 3. Create Python venv and install Freqtrade
uv venv venv --python 3.11
source venv/bin/activate
uv pip install -r requirements.txt

# 4. Initialize Freqtrade user data directory
freqtrade create-userdir --userdir user_data

# 5. Copy strategy template
cp venv/lib/python3.11/site-packages/freqtrade/templates/sample_strategy.py \
   strategies/CTSampleStrategy.py
```

---

## Running the Bot

### Demo Mode (default)
```bash
./scripts/start.sh
```

### Live Mode
```bash
MODE=live ./scripts/start.sh
```

### With a different strategy
```bash
MODE=demo STRATEGY=NostalgiaForInfinity ./scripts/start.sh
```

**Switching Demo → Live** only requires changing `MODE`. The live config uses
production Binance endpoints (`fapi.binance.com`) and `BINANCE_API_KEY` / `BINANCE_API_SECRET`
from `.secrets`. No other change needed.

---

## Web UI (FreqUI)

While the bot is running, open: **http://localhost:8080**

- **Username**: `admin`
- **Password**: see `FREQTRADE_UI_PASSWORD` in your `.secrets` file

The UI shows: open trades, completed trades, PnL, account balance, logs, open orders,
strategy statistics, and bot controls (start/stop).

---

## Backtesting

```bash
# Download historical data first
./scripts/download_data.sh

# Run backtest with default strategy
./scripts/backtest.sh

# Run with specific strategy and date range
STRATEGY=NostalgiaForInfinity TIMERANGE=20240101-20241231 ./scripts/backtest.sh

# Compare two strategies
STRATEGY_LIST="CTSampleStrategy NostalgiaForInfinity" ./scripts/backtest.sh
```

---

## Hyperopt (Strategy Optimization)

```bash
STRATEGY=CTSampleStrategy EPOCHS=200 ./scripts/hyperopt.sh
```

Results are saved to `user_data/hyperopt_results/`. Load the best parameters with:
```bash
freqtrade hyperopt-show --userdir user_data --best
```

---

## Adding Community Strategies

See [STRATEGIES.md](STRATEGIES.md) for the full guide.

Quick version:
1. Copy the strategy `.py` file to `strategies/`
2. Test it: `source venv/bin/activate && freqtrade test-strategy --strategy ClassName --userdir user_data --config config/config.demo.json`
3. Run it: `STRATEGY=ClassName ./scripts/start.sh`

---

## Common Commands

| Task | Command |
|---|---|
| Start bot (demo) | `./scripts/start.sh` |
| Start bot (live) | `MODE=live ./scripts/start.sh` |
| Download data | `./scripts/download_data.sh` |
| Run backtest | `./scripts/backtest.sh` |
| Optimize strategy | `./scripts/hyperopt.sh` |
| Test a strategy | `source venv/bin/activate && freqtrade test-strategy --strategy CTSampleStrategy --userdir user_data --config config/config.demo.json` |
| View live trades | Open http://localhost:8080 |
| Show bot version | `source venv/bin/activate && freqtrade --version` |

---

## AI Extension Point

If you want to add custom AI signal generation in the future:
- Implement it as a Freqtrade strategy class in `strategies/`
- The AI model should **only** generate `populate_entry_trend()` / `populate_exit_trend()` signals
- Freqtrade handles execution, risk management, and order placement
- Do NOT bypass Freqtrade's execution engine

---

## Configuration Reference

| Setting | Demo Value | Notes |
|---|---|---|
| `stake_amount` | 20 USDT | Per-trade allocation |
| `max_open_trades` | 3 | Max simultaneous positions |
| `leverage` | 3× | Change in config JSON |
| `timeframe` | 5m | Candle timeframe |
| `trading_mode` | futures | Binance Futures (not Spot) |
| `margin_mode` | isolated | Isolated margin per position |

To change leverage: edit `"leverage": 3` in `config/config.demo.json`.
To change stake: edit `"stake_amount": 20`.
