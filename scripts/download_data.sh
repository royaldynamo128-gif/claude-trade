#!/usr/bin/env bash
# =============================================================================
# ClaudeTrade — Download Historical Data Script
# Downloads OHLCV candle data from Binance for backtesting / hyperopt.
# Usage:
#   ./scripts/download_data.sh
#   TIMERANGE=20230101- TIMEFRAME=1h ./scripts/download_data.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$ROOT/.secrets" ]; then
    set -a; source "$ROOT/.secrets"; set +a
fi

source "$ROOT/venv/bin/activate"

TIMERANGE="${TIMERANGE:-20240101-}"
TIMEFRAME="${TIMEFRAME:-5m}"
CONFIG="$ROOT/config/config.demo.json"

echo "Downloading historical data from Binance..."
echo "  Timerange: $TIMERANGE"
echo "  Timeframe: $TIMEFRAME"
echo ""

freqtrade download-data \
    --config "$CONFIG" \
    --userdir "$ROOT/user_data" \
    --timerange "$TIMERANGE" \
    --timeframes "$TIMEFRAME" \
    --exchange binance
