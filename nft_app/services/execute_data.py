import mysql.connector
import gc
from config import ID_CHAIN_MAP
from datetime import timedelta
from services.helper import to_datetime_safe
import requests
from services.db_connect import get_db_config, get_connection

DB_CONFIG = get_db_config()

# Create database and table if not exist
def create_database_and_table():
    temp_config = DB_CONFIG.copy()
    temp_config.pop("database")

    try:
        connection = get_connection()
        cursor = connection.cursor()

        cursor.execute("SHOW DATABASES")
        databases = [db[0] for db in cursor.fetchall()]

        if DB_CONFIG['database'] in databases:
            print(f"Database {DB_CONFIG['database']} already exists.")
        else:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DB_CONFIG['database']}")
            print(f"Database {DB_CONFIG['database']} newly created")
        
        # Clear database list after use
        del databases  

        connection.database = DB_CONFIG['database']

        cursor.execute("SHOW TABLES")
        tables = [table[0] for table in cursor.fetchall()]

        if "wallet_nft_position" in tables:
            print("Table wallet_nft_position already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS wallet_nft_position(
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    wallet_address VARCHAR(60),
                    chain VARCHAR(10),
                    nft_id VARCHAR(60),
                    token0_symbol VARCHAR(20),
                    token1_symbol VARCHAR(20),
                    pool_address VARCHAR(100),
                    price_token0 DOUBLE,
                    price_token1 DOUBLE,
                    status VARCHAR(20),
                    date_add_liquidity DATETIME,
                    initial_token0_amount DOUBLE,
                    initial_token1_amount DOUBLE,
                    initial_total_value DOUBLE,
                    current_token0_amount DOUBLE,
                    current_token1_amount DOUBLE,
                    current_total_value DOUBLE,
                    delta_amount DOUBLE,
                    percent_change DOUBLE,
                    unclaimed_fee_token0 DOUBLE,
                    unclaimed_fee_token1 DOUBLE,
                    total_unclaimed_fee DOUBLE,
                    lp_fee_apr DOUBLE,
                    lp_fee_apr_1h DOUBLE,
                    pending_cake DOUBLE,
                    reward_price DOUBLE,
                    cake_reward_1h DOUBLE,
                    boost_multiplier DOUBLE,
                    farm_apr_1h DOUBLE,
                    farm_apr_all DOUBLE,
                    is_active TINYINT(1) DEFAULT 1,
                    wallet_url VARCHAR(255),
                    nft_id_url VARCHAR(255),
                    created_at DATETIME,
                    updated_at DATETIME,
                    has_invalid_price TINYINT(1),
                    lower_price FLOAT DEFAULT 0.0,
                    upper_price FLOAT DEFAULT 0.0,
                    current_price FLOAT DEFAULT 0.0,
                    type_dex VARCHAR(20),
                    pnl_value_base DECIMAL(36,18),
                    pnl_value_usd DECIMAL(36,18),
                    total_active_staked_usd DECIMAL(36,18),
                    total_pool_liquidity_usd DECIMAL(36,18),
                    npm_address VARCHAR(42) NOT NULL DEFAULT '',
                    INDEX idx_wallet_nft_identity (wallet_address, chain, type_dex, nft_id, npm_address, created_at)
                ) ENGINE=InnoDB;
            """
            cursor.execute(create_table_query)
            print("Table wallet_nft_position newly created")

        if "wallet_nft_summary" in tables:
            print("Table wallet_nft_summary already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS wallet_nft_summary (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    nft_id VARCHAR(60) NOT NULL,
                    wallet_address VARCHAR(60) NOT NULL,
                    chain VARCHAR(10) NOT NULL,
                    type_dex VARCHAR(20) NOT NULL DEFAULT '',
                    npm_address VARCHAR(42) NOT NULL DEFAULT '',
                    token0_symbol VARCHAR(20),
                    token1_symbol VARCHAR(20),
                    base_symbol VARCHAR(20),
                    net_invested_capital DECIMAL(36,18) DEFAULT 0,
                    total_cash_injected DECIMAL(36,18) DEFAULT 0,
                    invested_capital_base DECIMAL(36,18) DEFAULT 0,
                    total_claimed_fee0 DECIMAL(36,18) DEFAULT 0,
                    total_claimed_fee1 DECIMAL(36,18) DEFAULT 0,
                    total_claimed_reward DECIMAL(36,18) DEFAULT 0,
                    total_claimed_fee_usd DECIMAL(36,18) DEFAULT 0,
                    total_claimed_reward_usd DECIMAL(36,18) DEFAULT 0,
                    total_claimed_in_base DECIMAL(36,18) DEFAULT 0,
                    last_lp_token0 DECIMAL(36,18) DEFAULT 0,
                    last_lp_token1 DECIMAL(36,18) DEFAULT 0,
                    last_unclaimed_fee0 DECIMAL(36,18) DEFAULT 0,
                    last_unclaimed_fee1 DECIMAL(36,18) DEFAULT 0,
                    last_pending_reward DECIMAL(36,18) DEFAULT 0,
                    current_val_base DECIMAL(36,18) DEFAULT 0,
                    pnl_base_percent DECIMAL(18,6) DEFAULT 0,
                    pnl_value_usd DECIMAL(36,18) DEFAULT 0,
                    pnl_value_base DECIMAL(36,18) DEFAULT 0,
                    status VARCHAR(20),
                    updated_at DATETIME,
                    UNIQUE KEY uniq_wallet_nft_summary_identity (wallet_address, chain, type_dex, nft_id, npm_address)
                ) ENGINE=InnoDB;
            """
            cursor.execute(create_table_query)
            print("Table wallet_nft_summary newly created")
        
        if "list_bond_contract_notify" in tables:
            print("Table list_bond_contract_notify already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS list_bond_contract_notify (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    chain VARCHAR(10) NOT NULL,
                    contract_address VARCHAR(100) NOT NULL,
                    token_symbol VARCHAR(20) NOT NULL,
                    status ENUM('active', 'sold') NOT NULL DEFAULT 'active',
                    notify_threshold DECIMAL(5,2) DEFAULT 10.00,
                    updated_at DATETIME,
                    UNIQUE(contract_address)
                );
            """
            cursor.execute(create_table_query)
            print("Table list_bond_contract_notify newly created")
        
        if "nft_blacklist" in tables:
            print("Table nft_blacklist already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS nft_blacklist (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    wallet_address VARCHAR(60) NOT NULL,
                    chain VARCHAR(10) NOT NULL,
                    nft_id VARCHAR(60) NOT NULL,
                    type_dex VARCHAR(20) NOT NULL DEFAULT '',
                    npm_address VARCHAR(42) NOT NULL DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY unique_blacklist (wallet_address, chain, type_dex, nft_id, npm_address)
                );
            """
            cursor.execute(create_table_query)
            print("Table nft_blacklist newly created")

        if "pool_info" in tables:
            print("Table pool_info already exists.")
        else:
            create_table_query ="""
                CREATE TABLE IF NOT EXISTS pool_info (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    chain VARCHAR(10) NOT NULL,
                    pool_address VARCHAR(42) NOT NULL UNIQUE,
                    token0_address VARCHAR(42),
                    token1_address VARCHAR(42),
                    token0_symbol VARCHAR(20),
                    token1_symbol VARCHAR(20),
                    token0_decimals INT,
                    token1_decimals INT,
                    fee INT,
                    alloc_point INT,
                    pid INT,
                    cake_per_day DOUBLE,
                    total_value_lock DOUBLE DEFAULT 0.0,
                    cake_re ard_1h FLOAT DEFAULT 0.0,
                    total_current_liquidity DOUBLE DEFAULT 0.0,
                    total_staked_liquidity DOUBLE DEFAULT 0.0,
                    total_inactive_staked_liquidity DOUBLE DEFAULT 0.0, 
                    farm_apr DOUBLE DEFAULT 0.0,
                    is_stake_tracked BOOLEAN DEFAULT FALSE,
                    update_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            """
            cursor.execute(create_table_query)
            print("Table pool_info newly created")

        if "pool_sol_info" in tables:
            print("Table pool_info already exists.")
        else:
            create_table_query ="""
                CREATE TABLE IF NOT EXISTS pool_sol_info (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    chain VARCHAR(10) NOT NULL,
                    pool_account VARCHAR(100) NOT NULL UNIQUE,
                    token0_mint VARCHAR(100),
                    token1_mint VARCHAR(100),
                    token0_symbol VARCHAR(50),
                    token1_symbol VARCHAR(50),
                    token0_decimals INT,
                    token1_decimals INT,
                    reward_state INT NOT NULL,
                    open_time BIGINT NOT NULL,
                    end_time BIGINT NOT NULL,
                    fee INT DEFAULT 0,
                    reward_claimed BIGINT NOT NULL,
                    reward_total_emissioned BIGINT NOT NULL,
                    reward_account VARCHAR(60) NOT NULL,
                    reward_symbol VARCHAR(20) NOT NULL,
                    weekly_rewards DOUBLE NOT NULL DEFAULT 0.0,
                    cake_reward_1h FLOAT DEFAULT 0.0,
                    total_valid_liquidity DOUBLE DEFAULT 0.0,
                    total_inactive_staked_liquidity DOUBLE DEFAULT 0.0,
                    total_current_liquidity DOUBLE DEFAULT 0.0,
                    update_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            """
            cursor.execute(create_table_query)
            print("Table pool_sol_info newly created")
        
        if "token_cmc_map" in tables:
            print("Table token_cmc_map already exists.")
        else:
            create_table_query ="""
                CREATE TABLE IF NOT EXISTS token_cmc_map (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    token_address VARCHAR(100) UNIQUE,
                    chain VARCHAR(20),
                    cmc_id VARCHAR(20),
                    symbol VARCHAR(50),
                    name VARCHAR(100),
                    last_updated DATETIME
                )
            """
            cursor.execute(create_table_query)
            print("Table token_cmc_map newly created")
        
        if "hash_txs" in tables:
            print("Table hash_txs already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS hash_txs (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    hash VARCHAR(100) UNIQUE NOT NULL,
                    block VARCHAR(10) NOT NULL,
                    chain VARCHAR(3) NOT NULL,
                    tx_time TIMESTAMP NOT NULL
                );
            """
            cursor.execute(create_table_query)
            print("Table hash_txs newly created")
        
        if "transactions" in tables:
            print("Table transactions already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS transactions (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    hash VARCHAR(100) NOT NULL,
                    wallet VARCHAR(44) NOT NULL,
                    FOREIGN KEY (hash) REFERENCES hash_txs(hash) ON DELETE CASCADE ON UPDATE CASCADE,
                    UNIQUE KEY unique_wallet_hash (wallet, hash)
                );
            """
            cursor.execute(create_table_query)
            print("Table transactions newly created")
            
        if "detail_token_transactions" in tables:
            print("Table detail_token_transactions already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS detail_token_transactions (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    hash VARCHAR(100) NOT NULL,
                    from_address VARCHAR(44) NOT NULL,
                    to_address VARCHAR(44) NOT NULL,
                    contract VARCHAR(44) NOT NULL,
                    amount VARCHAR(50) NOT NULL,                    
                    symbol VARCHAR(50) NOT NULL,
                    wallet VARCHAR(44) NOT NULL,
                    FOREIGN KEY (hash) REFERENCES hash_txs(hash) ON DELETE CASCADE ON UPDATE CASCADE
                );
            """
            cursor.execute(create_table_query)
            print("Table detail_token_transactions newly created")
        
        if "nft_token_transactions" in tables:
            print("Table nft_token_transactions already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS nft_token_transactions (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    hash VARCHAR(100) NOT NULL UNIQUE,
                    contract VARCHAR(44) NOT NULL,
                    token_id VARCHAR(100) NOT NULL,
                    wallet VARCHAR(44) NOT NULL,
                    FOREIGN KEY (hash) REFERENCES hash_txs(hash) ON DELETE CASCADE ON UPDATE CASCADE
                );
            """
            cursor.execute(create_table_query)
            print("Table nft_token_transactions newly created")
        
        if "extreme_price_range_pool_sol" in tables:
            print("Table extreme_price_range_pool_sol already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS extreme_price_range_pool_sol (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    pool_id VARCHAR(100) NOT NULL,
                    tick_lower INT,
                    tick_upper INT,
                    min_price FLOAT DEFAULT 0.0,
                    max_price FLOAT DEFAULT 0.0,
                    tick_array_bitmap_extension_account VARCHAR(100) NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY unique_pool (pool_id)
                );
            """ 
            cursor.execute(create_table_query)
            print("Table extreme_price_range_pool_sol newly created")
        
        if "nft_closed_cache" in tables:
            print("Table nft_closed_cache already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS nft_closed_cache (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    wallet_address VARCHAR(64) NOT NULL,
                    chain_name VARCHAR(10) NOT NULL,
                    nft_id VARCHAR(60) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    type_dex VARCHAR(20) NOT NULL DEFAULT '',
                    npm_address VARCHAR(42) NOT NULL DEFAULT '',
                    last_checked_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY unique_nft (chain_name, wallet_address, type_dex, nft_id, npm_address)
                );
            """ 
            cursor.execute(create_table_query)
            print("Table nft_closed_cache newly created")
            
        if "aerodrome_pool_info" in tables:
            print("Table aerodrome_pool_info already exists.")
        else:
            create_table_query ="""
                CREATE TABLE IF NOT EXISTS aerodrome_pool_info (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    pool_address VARCHAR(42) NOT NULL UNIQUE,
                    chain VARCHAR(10) NOT NULL,
                    factory_address VARCHAR(42) NOT NULL,
                    factory_index INT NOT NULL,
                    token0_address VARCHAR(42),
                    token1_address VARCHAR(42),
                    token0_symbol VARCHAR(50),
                    token1_symbol VARCHAR(50),
                    token0_decimals INT,
                    token1_decimals INT,
                    fee INT,
                    tick_spacing INT,
                    aero_reward_1h FLOAT DEFAULT 0.0,
                    total_value_lock DOUBLE DEFAULT 0.0,
                    cake_reward_1h FLOAT DEFAULT 0.0,
                    total_current_liquidity DOUBLE DEFAULT 0.0,
                    total_staked_liquidity DOUBLE DEFAULT 0.0,
                    total_inactive_staked_liquidity DOUBLE DEFAULT 0.0, 
                    farm_apr DOUBLE DEFAULT 0.0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME
                )
            """
            cursor.execute(create_table_query)
            print("Table aerodrome_pool_info newly created")
        
        if "aerodrome_pool_epoch_state" in tables:
            print("Table aerodrome_pool_epoch_state already exists.")
        else:
            create_table_query = """
            CREATE TABLE IF NOT EXISTS aerodrome_pool_epoch_state (
                id INT AUTO_INCREMENT PRIMARY KEY,
                chain VARCHAR(10) NOT NULL,
                pool_address VARCHAR(42) NOT NULL UNIQUE,
                gauge_address VARCHAR(42) NOT NULL,
                epoch_start INT NOT NULL,
                epoch_finish INT NOT NULL,
                reward_address VARCHAR(42) NOT NULL,
                reward_symbol VARCHAR(10) NOT NULL,
                reward_decimals INT NOT NULL,
                reward_rate FLOAT NOT NULL,
                reward_per_day FLOAT NOT NULL,
                period_finish INT NOT NULL,
                vote_weight FLOAT NOT NULL,
                total_vote_weight FLOAT NOT NULL,
                vote_share FLOAT NOT NULL,
                farm_active BOOLEAN NOT NULL,
                is_stake_tracked BOOLEAN DEFAULT FALSE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                update_at DATETIME DEFAULT CURRENT_TIMESTAMP   
            )
            """
            cursor.execute(create_table_query)
            print("Table aerodrome_pool_epoch_state newly created")
        
        if "wallets" in tables:
            print("Table wallets already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS wallets(
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    wallet_address VARCHAR(128) NOT NULL UNIQUE,
                    chain VARCHAR(32) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
            """
            cursor.execute(create_table_query)
            print("Table wallets newly created")
        
        if "futures_positions_binance" in tables:
            print("Table futures_positions_binance already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS futures_positions_binance (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    wallet_id VARCHAR(128) NOT NULL,
                    symbol VARCHAR(16) NOT NULL,
                    position_amt DECIMAL(30,10) NOT NULL,
                    entry_price DECIMAL(30,10),
                    mark_price DECIMAL(30,10),
                    unrealized_pnl DECIMAL(30,10),
                    leverage INT,
                    margin_type ENUM('cross','isolated'),
                    isolated_margin DECIMAL(30,10),
                    position_side ENUM('BOTH','LONG','SHORT'),
                    notional DECIMAL(30,10),
                    update_time BIGINT,
                    futures_type VARCHAR(16),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (wallet_id) REFERENCES wallets(wallet_address) ON DELETE CASCADE
                );
            """
            cursor.execute(create_table_query)
            print("Table futures_positions_binance newly created")
        
        if "futures_orders_binance" in tables:
            print("Table futures_orders_binance already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS futures_orders_binance (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    wallet_id VARCHAR(128) NOT NULL,
                    order_id BIGINT NOT NULL,
                    symbol VARCHAR(16) NOT NULL,
                    status VARCHAR(32),
                    client_order_id VARCHAR(64),
                    price DECIMAL(30,10),
                    avg_price DECIMAL(30,10),
                    orig_qty DECIMAL(30,10),
                    executed_qty DECIMAL(30,10),
                    cum_quote DECIMAL(30,10),
                    type VARCHAR(32),
                    side ENUM('BUY','SELL'),
                    position_side ENUM('BOTH','LONG','SHORT'),
                    time BIGINT,
                    update_time BIGINT,
                    futures_type VARCHAR(16),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (wallet_id) REFERENCES wallets(wallet_address) ON DELETE CASCADE
                );
            """
            cursor.execute(create_table_query)
            print("Table futures_orders_binance newly created")
        
        if "transaction_history_v2" in tables:
            print("Table transaction_history_v2 already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS transaction_history_v2 (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    hash VARCHAR(100) NOT NULL,
                    block VARCHAR(10) NOT NULL,
                    chain VARCHAR(10) NOT NULL,
                    wallet VARCHAR(60) NOT NULL,
                    date_time TIMESTAMP NOT NULL,
                    contract VARCHAR(100),
                    token_id VARCHAR(100),
                    transaction_fee DECIMAL(30,10) DEFAULT 0,
                    gas_fee DECIMAL(30,10) DEFAULT 0,
                    UNIQUE KEY unique_wallet_hash (wallet, hash),
                    INDEX (hash)
                );
            """
            cursor.execute(create_table_query)
            print("Table transaction_history_v2 newly created")

        if "transaction_detail_v2" in tables:
            print("Table transaction_detail_v2 already exists.")
        else:
            create_table_query = """
                CREATE TABLE IF NOT EXISTS transaction_detail_v2 (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    hash VARCHAR(100) NOT NULL,
                    from_address VARCHAR(60) NOT NULL,
                    to_address VARCHAR(60) NOT NULL,
                    contract VARCHAR(100) NOT NULL,
                    amount VARCHAR(50) NOT NULL,
                    symbol VARCHAR(20) NOT NULL,
                    wallet VARCHAR(60) NOT NULL,
                    is_context TINYINT(1) DEFAULT 0,
                    FOREIGN KEY (hash) REFERENCES transaction_history_v2(hash) ON DELETE CASCADE ON UPDATE CASCADE
                );
            """
            cursor.execute(create_table_query)
            print("Table transaction_detail_v2 newly created")
            
        # Clear table list after use
        del tables 

        connection.commit()

    except mysql.connector.Error as e:
        error_message_sql = f"Error creating database/table: {e}"
        print(error_message_sql)
    
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'connection' in locals():
            connection.close()
    
    gc.collect()

NFT_POSITION_COLUMNS = [
    "wallet_address", "chain", "nft_id", "token0_symbol", "token1_symbol", "pool_address",
    "price_token0", "price_token1", "status", "date_add_liquidity",
    "initial_token0_amount", "initial_token1_amount", "initial_total_value",
    "current_token0_amount", "current_token1_amount", "current_total_value",
    "delta_amount", "percent_change",
    "unclaimed_fee_token0", "unclaimed_fee_token1", "total_unclaimed_fee",
    "lp_fee_apr", "lp_fee_apr_1h", "pending_cake", "reward_price", "cake_reward_1h", "boost_multiplier",
    "farm_apr_1h", "farm_apr_all", "is_active", "wallet_url", "nft_id_url", "created_at", "has_invalid_price",
    "lower_price", "upper_price", "current_price", "type_dex",
    "total_active_staked_usd", "total_pool_liquidity_usd", "npm_address",
]

def _normalize_nft_insert_row(data):
    if isinstance(data, dict):
        normalized = dict(data)
        normalized.setdefault("npm_address", "")
        return tuple(normalized.get(column) for column in NFT_POSITION_COLUMNS)

    if isinstance(data, list):
        data = tuple(data)

    if isinstance(data, tuple):
        if len(data) == 40:
            return data + ("",)
        if len(data) == 41:
            return data
        raise ValueError(f"NFT position row must have 40 or 41 fields, got {len(data)}")

    raise TypeError(f"Unsupported NFT position row type: {type(data)}")

def _append_identity_filters(base_where, params, wallet_address=None, chain=None, type_dex=None, npm_address=None):
    where_parts = [base_where]
    if wallet_address is not None:
        where_parts.append("wallet_address = %s")
        params.append(wallet_address)
    if chain is not None:
        where_parts.append("chain = %s")
        params.append(chain)
    if type_dex is not None:
        where_parts.append("type_dex = %s")
        params.append(type_dex)
    if npm_address is not None and (wallet_address is not None or chain is not None or type_dex is not None):
        where_parts.append("npm_address = %s")
        params.append(npm_address or "")
    return " AND ".join(where_parts), params

# Insert NFT data into MySQL
def insert_nft_data(data_list):
    conn = get_connection()
    cursor = conn.cursor()

    sql = """
        INSERT INTO wallet_nft_position (
            wallet_address, chain, nft_id, token0_symbol, token1_symbol, pool_address, 
            price_token0, price_token1, status, date_add_liquidity,
            initial_token0_amount, initial_token1_amount, initial_total_value,
            current_token0_amount, current_token1_amount, current_total_value,
            delta_amount, percent_change,
            unclaimed_fee_token0, unclaimed_fee_token1, total_unclaimed_fee,
            lp_fee_apr, lp_fee_apr_1h, pending_cake, reward_price, cake_reward_1h, boost_multiplier, 
            farm_apr_1h, farm_apr_all, is_active, wallet_url, nft_id_url, created_at, has_invalid_price,
            lower_price, upper_price, current_price, type_dex,
            total_active_staked_usd, total_pool_liquidity_usd, npm_address
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    inserted_ids = []
    for data in data_list:
        cursor.execute(sql, _normalize_nft_insert_row(data))
        inserted_ids.append(cursor.lastrowid)
        
    conn.commit()
    cursor.close()
    conn.close()

    # Gọi gc.collect() để giải phóng tài nguyên
    gc.collect()

    print(f"Data inserted successfully. IDs: {inserted_ids}")
    return inserted_ids
    
# Function get all NFT data
def fetch_nft_data(limit=100, offset=0):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)  # dictionary=True giúp trả về dạng dict thay vì tuple

        query = """
            SELECT * FROM wallet_nft_position
            WHERE status != 'Closed'
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """
        cursor.execute(query, (limit, offset))

        results = cursor.fetchall()
        return results

    except mysql.connector.Error as e:
        print(f"Error fetching all NFT data: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()
       
def fetch_nft_data_by_wallet_address(wallet_address, start_date=None, end_date=None, limit=100, offset=0):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True) 

        # Convert start_date and end_date to datetime if they're strings
        start_date = to_datetime_safe(start_date)
        end_date = to_datetime_safe(end_date)
        if end_date:
            # Add 1 day to make it inclusive of the end date
            end_date += timedelta(days=1)

        # Build date condition
        date_condition = ""
        if start_date and end_date:
            date_condition = "AND created_at >= %s AND created_at < %s"
        elif start_date:
            date_condition = "AND created_at >= %s"
        elif end_date:
            date_condition = "AND created_at < %s"

        query = f"""
            SELECT *
            FROM wallet_nft_position
            WHERE LOWER(wallet_address) = LOWER(%s)
            AND status != 'Closed'
            {date_condition}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """

        # Prepare parameters
        params = [wallet_address]
        if start_date and end_date:
            params.extend([start_date, end_date])
        elif start_date:
            params.append(start_date)
        elif end_date:
            params.append(end_date)

        params.extend([limit, offset])

        print("Final SQL:", query)
        print("With parameters:", params)

        cursor.execute(query, tuple(params))
        results = cursor.fetchall()
        if not results:
            print("No data found in the given date range.")
            return []
        return results

    except mysql.connector.Error as e:
        print(f"Error fetching NFT data by wallet: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def fetch_nft_data_by_wallet_and_chain(chain, wallet_address, start_date=None, end_date=None, limit=100, offset=0):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True) 
        
        # Convert start_date and end_date to datetime if they're strings
        start_date = to_datetime_safe(start_date)
        end_date = to_datetime_safe(end_date)
        if end_date:
            # Add 1 day to make it inclusive of the end date
            end_date += timedelta(days=1)

        # Build date condition
        date_condition = ""
        if start_date and end_date:
            date_condition = "AND created_at >= %s AND created_at < %s"
        elif start_date:
            date_condition = "AND created_at >= %s"
        elif end_date:
            date_condition = "AND created_at < %s"
        
        query = f"""
            SELECT * 
            FROM wallet_nft_position 
            WHERE chain = %s AND wallet_address = %s AND status != 'Closed'
            {date_condition}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """
        
        # Prepare parameters
        params = [chain, wallet_address]
        if start_date and end_date:
            params.extend([start_date, end_date])
        elif start_date:
            params.append(start_date)
        elif end_date:
            params.append(end_date)

        params.extend([limit, offset])

        print("Final SQL:", query)
        print("With parameters:", params)
        
        cursor.execute(query, tuple(params))
        results = cursor.fetchall()
        if not results:
            print("No data found in the given date range.")
            return []
        return results
    
    except mysql.connector.Error as e:
        print(f"Error fetching NFT data by wallet with chain: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_last_pending_cake_info(nft_id, wallet_address=None, chain=None, type_dex=None, npm_address=""):
    try:
        conn = get_connection()
        cursor = conn.cursor()

        where_sql, params = _append_identity_filters(
            "nft_id = %s",
            [str(nft_id)],
            wallet_address=wallet_address,
            chain=chain,
            type_dex=type_dex,
            npm_address=npm_address,
        )
        query = f"""
            SELECT pending_cake, created_at
            FROM wallet_nft_position
            WHERE {where_sql}
            ORDER BY created_at DESC
            LIMIT 1
        """
        cursor.execute(query, tuple(params))
        result = cursor.fetchone()
        if result:
            pending_cake, created_at = result
            return {
                "pending_cake": pending_cake,
                "created_at": created_at
            }
        else:
            return None

    except mysql.connector.Error as e:
        print(f"Error fetching last pending cake info: {e}")
        return None

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()
            
def get_last_unclaimed_fee_token(nft_id, wallet_address=None, chain=None, type_dex=None, npm_address=""):
    try:
        conn = get_connection()
        cursor = conn.cursor()

        where_sql, params = _append_identity_filters(
            "nft_id = %s",
            [str(nft_id)],
            wallet_address=wallet_address,
            chain=chain,
            type_dex=type_dex,
            npm_address=npm_address,
        )
        query = f"""
            SELECT unclaimed_fee_token0, unclaimed_fee_token1, created_at
            FROM wallet_nft_position
            WHERE {where_sql}
            ORDER BY created_at DESC
            LIMIT 1
        """
        cursor.execute(query, tuple(params))
        result = cursor.fetchone()
        if result:
            unclaimed_fee_token0, unclaimed_fee_token1, created_at = result
            return {
                "unclaimed_fee_token0": unclaimed_fee_token0,
                "unclaimed_fee_token1": unclaimed_fee_token1,
                "created_at": created_at
            }
        else:
            return None

    except mysql.connector.Error as e:
        print(f"Error fetching last pending cake info: {e}")
        return None

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_data_inactive_nft_id(nft_id):
    try:
        conn = get_connection()
        cursor = conn.cursor()

        query = """
            SELECT token0_symbol, token1_symbol, current_token0_amount, current_token1_amount, current_total_value, farm_apr_all
            FROM wallet_nft_position
            WHERE nft_id = %s
            ORDER BY created_at DESC
            LIMIT 1
        """
        cursor.execute(query, (nft_id,))
        result = cursor.fetchone()
        return result if result else None

    except mysql.connector.Error as e:
        print(f"Error fetching last pending cake: {e}")
        return None

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

# Get previous status of NFT ID
def get_db_active_inactive_nft_ids(wallet_address, chain):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        sql = """
            SELECT t1.nft_id
            FROM wallet_nft_position t1
            JOIN (
                SELECT nft_id, MAX(created_at) AS latest
                FROM wallet_nft_position
                WHERE wallet_address = %s AND chain = %s
                GROUP BY nft_id, COALESCE(npm_address, '')
            ) t2 ON t1.nft_id = t2.nft_id AND t1.created_at = t2.latest
            WHERE t1.status IN ('Active', 'Inactive')
        """
        cursor.execute(sql, (wallet_address, chain))
        result = cursor.fetchall()
        return [row[0] for row in result] if result else []

    except mysql.connector.Error as e:
        print(f"Error fetching NFT ID Active: {e}")
        return None

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()
            
def get_db_active_nft_ids(wallet_address, chain):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        sql = """
            SELECT t1.nft_id
            FROM wallet_nft_position t1
            JOIN (
                SELECT nft_id, MAX(created_at) AS latest
                FROM wallet_nft_position
                WHERE wallet_address = %s AND chain = %s
                GROUP BY nft_id, COALESCE(npm_address, '')
            ) t2 ON t1.nft_id = t2.nft_id AND t1.created_at = t2.latest
            WHERE t1.status='Active'
        """
        cursor.execute(sql, (wallet_address, chain))
        result = cursor.fetchall()
        return [row[0] for row in result] if result else []

    except mysql.connector.Error as e:
        print(f"Error fetching NFT ID Active: {e}")
        return None

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def fetch_summary_by_token(wallet_address, start_date=None, end_date=None):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # Convert dates
        start_date = to_datetime_safe(start_date)
        end_date = to_datetime_safe(end_date)
        if end_date:
            end_date += timedelta(days=1)  # inclusive

        # Build date filter
        date_condition = ""
        if start_date and end_date:
            date_condition = "AND created_at >= %s AND created_at < %s"
        elif start_date:
            date_condition = "AND created_at >= %s"
        elif end_date:
            date_condition = "AND created_at < %s"

        # The condition will be used inside all subqueries
        subquery_condition = f"""
            AND (wallet_address, nft_id, created_at) IN (
                SELECT wallet_address, nft_id, MAX(created_at)
                FROM wallet_nft_position
                WHERE wallet_address = %s {date_condition}
                GROUP BY wallet_address, nft_id
            )
        """

        # Final SQL query
        query = f"""
            SELECT 
                combined.token, 
                SUM(combined.initial_amount) AS initial, 
                SUM(combined.current_amount) AS current, 
                SUM(combined.delta_amount) AS delta, 
                SUM(combined.delta_amount_usd) AS delta_usd,
                reward_data.total_pending_cake AS reward
            FROM (
                SELECT 
                    token0_symbol AS token,
                    initial_token0_amount AS initial_amount,
                    current_token0_amount AS current_amount,
                    (current_token0_amount - initial_token0_amount) AS delta_amount,
                    (current_token0_amount - initial_token0_amount) * price_token0 AS delta_amount_usd
                FROM wallet_nft_position
                WHERE wallet_address = %s AND status != 'Closed'
                {subquery_condition}

                UNION ALL

                SELECT 
                    token1_symbol AS token,
                    initial_token1_amount AS initial_amount,
                    current_token1_amount AS current_amount,
                    (current_token1_amount - initial_token1_amount) AS delta_amount,
                    (current_token1_amount - initial_token1_amount) * price_token1 AS delta_amount_usd
                FROM wallet_nft_position
                WHERE wallet_address = %s AND status != 'Closed'
                {subquery_condition}
            ) AS combined

            CROSS JOIN (
                SELECT SUM(pending_cake) AS total_pending_cake
                FROM wallet_nft_position
                WHERE wallet_address = %s AND status != 'Closed'
                {subquery_condition}
            ) AS reward_data

            GROUP BY combined.token, reward_data.total_pending_cake
            ORDER BY combined.token
        """

        # Prepare parameters for subqueries
        def get_date_params():
            if start_date and end_date:
                return [wallet_address, start_date, end_date]
            elif start_date:
                return [wallet_address, start_date]
            elif end_date:
                return [wallet_address, end_date]
            else:
                return [wallet_address]

        params = (
            [wallet_address] + get_date_params() +  # for first subquery
            [wallet_address] + get_date_params() +  # for second subquery
            [wallet_address] + get_date_params()    # for reward_data
        )

        print("Final SQL:", query)
        print("With parameters:", params)

        cursor.execute(query, tuple(params))
        result = cursor.fetchall()
        return result

    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def fetch_summary_by_wallet_and_chain(wallet_address, chain, start_date=None, end_date=None):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # Convert dates
        start_date = to_datetime_safe(start_date)
        end_date = to_datetime_safe(end_date)
        if end_date:
            end_date += timedelta(days=1)  # inclusive

        # Build date filter
        date_condition = ""
        if start_date and end_date:
            date_condition = "AND created_at >= %s AND created_at < %s"
        elif start_date:
            date_condition = "AND created_at >= %s"
        elif end_date:
            date_condition = "AND created_at < %s"

        # The condition will be used inside all subqueries
        subquery_condition = f"""
            AND (wallet_address, nft_id, created_at) IN (
                SELECT wallet_address, nft_id, MAX(created_at)
                FROM wallet_nft_position
                WHERE wallet_address = %s AND chain = %s {date_condition}
                GROUP BY wallet_address, nft_id
            )
        """

        # Final SQL query
        query = f"""
            SELECT 
                combined.token, 
                SUM(combined.initial_amount) AS initial, 
                SUM(combined.current_amount) AS current, 
                SUM(combined.delta_amount) AS delta, 
                SUM(combined.delta_amount_usd) AS delta_usd,
                reward_data.total_pending_cake AS reward
            FROM (
                SELECT 
                    token0_symbol AS token,
                    initial_token0_amount AS initial_amount,
                    current_token0_amount AS current_amount,
                    (current_token0_amount - initial_token0_amount) AS delta_amount,
                    (current_token0_amount - initial_token0_amount) * price_token0 AS delta_amount_usd
                FROM wallet_nft_position
                WHERE wallet_address = %s AND chain = %s AND status != 'Closed'
                {subquery_condition}

                UNION ALL

                SELECT 
                    token1_symbol AS token,
                    initial_token1_amount AS initial_amount,
                    current_token1_amount AS current_amount,
                    (current_token1_amount - initial_token1_amount) AS delta_amount,
                    (current_token1_amount - initial_token1_amount) * price_token1 AS delta_amount_usd
                FROM wallet_nft_position
                WHERE wallet_address = %s AND chain = %s AND status != 'Closed'
                {subquery_condition}
            ) AS combined

            CROSS JOIN (
                SELECT SUM(pending_cake) AS total_pending_cake
                FROM wallet_nft_position
                WHERE wallet_address = %s AND chain = %s AND status != 'Closed'
                {subquery_condition}
            ) AS reward_data

            GROUP BY combined.token, reward_data.total_pending_cake
            ORDER BY combined.token
        """

        # Prepare parameters for subqueries
        def get_date_params():
            if start_date and end_date:
                return [wallet_address, chain, start_date, end_date]
            elif start_date:
                return [wallet_address, chain, start_date]
            elif end_date:
                return [wallet_address, chain, end_date]
            else:
                return [wallet_address, chain]

        params = (
            [wallet_address, chain] + get_date_params() +  # for first subquery
            [wallet_address, chain] + get_date_params() +  # for second subquery
            [wallet_address, chain] + get_date_params()    # for reward_data
        )

        print("Final SQL:", query)
        print("With parameters:", params)

        cursor.execute(query, tuple(params))
        result = cursor.fetchall()
        return result

    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_total_pending_cake_by_wallet(wallet_address, start_date=None, end_date=None):
    try:
        conn = get_connection()
        cursor = conn.cursor()

        start_date = to_datetime_safe(start_date)
        end_date = to_datetime_safe(end_date)
        if end_date:
            end_date += timedelta(days=1)

        # Date filter
        date_condition = ""
        if start_date and end_date:
            date_condition = "AND created_at >= %s AND created_at < %s"
        elif start_date:
            date_condition = "AND created_at >= %s"
        elif end_date:
            date_condition = "AND created_at < %s"

        subquery = f"""
            SELECT wallet_address, nft_id, MAX(created_at) AS max_created
            FROM wallet_nft_position
            WHERE wallet_address = %s {date_condition}
            GROUP BY wallet_address, nft_id
        """

        main_query = f"""
            SELECT SUM(wp.pending_cake)
            FROM wallet_nft_position wp
            INNER JOIN (
                {subquery}
            ) latest
                ON wp.wallet_address = latest.wallet_address
                AND wp.nft_id = latest.nft_id
                AND wp.created_at = latest.max_created
            WHERE wp.status != 'Burned'
        """

        params = [wallet_address]
        if start_date and end_date:
            params += [start_date, end_date]
        elif start_date:
            params.append(start_date)
        elif end_date:
            params.append(end_date)

        cursor.execute(main_query, tuple(params))
        result = cursor.fetchone()[0]
        return result if result is not None else 0

    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn: {e}")
        return 0

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_total_pending_cake_by_wallet_and_chain(wallet_address, chain, start_date=None, end_date=None):
    try:
        conn = get_connection()
        cursor = conn.cursor()

        start_date = to_datetime_safe(start_date)
        end_date = to_datetime_safe(end_date)
        if end_date:
            end_date += timedelta(days=1)

        # Date filter
        date_condition = ""
        if start_date and end_date:
            date_condition = "AND created_at >= %s AND created_at < %s"
        elif start_date:
            date_condition = "AND created_at >= %s"
        elif end_date:
            date_condition = "AND created_at < %s"

        subquery = f"""
            SELECT wallet_address, chain, nft_id, MAX(created_at) AS max_created
            FROM wallet_nft_position
            WHERE wallet_address = %s AND chain = %s {date_condition}
            GROUP BY wallet_address, nft_id
        """

        main_query = f"""
            SELECT SUM(wp.pending_cake)
            FROM wallet_nft_position wp
            INNER JOIN (
                {subquery}
            ) latest
                ON wp.wallet_address = latest.wallet_address
                AND wp.chain = latest.chain
                AND wp.nft_id = latest.nft_id
                AND wp.created_at = latest.max_created
            WHERE wp.status != 'Burned'
        """

        params = [wallet_address, chain]
        if start_date and end_date:
            params += [start_date, end_date]
        elif start_date:
            params.append(start_date)
        elif end_date:
            params.append(end_date)

        cursor.execute(main_query, tuple(params))
        result = cursor.fetchone()[0]
        return result if result is not None else 0

    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn: {e}")
        return 0

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()
 
def count_nft_by_wallet(wallet_address, start_date=None, end_date=None):
    try:
        conn = get_connection()
        cursor = conn.cursor()

        # Convert start_date and end_date to datetime if they're strings
        start_date = to_datetime_safe(start_date)
        end_date = to_datetime_safe(end_date)
        if end_date:
            end_date += timedelta(days=1)  # inclusive

        # Build date condition
        date_condition = ""
        if start_date and end_date:
            date_condition = "AND created_at >= %s AND created_at < %s"
        elif start_date:
            date_condition = "AND created_at >= %s"
        elif end_date:
            date_condition = "AND created_at < %s"

        query = f"""
            SELECT COUNT(*) 
            FROM wallet_nft_position
            WHERE LOWER(wallet_address) = LOWER(%s)
            AND status != 'Burned'
            {date_condition}
        """

        # Prepare parameters
        params = [wallet_address]
        if start_date and end_date:
            params.extend([start_date, end_date])
        elif start_date:
            params.append(start_date)
        elif end_date:
            params.append(end_date)

        print("Final SQL COUNT:", query)
        print("With parameters:", params)

        cursor.execute(query, tuple(params))
        result = cursor.fetchone()[0]
        return result

    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn: {e}")
        return 0

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def count_nft_by_wallet_and_chain(chain, wallet_address, start_date=None, end_date=None):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
         # Convert start_date and end_date to datetime if they're strings
        start_date = to_datetime_safe(start_date)
        end_date = to_datetime_safe(end_date)
        if end_date:
            end_date += timedelta(days=1)  # inclusive

        # Build date condition
        date_condition = ""
        if start_date and end_date:
            date_condition = "AND created_at >= %s AND created_at < %s"
        elif start_date:
            date_condition = "AND created_at >= %s"
        elif end_date:
            date_condition = "AND created_at < %s"
            
        query = f"""
            SELECT COUNT(*)
            FROM wallet_nft_position
            WHERE chain = %s 
            AND wallet_address = %s 
            AND status != 'Burned'
            {date_condition}
        """
        
        # Prepare parameters
        params = [chain, wallet_address]
        if start_date and end_date:
            params.extend([start_date, end_date])
        elif start_date:
            params.append(start_date)
        elif end_date:
            params.append(end_date)

        print("Final SQL COUNT:", query)
        print("With parameters:", params)

        cursor.execute(query, tuple(params))
        result = cursor.fetchone()[0]
        return result

    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn: {e}")
        return 0

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close() 

def count_all_nft():
    query = """
        SELECT COUNT(*) 
        FROM wallet_nft_position
        WHERE status != 'Burned';
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute(query)
        result = cursor.fetchone()[0]
        return result

    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn: {e}")
        return 0

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
    
def update_nft_status_to_closed(wallet_address, chain_name, nft_id):
    query = """
        UPDATE wallet_nft_position p
        JOIN (
            SELECT wallet_address, nft_id, chain, MAX(created_at) AS latest_created_at
            FROM wallet_nft_position
            GROUP BY wallet_address, nft_id, chain
        ) t 
        ON p.wallet_address = t.wallet_address 
        AND p.nft_id = t.nft_id 
        AND p.chain = t.chain 
        AND p.created_at = t.latest_created_at
        SET p.status = 'Closed', p.is_active = 0, p.updated_at = NOW()
        WHERE p.wallet_address = %s AND p.nft_id = %s AND p.chain = %s;
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute(query, (wallet_address, nft_id, chain_name,
                               wallet_address, nft_id, chain_name
                               )
                       )
        conn.commit()

    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn: {e}")

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def update_nft_status_to_burned(wallet_address, chain_name, nft_id, type_dex, npm_address="", pool_address=None):
    pool_select = ", pool_address" if pool_address else ""
    pool_group = ", pool_address" if pool_address else ""
    pool_join = "AND p.pool_address = t.pool_address" if pool_address else ""
    pool_inner_where = "AND pool_address = %s" if pool_address else ""
    pool_outer_where = "AND p.pool_address = %s" if pool_address else ""
    query = f"""
        UPDATE wallet_nft_position p
        JOIN (
            SELECT
                wallet_address,
                nft_id,
                chain,
                type_dex,
                COALESCE(npm_address, '') AS npm_address{pool_select},
                MAX(created_at) AS latest_created_at
            FROM wallet_nft_position
            WHERE wallet_address = %s
                AND nft_id = %s
                AND chain = %s
                AND type_dex = %s
                AND COALESCE(npm_address, '') = %s
                {pool_inner_where}
            GROUP BY wallet_address, nft_id, chain, type_dex, COALESCE(npm_address, ''){pool_group}
        ) t 
        ON p.wallet_address = t.wallet_address 
        AND p.nft_id = t.nft_id 
        AND p.chain = t.chain 
        AND p.type_dex = t.type_dex
        AND COALESCE(p.npm_address, '') = t.npm_address
        {pool_join}
        AND p.created_at = t.latest_created_at
        SET p.status = 'Burned', p.is_active = 0, p.updated_at = NOW()
        WHERE p.wallet_address = %s
            AND p.nft_id = %s
            AND p.chain = %s
            AND p.type_dex = %s
            AND COALESCE(p.npm_address, '') = %s
            {pool_outer_where};
    """
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()

        identity_params = [wallet_address, nft_id, chain_name, type_dex, npm_address or ""]
        params = list(identity_params)
        if pool_address:
            params.append(pool_address)
        params.extend(identity_params)
        if pool_address:
            params.append(pool_address)
        cursor.execute(query, tuple(params))
        rowcount = cursor.rowcount
        conn.commit()
        if rowcount == 0:
            print(
                "Burned update matched 0 rows: "
                f"wallet={wallet_address}, chain={chain_name}, nft_id={nft_id}, "
                f"type_dex={type_dex}, npm_address={npm_address or ''}, pool_address={pool_address or ''}"
            )
        return rowcount

    except mysql.connector.Error as e:
        print(
            "Burned update failed: "
            f"wallet={wallet_address}, chain={chain_name}, nft_id={nft_id}, "
            f"type_dex={type_dex}, npm_address={npm_address or ''}, pool_address={pool_address or ''}, error={e}"
        )
        return 0
        print(f"Lỗi truy vấn: {e}")

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def get_cached_closed_nft_ids(wallet_address, chain_name, type_dex=None, npm_address=None):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        filters = ["wallet_address = %s", "chain_name = %s", "status = 'Burned'"]
        params = [wallet_address, chain_name]
        if type_dex is not None:
            filters.append("type_dex = %s")
            params.append(type_dex)
        if npm_address is not None:
            filters.append("npm_address = %s")
            params.append(npm_address or "")
        query = f"""
            SELECT nft_id
            FROM nft_closed_cache
            WHERE {' AND '.join(filters)};
        """
        cursor.execute(query, tuple(params))
        result = cursor.fetchall()
        return [row[0] for row in result] if result else []

    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn: {e}")
        return []

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def insert_nft_closed_cache(wallet_address, chain_name, nft_id, status, type_dex, npm_address=""):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        query = """
            INSERT IGNORE INTO nft_closed_cache (wallet_address, chain_name, nft_id, status, type_dex, npm_address)
            VALUES (%s, %s, %s, %s, %s, %s);
        """
        cursor.execute(query, (wallet_address, chain_name, nft_id, status, type_dex, npm_address or ""))
        conn.commit()

    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn: {e}")

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
            
def fetch_list_bond_data():
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT * FROM list_bond_contract_notify 
            WHERE status="active";
        """
        cursor.execute(query)

        results = cursor.fetchall()
        return results

    except mysql.connector.Error as e:
        print(f"Error fetching all NFT data: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def insert_bond_data(chain, contract_address, token_symbol, status, notify_threshold=20.0):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        query = """
            INSERT IGNORE INTO list_bond_contract_notify (chain, contract_address, token_symbol, status, notify_threshold)
            VALUES (%s, %s, %s, %s, %s);
        """
        cursor.execute(query, (chain, contract_address, token_symbol, status, notify_threshold))
        conn.commit()

    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn: {e}")

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()    
            
def update_bond_status(contract_address, status):
    query = """
        UPDATE list_bond_contract_notify
        SET status = %s
        WHERE contract_address = %s;
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute(query, (status, contract_address))
        conn.commit()

    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn: {e}")

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def fetch_bond_data_by_contract_address(contract_address):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT * FROM list_bond_contract_notify
            WHERE contract_address = %s;
        """
        cursor.execute(query, (contract_address,))

        results = cursor.fetchone()
        return results

    except mysql.connector.Error as e:
        print(f"Error fetching all NFT data: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def update_bond_data(chain, new_contract_address, token_symbol, status, notify_threshold, old_contract_address):
    query = """
        UPDATE list_bond_contract_notify
        SET chain = %s, contract_address = %s, token_symbol = %s, status = %s, notify_threshold = %s
        WHERE contract_address = %s;
    """
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(query, (chain, new_contract_address, token_symbol, status, notify_threshold, old_contract_address))
        conn.commit()

        if cursor.rowcount == 0:
            print(f"[INFO] Không có bond nào với contract_address = {old_contract_address} được cập nhật.")
        else:
            print(f"[INFO] Đã cập nhật bond với contract_address = {old_contract_address}.")

    except mysql.connector.Error as e:
        print(f"[ERROR] Lỗi khi cập nhật bond: {e}")

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
            
def update_bond_threshold(contract_address, notify_threshold):
    query = """
        UPDATE list_bond_contract_notify
        SET notify_threshold = %s
        WHERE contract_address = %s;
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute(query, (notify_threshold, contract_address))
        conn.commit()
        return True
    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn update_bond_threshold: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def delete_bond_contract(contract_address):
    query = """
        DELETE FROM list_bond_contract_notify
        WHERE contract_address = %s;
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute(query, (contract_address,))
        conn.commit()

    except mysql.connector.Error as e:
        print(f"Lỗi truy vấn: {e}")

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
            
def fetch_and_update_bonds():
    url = "https://realtime-api.ape.bond/bonds"
    conn = None
    cursor = None
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        bond_data = response.json()
        
        bonds_list = bond_data.get('bonds', [])   
        
        conn = get_connection()
        cursor = conn.cursor()
        
        api_contract_addresses = set()
        supported_chains = set()
        
        for bond in bonds_list:
            is_active = not bond.get('soldOut', True)
            status = 'active' if is_active else 'sold'

            # Chỉ xử lý bond active
            if status != 'active':
                continue
            
            chain_id = bond.get('chainId')
            chain = ID_CHAIN_MAP.get(chain_id)
            
            if chain is None:
                continue
            
            # Skip specific chain if needed (like 10143 if it's meant to be skipped)
            if chain_id == 10143:
                continue
                
            supported_chains.add(chain)
            
            contract_address = bond.get('billAddress')
            token_symbol = bond.get('payoutTokenName', '')
            
            if not contract_address:
                continue 
            
            # Add to set to track
            contract_address_lower = contract_address.lower()
            api_contract_addresses.add(contract_address_lower)
            
            query = """
                INSERT INTO list_bond_contract_notify (chain, contract_address, token_symbol, status)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    chain = VALUES(chain),
                    token_symbol = VALUES(token_symbol),
                    status = VALUES(status),
                    updated_at = CURRENT_TIMESTAMP
            """
            value = (chain, contract_address_lower, token_symbol, status)
            cursor.execute(query, value)

        if supported_chains:
            # Lấy danh sách các bond hiện có trong DB thuộc các chain đã xử lý
            placeholders = ', '.join(['%s'] * len(supported_chains))
            query = f"SELECT LOWER(contract_address) FROM list_bond_contract_notify WHERE status = 'active' AND chain IN ({placeholders})"
            cursor.execute(query, list(supported_chains))
            db_bonds = set(row[0] for row in cursor.fetchall())

            # Tìm các bond không còn trong API → cập nhật trạng thái thành 'sold'
            missing_bonds = db_bonds - api_contract_addresses
            print(f"[INFO] Found {len(missing_bonds)} bonds to mark as sold for chains {supported_chains}")
            
            for old_contract in missing_bonds:
                cursor.execute("""
                    UPDATE list_bond_contract_notify
                    SET status = 'sold', updated_at = CURRENT_TIMESTAMP
                    WHERE LOWER(contract_address) = %s
                """, (old_contract,))
        
        conn.commit()
        print(f"[INFO] Successfully updated bonds from API.")
    
    except mysql.connector.Error as e:
        if conn:
            conn.rollback()
        print(f"[ERROR] MySQL error: {e}")
    except requests.RequestException as e:
        print(f"[ERROR] API request error: {e}")
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"[ERROR] Unexpected error: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
