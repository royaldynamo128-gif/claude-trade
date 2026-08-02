#!/usr/bin/env bash
# =============================================================================
# ClaudeTrade — Backtest Script
# Usage:
#   ./scripts/backtest.sh
#   STRATEGY=NostalgiaForInfinity TIMERANGE=20240101-20241231 ./scripts/backtest.sh
#   STRATEGY_LIST="StratA StratB" ./scripts/backtest.sh   # compare strategies
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$ROOT/.secrets" ]; then
    set -a; source "$ROOT/.secrets"; set +a
fi

source "$ROOT/venv/bin/activate"

STRATEGY="${STRATEGY:-CTSampleStrategy}"
TIMERANGE="${TIMERANGE:-20240101-}"
CONFIG="$ROOT/config/config.demo.json"
SECRETS="$ROOT/config/secrets.json"

echo "Running backtest..."
echo "  Strategy:  $STRATEGY"
echo "  Timerange: $TIMERANGE"
echo ""

if [ -n "${STRATEGY_LIST:-}" ]; then
    # Compare multiple strategies
    freqtrade backtesting \
        --config "$CONFIG" \
        --config "$SECRETS" \
        --strategy-list $STRATEGY_LIST \
        --strategy-path "$ROOT/strategies" \
        --userdir "$ROOT/user_data" \
        --timerange "$TIMERANGE"
else
    freqtrade backtesting \
        --config "$CONFIG" \
        --config "$SECRETS" \
        --strategy "$STRATEGY" \
        --strategy-path "$ROOT/strategies" \
        --userdir "$ROOT/user_data" \
        --timerange "$TIMERANGE"
fi
