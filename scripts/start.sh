#!/usr/bin/env bash
# =============================================================================
# ClaudeTrade — Start Script
# Usage:
#   ./scripts/start.sh                            # Demo mode (default)
#   MODE=live ./scripts/start.sh                  # Live mode
#   STRATEGY=MyStrategy ./scripts/start.sh        # Custom strategy
#   MODE=live STRATEGY=MyStrategy ./scripts/start.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$ROOT/venv/bin/activate"

MODE="${MODE:-demo}"
STRATEGY="${STRATEGY:-CTSampleStrategy}"
CONFIG="$ROOT/config/config.${MODE}.json"
SECRETS="$ROOT/config/secrets.${MODE}.json"

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: Config not found: $CONFIG"
    exit 1
fi

if [ ! -f "$SECRETS" ]; then
    echo "ERROR: Secrets not found: $SECRETS"
    echo "  Copy config/secrets.json.example → config/secrets.json and fill in values."
    exit 1
fi

# For demo mode, secrets.demo.json doesn't exist — use secrets.json
if [ "$MODE" = "demo" ] && [ ! -f "$SECRETS" ]; then
    SECRETS="$ROOT/config/secrets.json"
fi
# Resolve: demo uses secrets.json, live uses secrets.live.json
if [ "$MODE" = "demo" ]; then
    SECRETS="$ROOT/config/secrets.json"
else
    SECRETS="$ROOT/config/secrets.live.json"
fi

echo "======================================"
echo "  ClaudeTrade starting"
echo "  Mode:     $MODE"
echo "  Strategy: $STRATEGY"
echo "  Web UI:   http://localhost:8080"
echo "  User:     admin"
echo "======================================"

freqtrade trade \
    --config "$CONFIG" \
    --config "$SECRETS" \
    --strategy "$STRATEGY" \
    --strategy-path "$ROOT/strategies" \
    --userdir "$ROOT/user_data" \
    --logfile "$ROOT/user_data/logs/freqtrade.log"
