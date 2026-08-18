#!/bin/sh
ENV_FILE="/app/.env"

# --- Load biến từ .env ---
if [ -f "$ENV_FILE" ]; then
    sed -i 's/\r$//' "$ENV_FILE" || true
    set -a
    . "$ENV_FILE"
    set +a
else
    echo "Lỗi: Không tìm thấy file .env tại $ENV_FILE"
    exit 1
fi

LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

$PYTHON_EXEC -u "$PROJECT_DIR/nft_app/wallets_realtime/monitor_wallet_realtime.py" 2>&1 | tee -a "$LOG_DIR/wss_sol_monitor.log" &

$PYTHON_EXEC -u "$PROJECT_DIR/nft_app/wallets_realtime/monitor_evm_wallets_realtime.py" 2>&1 | tee -a "$LOG_DIR/wss_evm_monitor.log" &

echo "All monitors started. Logs are being output to console and files."