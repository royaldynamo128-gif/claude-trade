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
STRATEGY="${STRATEGY:-CTFuturesTrendStrategy}"
CONFIG="$ROOT/config/config.${MODE}.json"
if [ "$MODE" = "demo" ]; then
    if [ -f "$ROOT/config/secrets.json" ]; then
        SECRETS="$ROOT/config/secrets.json"
    else
        SECRETS="$ROOT/config/secrets.demo.json"
    fi
else
    SECRETS="$ROOT/config/secrets.live.json"
fi

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: Config not found: $CONFIG"
    exit 1
fi

if [ ! -f "$SECRETS" ]; then
    echo "ERROR: Secrets not found: $SECRETS"
    echo "  Copy config/secrets.json.example → config/secrets.json and fill in values."
    exit 1
fi

UI_PORT="8088"
[ "$MODE" = "live" ] && UI_PORT="8080"

UI_USER="freqtrade"
if [ -f "$SECRETS" ]; then
    SERVED_USER=$(python3 -c "import json; data=json.load(open('$SECRETS')); print(data.get('api_server', {}).get('username', ''))" 2>/dev/null || true)
    [ -n "$SERVED_USER" ] && UI_USER="$SERVED_USER"
fi

echo "======================================"
echo "  ClaudeTrade starting"
echo "  Mode:     $MODE"
echo "  Strategy: $STRATEGY"
echo "  Web UI:   http://localhost:${UI_PORT}"
echo "  User:     ${UI_USER}"
echo "======================================"

freqtrade trade \
    --config "$CONFIG" \
    --config "$SECRETS" \
    --strategy "$STRATEGY" \
    --strategy-path "$ROOT/strategies" \
    --userdir "$ROOT/user_data" \
    --logfile "$ROOT/user_data/logs/freqtrade.log"
