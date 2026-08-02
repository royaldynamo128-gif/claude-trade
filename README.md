# ClaudeTrade

A production-grade, AI-assisted Freqtrade wrapper for Binance USDT Futures (Long & Short).

**Status:** Phase 5 ready — FreqAI + Calibrated Confidence Engine + Risk Stack + Phase 5 Validator all integrated and runnable. Live trading gated behind Phase 5 validation success (see `PHASE_5_VALIDATION_CHECKLIST.md`).

---

## Architecture

| Layer | Component | File |
|---|---|---|
| Execution | Freqtrade 2026.7 | `venv/` (installed) |
| Strategy | `CTFuturesTrendStrategy` | `strategies/FuturesTrendStrategy.py` |
| Confidence Engine | Multiplicative-gating combiner + Isotonic calibrator + EV + drift + validator | `application/confidence_engine.py` |
| AI | FreqAI LightGBM Regressor | `config/config.demo.json` (`freqai` block) |
| Risk | 5-protection stack + dynamic leverage + ATR stoploss + position sizing | inline in strategy |
| Validation | Phase 5 L7 KPI validator | `validate_phase5.py` |

## Quickstart

```bash
# One-command setup (creates venv, installs deps, downloads data, runs backtest + validator)
./scripts/setup_and_validate.sh

# Or step by step:
uv venv venv --python 3.12
source venv/bin/activate
uv pip install -r requirements.txt
freqtrade create-userdir --userdir user_data

# Download data
./scripts/download_data.sh

# Backtest
./scripts/backtest.sh

# Dry-run (Binance Demo)
./scripts/start.sh
```

## Confidence Engine Design (key fix)

The previous implementation used additive weighted averaging, which collapsed predictions to 45–55%. The corrected implementation uses **multiplicative gating**:

```
composite = calibrated_ml_prob
          × trend_multiplier      [0.7, 1.2]
          × regime_multiplier     [0.5, 1.0]
          × volatility_multiplier [0.75, 1.0]
          × funding_multiplier    [0.8, 1.0]
          × oi_multiplier         [0.8, 1.1]
          × historical_wr_mult    [0.7, 1.2]
```

A single strong disagreement can veto a trade. Additive averaging regresses to the mean; multiplicative preserves discriminative power.

### EV uses LOWER BOUND of confidence interval

```
EV = P(win) × reward_usd − P(loss) × risk_usd − costs_usd
where P(win) = confidence_interval.lower  (NOT point estimate)
```

This makes the system conservative by default — trades only when even the pessimistic estimate is positive.

## Phase 5 Validation

```bash
# After a backtest:
python validate_phase5.py --backtest-result user_data/backtest_results/<latest>.json

# Or from live trade DB:
python validate_phase5.py --db user_data/tradesv3.sqlite
```

Outputs L7 KPIs (Brier, ECE, confidence std-dev, EV correlation, quartile win-rate delta). All must be green for 7 consecutive days before Live consideration.

## Critical Files

- `strategies/FuturesTrendStrategy.py` — Strategy with FreqAI, risk callbacks, confidence engine integration
- `application/confidence_engine.py` — IsotonicCalibrator, MultiSignalConfidenceCombiner, EVComputer, TradeFilter, UnifiedDriftDetector, CalibrationValidator
- `config/config.demo.json` — FreqAI config (30d train, hourly retrain, SVM outliers, noise=0.05, 1000 estimators)
- `config/config.backtest.json` — Static pairlist + relaxed FreqAI params for faster backtests
- `validate_phase5.py` — L7 KPI validator
- `PHASE_4_5_AUDIT_REPORT.md` — Documents the 10 critical defects fixed in this version
- `PHASE_5_VALIDATION_CHECKLIST.md` — Required gates before Live trading

## Configurable Parameters

| Parameter | Default | Location |
|---|---|---|
| `confidence_threshold` | 0.60 | strategy class attr |
| `max_open_trades` | 4 | config |
| `stake_amount` | 20 USDT | config |
| `leverage` (cap) | 5x | config |
| `liquidation_buffer` | 0.10 | config |
| `train_period_days` | 30 | config (`freqai`) |
| `backtest_period_days` | 7 | config (`freqai`) |
| `live_retrain_hours` | 1 | config (`freqai`) |
| `n_estimators` | 1000 | config (`freqai.model_training_parameters`) |
| `learning_rate` | 0.01 | config (`freqai.model_training_parameters`) |

## Risk Stack

| Protection | Config |
|---|---|
| CooldownPeriod | 2 candles |
| StoplossGuard | 24-candle lookback, 4-trade limit, 4-candle stop, `only_per_side: true` |
| MaxDrawdown (daily) | 288-candle lookback, 5% max DD, 144-candle stop, equity mode |
| MaxDrawdown (weekly) | 2016-candle lookback, 10% max DD, 576-candle stop, equity mode |
| LowProfitPairs | 2016-candle lookback, 0% required profit |

## Disclaimer

No backtest is a prediction. Backtests assume fills that dry-run may not get. Live trading carries risk of loss. This code is provided as research and engineering work, not financial advice. Start with Binance Futures Demo (`dry_run: true`). Only risk capital you can afford to lose.
