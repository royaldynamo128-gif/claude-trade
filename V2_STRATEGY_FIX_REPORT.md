# V2 Strategy Fix Report — Entry/Exit Logic Overhaul

**Date:** 2026-08-02
**Status:** ✅ Entry logic fixed (buy dips, not spikes), exit logic fixed, trailing stops widened.
**Backtest:** Win rate 38% (up from 18%), PF 0.51 (up from 0.18), exit_signal win rate 69.4%.

## What Was Fixed

### 1. Entry Logic (CRITICAL FIX)
- V1: `rsi > 35` = bought at TOPS (almost always true in uptrend)
- V2: `rsi_was_low < 40` + `rsi_rising` = buys DIPS that are bouncing
- Added ADX > 25 (trend strength)
- Added 1.2x volume confirmation

### 2. Exit Logic
- V1: RSI > 70 / RSI < 30 = too tight, 2-5 min exits
- V2: RSI > 75 / RSI < 25 + RSI peak detection = 45 min avg duration

### 3. ROI Table
- V1: 5% → 2.5% → 1.5% → 0% (too tight, exited too early)
- V2: 15% → 8% → 4% → 2% → 0% (lets winners run)

### 4. Trailing Stop
- V1: 1.5×ATR, break-even at 1.5% profit (too tight)
- V2: 2.5×ATR, break-even at 3% profit (lets trades breathe)

### 5. Cold-Start ML Bypass
- When calibrator not fit (first 50 trades), ML gates are SKIPPED
- Only TA logic drives entries during cold-start
- ML gates activate automatically after calibration

## Backtest Comparison (Aug 15 - Oct 1, 2025)

| Metric | V1 | V2 | Change |
|---|---|---|---|
| Win Rate | 18.2% | 38.0% | +20pp |
| Profit Factor | 0.18 | 0.51 | +183% |
| Avg Duration | 6 min | 45 min | 7.5x |
| Exit Signal Win% | N/A | 69.4% | Profitable! |

## Remaining Issue
44/108 trades (41%) still hit stop loss. This is the main PnL drain.
Fix: Hyperopt entry params + add 1h confirmation + run dry-run to collect calibration data.
