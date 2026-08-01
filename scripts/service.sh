#!/usr/bin/env bash
# =============================================================================
# ClaudeTrade — Service Manager
# Manages the systemd user service for the Freqtrade bot.
# Usage:
#   ./scripts/service.sh <command>
#
# Commands:
#   start     Start the bot service
#   stop      Stop the bot service
#   restart   Restart the bot service
#   status    Show current service status
#   logs      Tail live logs (Ctrl+C to exit)
#   enable    Enable auto-start on system boot
#   disable   Disable auto-start on system boot
# =============================================================================
set -euo pipefail

SERVICE="claude-trade-bot"

case "${1:-help}" in
    start)
        echo "Starting $SERVICE..."
        systemctl --user start "$SERVICE"
        sleep 2
        systemctl --user status "$SERVICE" --no-pager
        echo ""
        echo "Web UI: http://localhost:8080"
        ;;
    stop)
        echo "Stopping $SERVICE..."
        systemctl --user stop "$SERVICE"
        echo "Done."
        ;;
    restart)
        echo "Restarting $SERVICE..."
        systemctl --user restart "$SERVICE"
        sleep 2
        systemctl --user status "$SERVICE" --no-pager
        ;;
    status)
        systemctl --user status "$SERVICE" --no-pager
        ;;
    logs)
        echo "Tailing logs for $SERVICE (Ctrl+C to stop)..."
        journalctl --user -u "$SERVICE" -f
        ;;
    enable)
        systemctl --user enable "$SERVICE"
        echo "$SERVICE enabled — will auto-start on boot."
        ;;
    disable)
        systemctl --user disable "$SERVICE"
        echo "$SERVICE disabled — will not auto-start on boot."
        ;;
    help|*)
        echo "Usage: $0 {start|stop|restart|status|logs|enable|disable}"
        exit 1
        ;;
esac
