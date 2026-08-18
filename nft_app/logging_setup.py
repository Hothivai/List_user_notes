import logging 
import os
from logging.handlers import TimedRotatingFileHandler

def setup_logger(name, log_file, level=logging.INFO):
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.hasHandlers():
        return logger

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    handler = TimedRotatingFileHandler(log_file, when='midnight', interval=1, backupCount=3, encoding='utf-8')
    formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    handler.setFormatter(formatter)

    logger.addHandler(handler)
    logger.propagate = False  # không lan sang root logger
    return logger

# --- Tạo các logger riêng ---
evm_logger = setup_logger("EVM_MONITOR", "logs/monitor_evm_wallets.log")
sol_logger = setup_logger("SOL_MONITOR", "logs/monitor_sol_wallets.log")
system_logger = setup_logger("SYSTEM", "logs/main_system.log")
error_logger = setup_logger("ERROR", "logs/error_system.log", level=logging.ERROR)
api_logger = setup_logger("API", "logs/api.log")
aerodrome_evm_logger = setup_logger("AERODROME_EVM", "logs/monitor_aerodrome_evm_wallets.log")

nft_debug_logger = setup_logger("NFT_DEBUG_LOGIC", "logs/nft_debug.log")

# Flask server logs
flask_logger = setup_logger("werkzeug", "logs/flask_server.log")

tx_history_logger = setup_logger("TX_HISTORY", "logs/tx_history.log", level=logging.DEBUG)