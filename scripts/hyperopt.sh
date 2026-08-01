#!/usr/bin/env bash
# =============================================================================
# ClaudeTrade — Hyperopt Script
# Usage:
#   ./scripts/hyperopt.sh
#   STRATEGY=MyStrategy EPOCHS=200 ./scripts/hyperopt.sh
#   LOSS=CalmarHyperOptLoss ./scripts/hyperopt.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$ROOT/.secrets" ]; then
    set -a; source "$ROOT/.secrets"; set +a
fi

source "$ROOT/venv/bin/activate"

STRATEGY="${STRATEGY:-CTSampleStrategy}"
EPOCHS="${EPOCHS:-100}"
LOSS="${LOSS:-SharpeHyperOptLoss}"
TIMERANGE="${TIMERANGE:-20240101-}"
CONFIG="$ROOT/config/config.demo.json"
SECRETS="$ROOT/config/secrets.json"

echo "Running hyperopt..."
echo "  Strategy: $STRATEGY"
echo "  Epochs:   $EPOCHS"
echo "  Loss:     $LOSS"
echo ""

freqtrade hyperopt \
    --config "$CONFIG" \
    --config "$SECRETS" \
    --strategy "$STRATEGY" \
    --strategy-path "$ROOT/strategies" \
    --userdir "$ROOT/user_data" \
    --hyperopt-loss "$LOSS" \
    --epochs "$EPOCHS" \
    --timerange "$TIMERANGE"
