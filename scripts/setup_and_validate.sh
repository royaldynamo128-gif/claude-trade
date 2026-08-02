#!/usr/bin/env bash
# =============================================================================
# ClaudeTrade — Setup & Validation Script
# One-command setup: creates venv, installs deps, downloads data, runs backtest
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

echo "======================================"
echo "  ClaudeTrade Setup & Validation"
echo "======================================"

# 1. Create venv
if [ ! -d "$ROOT/venv" ]; then
    echo "[1/6] Creating Python 3.12 venv with uv..."
    uv venv venv --python 3.12
fi
source "$ROOT/venv/bin/activate"

# 2. Install dependencies
echo "[2/6] Installing dependencies..."
uv pip install --upgrade pip wheel setuptools
uv pip install "freqtrade>=2024.12" lightgbm xgboost scikit-learn datasieve

# 3. Create user_data directory
echo "[3/6] Initializing user_data..."
freqtrade create-userdir --userdir "$ROOT/user_data" 2>/dev/null || true
mkdir -p "$ROOT/user_data/logs" "$ROOT/user_data/data/binance"

# 4. Download historical data (5m + 1h, 13 months)
echo "[4/6] Downloading historical data (BTC + 9 alt pairs, 13 months)..."
freqtrade download-data \
    --config "$ROOT/config/config.backtest.json" \
    --userdir "$ROOT/user_data" \
    --timerange 20250701-20260801 \
    --timeframes 5m 1h \
    --exchange binance \
    --trading-mode futures

# 5. Run smoke backtest (1 month)
echo "[5/6] Running smoke backtest (Aug 2025)..."
freqtrade backtesting \
    --strategy CTFuturesTrendStrategy \
    --strategy-path "$ROOT/strategies" \
    --config "$ROOT/config/config.backtest.json" \
    --userdir "$ROOT/user_data" \
    --timerange 20250801-20250901 \
    --freqaimodel LightGBMRegressor \
    --enable-protections

# 6. Run Phase 5 validator
echo "[6/6] Running Phase 5 L7 KPI validator..."
LATEST_BT=$(ls -t "$ROOT/user_data/backtest_results/"backtest-result-*.zip 2>/dev/null | head -1)
if [ -n "$LATEST_BT" ]; then
    BT_DIR=$(mktemp -d)
    unzip -oq "$LATEST_BT" -d "$BT_DIR"
    BT_JSON=$(find "$BT_DIR" -name "*.json" | grep -v meta | head -1)
    python "$ROOT/validate_phase5.py" --backtest-result "$BT_JSON" --output "$ROOT/phase5_validation_report.json"
    rm -rf "$BT_DIR"
else
    echo "WARNING: No backtest result found. Skipping validation."
fi

echo ""
echo "======================================"
echo "  Setup Complete"
echo "======================================"
echo ""
echo "Next steps:"
echo "  1. Review phase5_validation_report.json"
echo "  2. For longer backtest: ./scripts/backtest.sh"
echo "  3. For dry-run: ./scripts/start.sh"
echo ""
echo "Architecture:"
echo "  - Strategy:        CTFuturesTrendStrategy (strategies/FuturesTrendStrategy.py)"
echo "  - Confidence Eng:  application/confidence_engine.py (multiplicative gating)"
echo "  - Risk Stack:      leverage, custom_stoploss, custom_stake_amount, confirm_trade_entry"
echo "  - AI:              FreqAI LightGBMRegressor (config.demo.json)"
echo "  - Protections:     CooldownPeriod, StoplossGuard, MaxDrawdown (daily+weekly), LowProfitPairs"
echo "  - Validator:       validate_phase5.py (L7 KPI measurement)"
echo ""
