-- Create database if not exists
CREATE DATABASE IF NOT EXISTS apebond;

-- Create table if not exists
CREATE TABLE IF NOT EXISTS wallet_nft_position (
    id INT AUTO_INCREMENT PRIMARY KEY,
    wallet_address VARCHAR(42) NOT NULL,
    chain VARCHAR(20) NOT NULL,
    nft_id BIGINT UNSIGNED NOT NULL,
    status VARCHAR(20),

    date_add_liquidity DATETIME,

    initial_token0_amount DECIMAL(38,18),
    initial_token1_amount DECIMAL(38,18),
    initial_total_value DECIMAL(38,18),

    current_token0_amount DECIMAL(38,18),
    current_token1_amount DECIMAL(38,18),
    current_total_value DECIMAL(38,18),

    delta_amount DECIMAL(38,18),
    percent_change DECIMAL(10,6),

    unclaimed_fee_token0 DECIMAL(38,18),
    unclaimed_fee_token1 DECIMAL(38,18),
    total_unclaimed_fee DECIMAL(38,18),

    lp_fee_apr DECIMAL(10,6),
    pending_cake DECIMAL(38,18),
    boost_multiplier DECIMAL(10,4),

    farm_apr_1h DECIMAL(10,6),
    farm_apr_all DECIMAL(10,6),

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uniq_wallet_chain_nft (wallet_address, chain, nft_id)
) ENGINE=InnoDB;


-- Create table nft_closed_cache table
CREATE TABLE IF NOT EXISTS nft_closed_cache (
    wallet_address VARCHAR(64) NOT NULL,
    chain_name VARCHAR(32) NOT NULL,
    nft_id VARCHAR(32) NOT NULL,
    status VARCHAR(20) NOT NULL,
    last_checked_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (wallet_address, chain_name, nft_id)
);
