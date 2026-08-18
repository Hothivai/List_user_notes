import mysql.connector
from flask import jsonify
from datetime import datetime, timedelta
from services.pancake_api import get_data_pool_bsc
from services.helper import to_datetime_safe
from web3 import Web3
from services.db_connect import get_connection, get_db_config
from config import RPC_BACKUP_LIST

DB_CONFIG = get_db_config()

SUMMARY_IDENTITY_JOIN = """
    h.wallet_address = s.wallet_address
    AND h.chain = s.chain
    AND h.type_dex = s.type_dex
    AND h.nft_id = s.nft_id
    AND COALESCE(h.npm_address, '') = COALESCE(s.npm_address, '')
"""

LATEST_POSITION_IDENTITY_JOIN = """
    SELECT
        latest_rows.wallet_address,
        latest_rows.chain,
        latest_rows.type_dex,
        latest_rows.nft_id,
        COALESCE(latest_rows.npm_address, '') AS npm_address,
        MAX(latest_rows.id) AS latest_id
    FROM wallet_nft_position latest_rows
    INNER JOIN (
        SELECT wallet_address, chain, type_dex, nft_id, COALESCE(npm_address, '') AS npm_address, MAX(created_at) AS max_time
        FROM wallet_nft_position
        GROUP BY wallet_address, chain, type_dex, nft_id, COALESCE(npm_address, '')
    ) latest_time ON latest_rows.wallet_address = latest_time.wallet_address
        AND latest_rows.chain = latest_time.chain
        AND latest_rows.type_dex = latest_time.type_dex
        AND latest_rows.nft_id = latest_time.nft_id
        AND COALESCE(latest_rows.npm_address, '') = latest_time.npm_address
        AND latest_rows.created_at = latest_time.max_time
    GROUP BY latest_rows.wallet_address, latest_rows.chain, latest_rows.type_dex, latest_rows.nft_id, COALESCE(latest_rows.npm_address, '')
"""

BLACKLIST_IDENTITY_JOIN = """
    h.wallet_address = b.wallet_address
    AND h.chain = b.chain
    AND h.nft_id = b.nft_id
    AND (b.type_dex = h.type_dex OR COALESCE(b.type_dex, '') = '')
    AND (
        COALESCE(b.npm_address, '') = COALESCE(h.npm_address, '')
        OR (h.type_dex = 'aerodrome' AND COALESCE(b.npm_address, '') = '')
    )
"""

def filter_aerodrome_legacy_shadow_rows(rows):
    if not rows:
        return rows

    unique_rows = []
    seen_position_ids = set()
    for row in rows:
        position_id = row.get("id")
        if position_id is not None:
            if position_id in seen_position_ids:
                continue
            seen_position_ids.add(position_id)
        unique_rows.append(row)

    rows = unique_rows

    concrete_identities = {
        (
            row.get("wallet_address"),
            row.get("chain"),
            row.get("type_dex"),
            str(row.get("nft_id")),
            str(row.get("pool_address") or "").lower(),
        )
        for row in rows
        if row.get("type_dex") == "aerodrome"
        and (row.get("npm_address") or "")
        and row.get("pool_address")
    }

    if not concrete_identities:
        return rows

    filtered = []
    for row in rows:
        if (
            row.get("type_dex") == "aerodrome"
            and not (row.get("npm_address") or "")
            and row.get("pool_address")
            and (
                row.get("wallet_address"),
                row.get("chain"),
                row.get("type_dex"),
                str(row.get("nft_id")),
                str(row.get("pool_address") or "").lower(),
            ) in concrete_identities
        ):
            continue
        filtered.append(row)

    return filtered

def fetch_latest_nft_id(status):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # h.* sẽ lấy lại toàn bộ 30+ cột mà bạn đã insert vào history
        query = """
            SELECT 
                h.*,
                COALESCE(s.net_invested_capital, 0) AS net_invested_capital, 
                COALESCE(s.total_claimed_fee0, 0) AS total_claimed_fee0, 
                COALESCE(s.total_claimed_fee1, 0) AS total_claimed_fee1, 
                COALESCE(s.total_claimed_reward, 0) AS total_claimed_reward,
                COALESCE(s.total_claimed_fee_usd, 0) AS total_claimed_fee_usd,
                COALESCE(s.total_claimed_reward_usd, 0) AS total_claimed_reward_usd,
                COALESCE(s.total_cash_injected, 0) AS total_cash_injected,
                COALESCE(h.pnl_value_usd, 0) AS pnl_value_usd,
                COALESCE(h.pnl_value_base, 0) AS pnl_value_base,
                s.base_symbol,
                CASE WHEN b.id IS NOT NULL THEN 1 ELSE 0 END AS is_blacklisted,
                COALESCE(h.total_active_staked_usd, 0) AS total_active_staked_usd,
                COALESCE(h.total_pool_liquidity_usd, 0) AS total_pool_liquidity_usd,
                COALESCE(p_evm.alloc_point, 0) AS alloc_point_evm,
                COALESCE(p_evm.fee, 0) AS fee_evm,
                COALESCE(p_evm.cake_per_day, 0) AS cake_per_day_evm,
                COALESCE(p_sol.fee, 0) AS fee_sol,
                COALESCE(p_sol.weekly_rewards, 0) AS weekly_rewards,
                COALESCE(p_aero_epoch.reward_per_day, 0) AS reward_per_day_aero,
                COALESCE(p_aero_info.tick_spacing, 0) AS tick_spacing_aero,
                COALESCE(p_sol.token0_decimals, 0) AS token0_decimals_sol,
                COALESCE(p_sol.token1_decimals, 0) AS token1_decimals_sol,
                CASE
                    WHEN h.type_dex = 'aerodrome' THEN COALESCE(p_aero_info.token0_decimals, 0)
                    ELSE COALESCE(p_evm.token0_decimals, 0)
                END AS token0_decimals_evm,
                CASE
                    WHEN h.type_dex = 'aerodrome' THEN COALESCE(p_aero_info.token1_decimals, 0)
                    ELSE COALESCE(p_evm.token1_decimals, 0)
                END AS token1_decimals_evm
            FROM wallet_nft_position h
            LEFT JOIN wallet_nft_summary s ON {summary_identity_join}
            INNER JOIN (
                {latest_identity}
            ) last_h ON h.wallet_address = last_h.wallet_address
                AND h.chain = last_h.chain
                AND h.type_dex = last_h.type_dex
                AND h.nft_id = last_h.nft_id
                AND COALESCE(h.npm_address, '') = last_h.npm_address
                AND h.id = last_h.latest_id
            LEFT JOIN nft_blacklist b 
                ON {blacklist_identity_join}
            LEFT JOIN pool_sol_info p_sol 
                ON h.chain = 'SOL' AND h.pool_address = p_sol.pool_account
            LEFT JOIN pool_info p_evm 
                ON h.chain != 'SOL' AND h.pool_address = p_evm.pool_address AND h.chain = p_evm.chain
            LEFT JOIN aerodrome_pool_epoch_state p_aero_epoch 
                ON h.chain != 'SOL' AND h.pool_address = p_aero_epoch.pool_address AND h.chain = p_aero_epoch.chain
            LEFT JOIN aerodrome_pool_info p_aero_info 
                ON h.chain != 'SOL' AND h.pool_address = p_aero_info.pool_address AND h.chain = p_aero_info.chain
            WHERE b.id IS NULL {status_filter}
            ORDER BY h.created_at DESC;
        """

        params = []
        status_filter = ""
        if status is not None:
            status_filter = "AND h.status != %s"
            params.append(status)
            
        final_query = query.format(
            status_filter=status_filter,
            summary_identity_join=SUMMARY_IDENTITY_JOIN,
            latest_identity=LATEST_POSITION_IDENTITY_JOIN,
            blacklist_identity_join=BLACKLIST_IDENTITY_JOIN,
        )
        cursor.execute(final_query, tuple(params))
        return filter_aerodrome_legacy_shadow_rows(cursor.fetchall())

    except mysql.connector.Error as e:
        print(f"Error fetching full NFT data: {e}")
        return []
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if 'conn' in locals() and conn: conn.close()

def fetch_latest_nft_by_wallet(wallet_address):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT 
                h.*,
                COALESCE(s.net_invested_capital, 0) AS net_invested_capital, 
                COALESCE(s.total_claimed_fee0, 0) AS total_claimed_fee0, 
                COALESCE(s.total_claimed_fee1, 0) AS total_claimed_fee1, 
                COALESCE(s.total_claimed_reward, 0) AS total_claimed_reward,
                COALESCE(s.total_claimed_fee_usd, 0) AS total_claimed_fee_usd,
                COALESCE(s.total_claimed_reward_usd, 0) AS total_claimed_reward_usd,
                COALESCE(s.total_cash_injected, 0) AS total_cash_injected,
                COALESCE(h.pnl_value_usd, 0) AS pnl_value_usd,
                COALESCE(h.pnl_value_base, 0) AS pnl_value_base,
                s.base_symbol,
                CASE WHEN b.id IS NOT NULL THEN 1 ELSE 0 END AS is_blacklisted,
                COALESCE(h.total_active_staked_usd, 0) AS total_active_staked_usd,
                COALESCE(h.total_pool_liquidity_usd, 0) AS total_pool_liquidity_usd,
                COALESCE(p_evm.alloc_point, 0) AS alloc_point_evm,
                COALESCE(p_evm.fee, 0) AS fee_evm,
                COALESCE(p_evm.cake_per_day, 0) AS cake_per_day_evm,
                COALESCE(p_sol.fee, 0) AS fee_sol,
                COALESCE(p_sol.weekly_rewards, 0) AS weekly_rewards,
                COALESCE(p_aero_epoch.reward_per_day, 0) AS reward_per_day_aero,
                COALESCE(p_aero_info.tick_spacing, 0) AS tick_spacing_aero,
                COALESCE(p_sol.token0_decimals, 0) AS token0_decimals_sol,
                COALESCE(p_sol.token1_decimals, 0) AS token1_decimals_sol,
                CASE
                    WHEN h.type_dex = 'aerodrome' THEN COALESCE(p_aero_info.token0_decimals, 0)
                    ELSE COALESCE(p_evm.token0_decimals, 0)
                END AS token0_decimals_evm,
                CASE
                    WHEN h.type_dex = 'aerodrome' THEN COALESCE(p_aero_info.token1_decimals, 0)
                    ELSE COALESCE(p_evm.token1_decimals, 0)
                END AS token1_decimals_evm
            FROM wallet_nft_position h
            LEFT JOIN wallet_nft_summary s ON {summary_identity_join}
            INNER JOIN (
                {latest_identity}
            ) last_h ON h.wallet_address = last_h.wallet_address
                AND h.chain = last_h.chain
                AND h.type_dex = last_h.type_dex
                AND h.nft_id = last_h.nft_id
                AND COALESCE(h.npm_address, '') = last_h.npm_address
                AND h.id = last_h.latest_id
            LEFT JOIN nft_blacklist b 
                ON {blacklist_identity_join}
            LEFT JOIN pool_sol_info p_sol 
                ON h.chain = 'SOL' AND h.pool_address = p_sol.pool_account
            LEFT JOIN pool_info p_evm 
                ON h.chain != 'SOL' AND h.pool_address = p_evm.pool_address AND h.chain = p_evm.chain
            LEFT JOIN aerodrome_pool_epoch_state p_aero_epoch 
                ON h.chain != 'SOL' AND h.pool_address = p_aero_epoch.pool_address AND h.chain = p_aero_epoch.chain
            LEFT JOIN aerodrome_pool_info p_aero_info 
                ON h.chain != 'SOL' AND h.pool_address = p_aero_info.pool_address AND h.chain = p_aero_info.chain
            WHERE h.wallet_address = %s AND b.id IS NULL
            ORDER BY h.created_at DESC;
        """

        final_query = query.format(
            summary_identity_join=SUMMARY_IDENTITY_JOIN,
            latest_identity=LATEST_POSITION_IDENTITY_JOIN,
            blacklist_identity_join=BLACKLIST_IDENTITY_JOIN,
        )
        cursor.execute(final_query, (wallet_address,))
        return filter_aerodrome_legacy_shadow_rows(cursor.fetchall())

    except mysql.connector.Error as e:
        print(f"Error fetching NFTs by wallet: {e}")
        return []
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if 'conn' in locals() and conn: conn.close()
            
def fetch_latest_nft_by_wallet_and_chain(wallet_address, chain):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT 
                h.*,
                s.net_invested_capital, 
                s.total_claimed_fee0, 
                s.total_claimed_fee1, 
                s.total_claimed_reward,
                s.total_claimed_fee_usd,
                s.total_claimed_reward_usd,
                s.total_cash_injected,
                h.pnl_value_usd,
                h.pnl_value_base,
                s.base_symbol,
                CASE WHEN b.id IS NOT NULL THEN 1 ELSE 0 END AS is_blacklisted,
                COALESCE(h.total_active_staked_usd, 0) AS total_active_staked_usd,
                COALESCE(h.total_pool_liquidity_usd, 0) AS total_pool_liquidity_usd,
                COALESCE(p_evm.alloc_point, 0) AS alloc_point_evm,
                COALESCE(p_evm.fee, 0) AS fee_evm,
                COALESCE(p_evm.cake_per_day, 0) AS cake_per_day_evm,
                COALESCE(p_sol.fee, 0) AS fee_sol,
                COALESCE(p_sol.weekly_rewards, 0) AS weekly_rewards,
                COALESCE(p_aero_epoch.reward_per_day, 0) AS reward_per_day_aero,
                COALESCE(p_aero_info.tick_spacing, 0) AS tick_spacing_aero,
                COALESCE(p_sol.token0_decimals, 0) AS token0_decimals_sol,
                COALESCE(p_sol.token1_decimals, 0) AS token1_decimals_sol,
                CASE
                    WHEN h.type_dex = 'aerodrome' THEN COALESCE(p_aero_info.token0_decimals, 0)
                    ELSE COALESCE(p_evm.token0_decimals, 0)
                END AS token0_decimals_evm,
                CASE
                    WHEN h.type_dex = 'aerodrome' THEN COALESCE(p_aero_info.token1_decimals, 0)
                    ELSE COALESCE(p_evm.token1_decimals, 0)
                END AS token1_decimals_evm
            FROM wallet_nft_summary s
            INNER JOIN wallet_nft_position h ON {summary_identity_join}
            INNER JOIN (
                {latest_identity}
            ) last_h ON h.wallet_address = last_h.wallet_address
                AND h.chain = last_h.chain
                AND h.type_dex = last_h.type_dex
                AND h.nft_id = last_h.nft_id
                AND COALESCE(h.npm_address, '') = last_h.npm_address
                AND h.id = last_h.latest_id
            LEFT JOIN nft_blacklist b 
                ON {blacklist_identity_join}
            LEFT JOIN pool_sol_info p_sol 
                ON s.chain = 'SOL' AND h.pool_address = p_sol.pool_account
            LEFT JOIN pool_info p_evm 
                ON s.chain != 'SOL' AND h.pool_address = p_evm.pool_address AND h.chain = p_evm.chain
            LEFT JOIN aerodrome_pool_epoch_state p_aero_epoch 
                ON s.chain != 'SOL' AND h.pool_address = p_aero_epoch.pool_address AND s.chain = p_aero_epoch.chain
            LEFT JOIN aerodrome_pool_info p_aero_info 
                ON s.chain != 'SOL' AND h.pool_address = p_aero_info.pool_address AND s.chain = p_aero_info.chain
            WHERE s.wallet_address = %s AND s.chain = %s AND b.id IS NULL
            ORDER BY h.created_at DESC;
        """

        final_query = query.format(
            summary_identity_join=SUMMARY_IDENTITY_JOIN,
            latest_identity=LATEST_POSITION_IDENTITY_JOIN,
            blacklist_identity_join=BLACKLIST_IDENTITY_JOIN,
        )
        cursor.execute(final_query, (wallet_address, chain))
        return filter_aerodrome_legacy_shadow_rows(cursor.fetchall())

    except mysql.connector.Error as e:
        print(f"Error fetching NFTs by wallet and chain: {e}")
        return []
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if 'conn' in locals() and conn: conn.close()

# def fetch_nft_by_token(token):
#     try:
#         conn = get_connection()
#         cursor = conn.cursor(dictionary=True)

#         query = """
#             SELECT t1.*, 
#                    CASE WHEN b.id IS NOT NULL THEN 1 ELSE 0 END AS is_blacklisted
#             FROM wallet_nft_position t1
#             INNER JOIN (
#                 SELECT nft_id, MAX(created_at) AS max_created_at
#                 FROM wallet_nft_position
#                 GROUP BY nft_id
#             ) t2 ON t1.nft_id = t2.nft_id AND t1.created_at = t2.max_created_at
#             LEFT JOIN nft_blacklist b 
#                    ON t1.wallet_address = b.wallet_address 
#                    AND t1.chain = b.chain 
#                    AND t1.nft_id = b.nft_id
#             LEFT JOIN pool_info p
#                    ON t1.chain = p.chain AND (
#                         (t1.token0_symbol = p.token0_symbol AND t1.token1_symbol = p.token1_symbol)
#                      OR (t1.token0_symbol = p.token1_symbol AND t1.token1_symbol = p.token0_symbol)
#                    )
#             WHERE t1.status != 'Closed'
#               AND b.id IS NULL
#         """

#         params = []

#         if token:
#             query += """
#                 AND (
#                     LOWER(p.token0_symbol) LIKE %s OR LOWER(p.token1_symbol) LIKE %s OR
#                     LOWER(p.token0_address) = %s OR LOWER(p.token1_address) = %s OR
#                     LOWER(p.pool_address) = %s
#                 )
#             """
#             token_like = f"%{token.lower()}%"
#             token_exact = token.lower()
#             params.extend([token_like, token_like, token_exact, token_exact, token_exact])

#         query += " ORDER BY t1.created_at DESC"

#         cursor.execute(query, params)
#         return cursor.fetchall()

#     except mysql.connector.Error as e:
#         print(f"[DB Error] filter_nft_by_token_only: {e}")
#         return []

#     finally:
#         if 'cursor' in locals():
#             cursor.close()
#         if 'conn' in locals():
#             conn.close()

def filter_by_token(nfts, token):
    if not token:
        return nfts

    token = str(token).lower()
    filtered = []

    for nft in nfts:
        token0_symbol = str(nft.get('token0_symbol') or '').lower()
        token1_symbol = str(nft.get('token1_symbol') or '').lower()
        token0_address = str(nft.get('token0_address') or '').lower()
        token1_address = str(nft.get('token1_address') or '').lower()
        pool_address = str(nft.get('pool_address') or '').lower()

        if (
            token in token0_symbol
            or token in token1_symbol
            or token == token0_address
            or token == token1_address
            or token == pool_address
        ):
            filtered.append(nft)

    return filtered

def enrich_with_pool_info(nfts):
    """
    Tối ưu: query tất cả pool_info chỉ 1 lần thay vì loop query từng NFT.
    """
    if not nfts:
        return nfts

    # Chuẩn bị tập hợp các cặp token để query
    token_pairs = set()
    for nft in nfts:
        chain = nft.get('chain') or ''
        t0 = nft.get('token0_symbol') or ''
        t1 = nft.get('token1_symbol') or ''
        token_pairs.add((chain, t0, t1))
        token_pairs.add((chain, t1, t0))  # để cover swap order

    # Tạo list params và placeholders
    params = []
    placeholders = []
    for chain, t0, t1 in token_pairs:
        placeholders.append("(chain=%s AND token0_symbol=%s AND token1_symbol=%s)")
        params.extend([chain, t0, t1])

    query = f"""
        SELECT *
        FROM pool_info
        WHERE {" OR ".join(placeholders)}
    """

    # Query 1 lần
    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(query, params)
        pool_rows = cursor.fetchall()

        # Map key = (chain, token0_symbol, token1_symbol)
        pool_map = {}
        for pool in pool_rows:
            key1 = (pool['chain'], pool['token0_symbol'], pool['token1_symbol'])
            key2 = (pool['chain'], pool['token1_symbol'], pool['token0_symbol'])
            pool_map[key1] = pool
            pool_map[key2] = pool  # để cover swap order

        # Update nfts
        for nft in nfts:
            chain = nft.get('chain') or ''
            t0 = nft.get('token0_symbol') or ''
            t1 = nft.get('token1_symbol') or ''
            pool = pool_map.get((chain, t0, t1))
            if pool:
                nft.update({
                    'pool_address': pool['pool_address'],
                    'token0_address': pool['token0_address'],
                    'token1_address': pool['token1_address'],
                    'fee': pool.get('fee'),
                    'pool_info': pool
                })

        return nfts
    finally:
        cursor.close()
        conn.close()

def fetch_latest_summary_by_token(wallet_address, include_closed=False, start_date=None, end_date=None):
    # chuẩn hóa ngày
    start_date = to_datetime_safe(start_date)
    end_date = to_datetime_safe(end_date)
    if end_date:
        end_date += timedelta(days=1)  # make end_date inclusive

    # Lấy raw latest NFT per nft_id (function bạn đã có)
    nfts = fetch_latest_nft_by_wallet(wallet_address) or []

    # Lọc theo ngày (nếu user truyền)
    if start_date or end_date:
        filtered = []
        for nft in nfts:
            ca = nft.get("created_at")
            if ca is None:
                # Nếu có filter ngày mà record không có created_at -> bỏ
                continue
            ca_dt = to_datetime_safe(ca)
            if start_date and ca_dt < start_date:
                continue
            if end_date and ca_dt >= end_date:
                continue
            filtered.append(nft)
        nfts = filtered

    summary = {}
    total_reward = 0.0

    for nft in nfts:
        # bỏ closed nếu người gọi yêu cầu
        if not include_closed and nft.get("status") in ("Closed", "Burned"):
            continue

        # token0
        token0 = nft.get("token0_symbol")
        if token0:
            initial = float(nft.get("initial_token0_amount") or 0)
            current = float(nft.get("current_token0_amount") or 0)
            delta = current - initial
            price0 = float(nft.get("price_token0") or 0)
            delta_usd = delta * price0
            fee = float(nft.get("unclaimed_fee_token0") or 0.0)

            s = summary.setdefault(token0, {"token": token0, "initial": 0.0, "current": 0.0, "delta": 0.0, "delta_usd": 0.0, "fee": 0.0})
            s["initial"] += initial
            s["current"] += current
            s["delta"] += delta
            s["delta_usd"] += delta_usd
            s["fee"] += fee

        # token1
        token1 = nft.get("token1_symbol")
        if token1:
            initial = float(nft.get("initial_token1_amount") or 0)
            current = float(nft.get("current_token1_amount") or 0)
            delta = current - initial
            price1 = float(nft.get("price_token1") or 0)
            delta_usd = delta * price1
            fee = float(nft.get("unclaimed_fee_token1") or 0)

            s = summary.setdefault(token1, {"token": token1, "initial": 0.0, "current": 0.0, "delta": 0.0, "delta_usd": 0.0, "fee": 0.0})
            s["initial"] += initial
            s["current"] += current
            s["delta"] += delta
            s["delta_usd"] += delta_usd
            s["fee"] += fee

        # reward (tổng ví)
        total_reward += float(nft.get("pending_cake") or 0)

    # Gắn reward tổng vào từng token (hoặc bạn có thể trả riêng)
    results = []
    for token, data in summary.items():
        data["reward"] = total_reward
        results.append(data)

    return results

def fetch_latest_summary_by_wallet_and_chain(wallet_address, chain, include_closed=False, start_date=None, end_date=None):
    # Lấy raw NFT mới nhất
    nfts = fetch_latest_nft_by_wallet_and_chain(wallet_address, chain)

    # Filter theo date
    if start_date:
        start_date = to_datetime_safe(start_date)
        nfts = [n for n in nfts if n.get("created_at") and n["created_at"] >= start_date]
    if end_date:
        end_date = to_datetime_safe(end_date) + timedelta(days=1)
        nfts = [n for n in nfts if n.get("created_at") and n["created_at"] < end_date]

    # Loại Closed nếu cần
    if not include_closed:
        nfts = [n for n in nfts if n.get("status") != "Closed" and n.get("status") != "Burned"]

    summary = {}
    total_reward = 0

    for nft in nfts:
        # token0
        token0 = nft.get("token0_symbol")
        if token0:
            initial = nft.get("initial_token0_amount", 0) or 0
            current = nft.get("current_token0_amount", 0) or 0
            delta = current - initial
            delta_usd = delta * (nft.get("price_token0", 0) or 0)
            fee = float(nft.get("unclaimed_fee_token0") or 0)

            s = summary.setdefault(token0, {"token": token0, "initial": 0, "current": 0, "delta": 0, "delta_usd": 0, "fee": 0})
            s["initial"] += initial
            s["current"] += current
            s["delta"] += delta
            s["delta_usd"] += delta_usd
            s["fee"] += fee

        # token1
        token1 = nft.get("token1_symbol")
        if token1:
            initial = nft.get("initial_token1_amount", 0) or 0
            current = nft.get("current_token1_amount", 0) or 0
            delta = current - initial
            delta_usd = delta * (nft.get("price_token1", 0) or 0)
            fee = float(nft.get("unclaimed_fee_token1") or 0)

            s = summary.setdefault(token1, {"token": token1, "initial": 0, "current": 0, "delta": 0, "delta_usd": 0, "fee": 0})
            s["initial"] += initial
            s["current"] += current
            s["delta"] += delta
            s["delta_usd"] += delta_usd
            s["fee"] += fee

        # reward
        total_reward += nft.get("pending_cake", 0) or 0

    # Gắn reward vào từng token
    results = []
    for token, data in summary.items():
        data["reward"] = total_reward
        results.append(data)

    return results

def get_futures_positions_binance_data_by_wallet(waller_address):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT p1.*
            FROM futures_positions_binance p1
            INNER JOIN (
                SELECT wallet_id, MAX(created_at) AS max_created_at
                FROM futures_positions_binance
                GROUP BY wallet_id
            ) p2 ON p1.wallet_id = p2.wallet_id AND p1.created_at = p2.max_created_at
            WHERE p1.wallet_id = %s
        """
        cursor.execute(query, (waller_address,))

        results = cursor.fetchall()
        return results

    except mysql.connector.Error as e:
        print(f"Error fetching all binance data: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_futures_orders_binance_data_by_wallet(waller_address):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT o1.* 
            FROM futures_orders_binance o1
             INNER JOIN (
                SELECT wallet_id, MAX(created_at) AS max_created_at 
                FROM futures_positions_binance
                GROUP BY wallet_id
            ) o2 ON o1.wallet_id = o2.wallet_id AND o1.created_at = o2.max_created_at
            WHERE 01.wallet_id = %s
        """
        cursor.execute(query, (waller_address,))

        results = cursor.fetchall()
        return results

    except mysql.connector.Error as e:
        print(f"Error fetching all binance data: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_latest_total_pending_cake_by_wallet(wallet_address, start_date=None, end_date=None):
    """
    Tổng pending_cake từ tất cả NFT mới nhất trong ví (không Burned, không blacklist).
    Lọc theo start_date / end_date nếu có.
    """
    nfts = fetch_latest_nft_by_wallet(wallet_address)
    total_reward = 0

    for nft in nfts:
        created_at = nft.get("created_at")

        # parse created_at nếu là string
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except Exception:
                created_at = None

        # filter theo date
        if start_date and created_at and created_at < start_date:
            continue
        if end_date and created_at and created_at >= (end_date + timedelta(days=1)):
            continue

        total_reward += nft.get("pending_cake", 0) or 0

    return total_reward

def get_latest_total_pending_cake_by_wallet_and_chain(wallet_address, chain, start_date=None, end_date=None):
    """
    Tổng pending_cake từ tất cả NFT mới nhất trong ví theo chain (không Burned, không blacklist).
    Lọc theo start_date / end_date nếu có.
    """
    nfts = fetch_latest_nft_by_wallet_and_chain(wallet_address, chain)
    total_reward = 0

    for nft in nfts:
        created_at = nft.get("created_at")

        # parse created_at nếu là string
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except Exception:
                created_at = None

        # filter theo date
        if start_date and created_at and created_at < start_date:
            continue
        if end_date and created_at and created_at >= (end_date + timedelta(days=1)):
            continue

        total_reward += nft.get("pending_cake", 0) or 0

    return total_reward

def fetch_nft_history_by_id(chain, nft_id, limit=30, offset=0, npm_address=None):
    try:
        nft_id = nft_id.strip()
        limit = int(limit)
        offset = int(offset)
        if limit < 0 or offset < 0:
            raise ValueError("Parameters must be non-negative integers.")

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # Thêm JOIN với wallet_nft_summary để lấy thông tin vốn ròng và lãi đã chốt
        identity_join = """
            t1.wallet_address = s.wallet_address
            AND t1.chain = s.chain
            AND t1.type_dex = s.type_dex
            AND t1.nft_id = s.nft_id
            AND t1.npm_address = s.npm_address
        """
        npm_filter = ""
        params = [nft_id, chain]
        if npm_address is not None:
            npm_value = npm_address or ""
            npm_filter = """
            AND (
                COALESCE(t1.npm_address, '') = %s
                OR (
                    t1.type_dex = 'aerodrome'
                    AND COALESCE(t1.npm_address, '') = ''
                    AND COALESCE(t1.pool_address, '') <> ''
                    AND EXISTS (
                        SELECT 1
                        FROM wallet_nft_position m
                        WHERE m.wallet_address = t1.wallet_address
                          AND m.chain = t1.chain
                          AND m.type_dex = t1.type_dex
                          AND m.nft_id = t1.nft_id
                          AND COALESCE(m.pool_address, '') = COALESCE(t1.pool_address, '')
                          AND COALESCE(m.npm_address, '') = %s
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM wallet_nft_position m_other
                        WHERE m_other.wallet_address = t1.wallet_address
                          AND m_other.chain = t1.chain
                          AND m_other.type_dex = t1.type_dex
                          AND m_other.nft_id = t1.nft_id
                          AND COALESCE(m_other.pool_address, '') = COALESCE(t1.pool_address, '')
                          AND COALESCE(m_other.npm_address, '') <> ''
                          AND COALESCE(m_other.npm_address, '') <> %s
                    )
                )
            )
            """
            params.extend([npm_value, npm_value, npm_value])

        query = f"""
            SELECT t1.*,
                COALESCE(s.net_invested_capital, 0) AS net_invested_capital, 
                COALESCE(s.total_claimed_fee0, 0) AS total_claimed_fee0, 
                COALESCE(s.total_claimed_fee1, 0) AS total_claimed_fee1, 
                COALESCE(s.total_claimed_reward, 0) AS total_claimed_reward,
                COALESCE(s.total_claimed_fee_usd, 0) AS total_claimed_fee_usd,
                COALESCE(s.total_claimed_reward_usd, 0) AS total_claimed_reward_usd,
                COALESCE(s.total_cash_injected, 0) AS total_cash_injected,
                COALESCE(t1.pnl_value_usd, 0) AS pnl_value_usd,
                COALESCE(t1.pnl_value_base, 0) AS pnl_value_base,
                s.base_symbol,
                COALESCE(t1.total_active_staked_usd, 0) AS total_active_staked_usd,
                COALESCE(t1.total_pool_liquidity_usd, 0) AS total_pool_liquidity_usd,
                COALESCE(p_evm.alloc_point, 0) AS alloc_point_evm,
                COALESCE(p_evm.fee, 0) AS fee_evm,
                COALESCE(p_evm.cake_per_day, 0) AS cake_per_day_evm,
                COALESCE(p_sol.fee, 0) AS fee_sol,
                COALESCE(p_sol.weekly_rewards, 0) AS weekly_rewards,
                COALESCE(p_aero_epoch.reward_per_day, 0) AS reward_per_day_aero,
                COALESCE(p_aero_info.tick_spacing, 0) AS tick_spacing_aero,
                COALESCE(p_sol.token0_decimals, 0) AS token0_decimals_sol,
                COALESCE(p_sol.token1_decimals, 0) AS token1_decimals_sol,
                CASE
                    WHEN t1.type_dex = 'aerodrome' THEN COALESCE(p_aero_info.token0_decimals, 0)
                    ELSE COALESCE(p_evm.token0_decimals, 0)
                END AS token0_decimals_evm,
                CASE
                    WHEN t1.type_dex = 'aerodrome' THEN COALESCE(p_aero_info.token1_decimals, 0)
                    ELSE COALESCE(p_evm.token1_decimals, 0)
                END AS token1_decimals_evm
            FROM wallet_nft_position AS t1
            LEFT JOIN wallet_nft_summary AS s ON {identity_join}
            LEFT JOIN pool_sol_info AS p_sol
                ON t1.chain = 'SOL' AND t1.pool_address = p_sol.pool_account
            LEFT JOIN pool_info p_evm
                ON t1.chain != 'SOL' AND t1.pool_address = p_evm.pool_address AND t1.chain = p_evm.chain
            LEFT JOIN aerodrome_pool_epoch_state p_aero_epoch
                ON t1.chain != 'SOL' AND t1.pool_address = p_aero_epoch.pool_address AND t1.chain = p_aero_epoch.chain
            LEFT JOIN aerodrome_pool_info p_aero_info
                ON t1.chain != 'SOL' AND t1.pool_address = p_aero_info.pool_address AND t1.chain = p_aero_info.chain
            WHERE t1.nft_id = %s AND t1.chain = %s
            {npm_filter}
            ORDER BY t1.created_at DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        cursor.execute(query, tuple(params))
        return cursor.fetchall()

    except Exception as e:
        print(f"❌ Error fetching history for NFT ID {nft_id}: {e}")
        return []
    finally:
        if 'cursor' in locals() and cursor: cursor.close()
        if 'conn' in locals() and conn: conn.close()

def count_nft_history_records_by_id(chain, nft_id, npm_address=None):
    try:
        nft_id = nft_id.strip()
        if not nft_id:
            raise ValueError("NFT ID must be a non-empty string.")

        conn = get_connection()
        cursor = conn.cursor()

        query = "SELECT COUNT(*) FROM wallet_nft_position WHERE nft_id = %s AND chain = %s"
        params = [nft_id, chain]
        if npm_address is not None:
            npm_value = npm_address or ""
            query += """
                AND (
                    COALESCE(npm_address, '') = %s
                    OR (
                        type_dex = 'aerodrome'
                        AND COALESCE(npm_address, '') = ''
                        AND COALESCE(pool_address, '') <> ''
                        AND EXISTS (
                            SELECT 1
                            FROM wallet_nft_position m
                            WHERE m.wallet_address = wallet_nft_position.wallet_address
                              AND m.chain = wallet_nft_position.chain
                              AND m.type_dex = wallet_nft_position.type_dex
                              AND m.nft_id = wallet_nft_position.nft_id
                              AND COALESCE(m.pool_address, '') = COALESCE(wallet_nft_position.pool_address, '')
                              AND COALESCE(m.npm_address, '') = %s
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM wallet_nft_position m_other
                            WHERE m_other.wallet_address = wallet_nft_position.wallet_address
                              AND m_other.chain = wallet_nft_position.chain
                              AND m_other.type_dex = wallet_nft_position.type_dex
                              AND m_other.nft_id = wallet_nft_position.nft_id
                              AND COALESCE(m_other.pool_address, '') = COALESCE(wallet_nft_position.pool_address, '')
                              AND COALESCE(m_other.npm_address, '') <> ''
                              AND COALESCE(m_other.npm_address, '') <> %s
                        )
                    )
                )
            """
            params.extend([npm_value, npm_value, npm_value])
        cursor.execute(query, tuple(params))
        count = cursor.fetchone()[0]
        return count

    except mysql.connector.Error as e:
        print(f"Error counting history records for NFT ID {nft_id}: {e}")
        return 0

    except ValueError as ve:
        print(f"Invalid parameter: {ve}")
        return 0

    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

def toggle_blacklist(wallet_address, chain, nft_id, type_dex="", npm_address=""):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        query = """
            SELECT 1 FROM nft_blacklist
            WHERE wallet_address = %s AND chain = %s AND type_dex = %s AND nft_id = %s AND npm_address = %s
        """
        
        cursor.execute(query, (wallet_address, chain, type_dex or "", nft_id, npm_address or ""))
        exists = cursor.fetchone()
        
        if exists:
            query = """
                DELETE FROM nft_blacklist 
                WHERE wallet_address = %s AND chain = %s AND type_dex = %s AND nft_id = %s AND npm_address = %s
            """
            cursor.execute(query, (wallet_address, chain, type_dex or "", nft_id, npm_address or ""))
            message = 'Removed NFT ID: ' + nft_id + ' from Blacklist'
        else:
            query = """
                INSERT INTO nft_blacklist (wallet_address, chain, type_dex, nft_id, npm_address)
                VALUES (%s, %s, %s, %s, %s)
            """
            cursor.execute(query, (wallet_address, chain, type_dex or "", nft_id, npm_address or ""))
            message = f'Added NFT ID: {nft_id} to Blacklist'
            
        conn.commit()
        return {'status': 'success', 'message': message}  # ✅ Trả về dict

    except mysql.connector.Error as e:
        print(f"Error toggling blacklist: {e}")
        return {'status': 'error', 'message': 'Failed to toggle blacklist'}  # ✅ dict thuần

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()
            
def get_blacklist_nft_ids(wallet_address, chain_name, type_dex=None, npm_address=None):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        filters = ["wallet_address = %s", "chain = %s"]
        params = [wallet_address, chain_name]
        if type_dex is not None:
            filters.append("type_dex = %s")
            params.append(type_dex)
        if npm_address is not None:
            if type_dex == "aerodrome":
                filters.append("(npm_address = %s OR npm_address = '')")
            else:
                filters.append("npm_address = %s")
            params.append(npm_address or "")

        query = f"""
            SELECT nft_id 
            FROM nft_blacklist
            WHERE {' AND '.join(filters)}
        """
        
        cursor.execute(query, tuple(params))
        results = cursor.fetchall()
        
        return [row[0] for row in results] if results else []

    except mysql.connector.Error as e:
        print(f"Error fetching blacklist NFT IDs: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()
            
def fetch_blacklist_nft_ids():
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT * FROM nft_blacklist
        """
        cursor.execute(query, )

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
            
def get_pool_info_with_fallback(factory_contract, chain_name, chain_api, token0, token1, fee, rpc_list=None):
    t0, t1 = sorted([token0.lower(), token1.lower()])
    
    if rpc_list is None:
        rpc_list = RPC_BACKUP_LIST.get(chain_name, [])

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # ✅ 1. Check trong DB
        cursor.execute("""
            SELECT * FROM pool_info 
            WHERE token0_address = %s AND token1_address = %s AND fee = %s
        """, (t0, t1, fee))
        result = cursor.fetchone()
        if result:
            print("✅ Pool data found in DB")
            pool_address = Web3.to_checksum_address(result["pool_address"])
            return {
                "chain": result["chain"],
                "pool_address": pool_address,
                "token0_symbol": result["token0_symbol"],
                "token1_symbol": result["token1_symbol"],
                "token0_decimals": result["token0_decimals"],
                "token1_decimals": result["token1_decimals"],
                "fee": result["fee"],
                "alloc_point": result.get("alloc_point", 0),
                "source": "db"
            }

        # ❌ 2. Nếu chưa có → thử gọi contract với RPC chính trước
        print("❌ Pool data not found in DB, calling contract and API")

        pool_address = None
        try:
            pool_address = factory_contract.functions.getPool(token0, token1, fee).call()
        except Exception as e:
            print(f"⚠️ Primary RPC failed getPool({token0}, {token1}, {fee}): {e}")

        # Nếu RPC chính fail → thử fallback RPC
        if not pool_address:
            for rpc in rpc_list:
                try:
                    w3_backup = Web3(Web3.HTTPProvider(rpc))
                    factory_backup = w3_backup.eth.contract(address=factory_contract.address, abi=factory_contract.abi)
                    pool_address = factory_backup.functions.getPool(token0, token1, fee).call()
                    print(f"✅ Success with backup RPC {rpc}")
                    break
                except Exception as e:
                    print(f"⚠️ Retry getPool failed with {rpc}: {e}")
                    continue

        if not pool_address:
            print(f"❌ All RPC failed for getPool({token0}, {token1}, {fee})")
            return None

        # Lấy thêm data từ API
        pool_data = get_data_pool_bsc(chain_api, pool_address)
        token0_symbol = pool_data["token0"]["symbol"]
        token1_symbol = pool_data["token1"]["symbol"]
        token0_decimals = pool_data["token0"]["decimals"]
        token1_decimals = pool_data["token1"]["decimals"]
        alloc_point = 0

        # ✅ Insert vào DB
        insert_query = """
            INSERT INTO pool_info (
                chain, pool_address, token0_address, token1_address,
                token0_symbol, token1_symbol, token0_decimals, token1_decimals, fee, alloc_point
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(insert_query, (
            chain_name, pool_address, t0, t1,
            token0_symbol, token1_symbol,
            int(token0_decimals), int(token1_decimals),
            fee, alloc_point
        ))
        conn.commit()

        return {
            "chain": chain_name,
            "pool_address": pool_address,
            "token0_symbol": token0_symbol, 
            "token1_symbol": token1_symbol,
            "token0_decimals": token0_decimals,
            "token1_decimals": token1_decimals,
            "fee": fee,
            "alloc_point": alloc_point,
            "source": "api"
        }

    except Exception as e:
        print(f"❌ Error in get_pool_info_with_fallback: {e}")
        return None

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_aerodrome_pool_info(chain, token0_address, token1_address, tick_spacing, factory_address=None):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        factory_filter = ""
        params = [chain, token0_address, token1_address, tick_spacing]
        if factory_address:
            factory_filter = "AND p1.factory_address = %s"
            params.append(factory_address)

        query = """
            SELECT p1.*, p2.* 
            FROM aerodrome_pool_info AS p1
            INNER JOIN aerodrome_pool_epoch_state AS p2 
                ON p1.pool_address = p2.pool_address 
                AND p1.chain = p2.chain
            WHERE p1.chain = %s 
            AND p1.token0_address = %s
            AND p1.token1_address = %s 
            AND p1.tick_spacing = %s
            {factory_filter}
            ORDER BY p2.epoch_start DESC
        """.format(factory_filter=factory_filter)
        
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
        if not rows:
            return None
        if not factory_address:
            distinct_pools = {str(row.get("pool_address", "")).lower() for row in rows}
            if len(distinct_pools) > 1:
                print(
                    "Warning: Aerodrome pool lookup without factory_address returned "
                    f"{len(distinct_pools)} pools for {chain} {token0_address}/{token1_address} "
                    f"tick_spacing={tick_spacing}. Using latest row only."
                )
        result = rows[0]
        return result  # dict hoặc None nếu không có
    except mysql.connector.Error as e:
        print(f"Error fetching aerodrome pool info: {e}")
        return None
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_total_alloc_point_each_chain(chain):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT SUM(alloc_point) AS total_alloc_point
            FROM pool_info
            WHERE chain = %s
        """
        cursor.execute(query, (chain,))
        result = cursor.fetchone()

        if result and result['total_alloc_point'] is not None:
            return result['total_alloc_point']
        else:
            return 0

    except mysql.connector.Error as e:
        print(f"Error fetching total alloc point for chain {chain}: {e}")
        return 0
            
def _legacy_get_nft_status_data(wallet_address, chain_name, type_dex):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # 1. NFT Active + Inactive (latest)
        query_active_inactive = """
            SELECT t1.nft_id, t1.status
            FROM wallet_nft_position t1
            JOIN (
                SELECT nft_id, MAX(created_at) AS latest
                FROM wallet_nft_position
                WHERE wallet_address = %s AND chain = %s AND type_dex = %s
                GROUP BY nft_id, COALESCE(npm_address, '')
            ) t2 ON t1.nft_id = t2.nft_id AND t1.created_at = t2.latest
            WHERE t1.wallet_address = %s
                AND t1.chain = %s
                AND t1.type_dex = %s
                AND t1.status IN ('Active', 'Inactive');
        """

        # 2. Closed từ cache
        query_closed_cache = """
            SELECT nft_id
            FROM nft_closed_cache
            WHERE wallet_address = %s AND chain_name = %s AND type_dex = %s AND status = 'Burned';
        """

        # 3. Blacklist
        query_blacklist = """
            SELECT nft_id 
            FROM nft_blacklist
            WHERE wallet_address = %s AND chain = %s;
        """

        # Execute all 3
        cursor.execute(query_active_inactive, (wallet_address, chain_name, type_dex, wallet_address, chain_name, type_dex))
        active_inactive_rows = cursor.fetchall()

        cursor.execute(query_closed_cache, (wallet_address, chain_name, type_dex))
        closed_rows = cursor.fetchall()

        cursor.execute(query_blacklist, (wallet_address, chain_name))
        blacklist_rows = cursor.fetchall()

        # Process
        active_inactive_map = {row['nft_id']: row['status'] for row in active_inactive_rows}
        closed_ids = [row['nft_id'] for row in closed_rows]
        blacklist_ids = [row['nft_id'] for row in blacklist_rows]

        return {
            "active_inactive_map": active_inactive_map,  # {nft_id: status}
            "closed_ids": closed_ids,  # list
            "blacklist_ids": blacklist_ids,  # list
        }

    except mysql.connector.Error as e:
        print(f"Error fetching NFT status data: {e}")
        return None

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def get_nft_status_data(wallet_address, chain_name, type_dex, npm_address=None):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        active_npm_filter = ""
        active_params = [wallet_address, chain_name, type_dex]
        if npm_address is not None:
            active_npm_filter = "AND npm_address = %s"
            active_params.append(npm_address or "")

        query_active_inactive = """
            SELECT
                t1.nft_id,
                t1.status,
                COALESCE(t1.npm_address, '') AS npm_address,
                COALESCE(t1.pool_address, '') AS pool_address,
                COALESCE(p_aero_info.factory_address, '') AS factory_address
            FROM wallet_nft_position t1
            JOIN (
                SELECT nft_id, COALESCE(npm_address, '') AS npm_address, MAX(created_at) AS latest
                FROM wallet_nft_position
                WHERE wallet_address = %s AND chain = %s AND type_dex = %s
                {active_npm_filter}
                GROUP BY nft_id, COALESCE(npm_address, '')
            ) t2 ON t1.nft_id = t2.nft_id
                AND COALESCE(t1.npm_address, '') = t2.npm_address
                AND t1.created_at = t2.latest
            LEFT JOIN aerodrome_pool_info p_aero_info
                ON t1.type_dex = 'aerodrome'
                AND t1.chain = p_aero_info.chain
                AND t1.pool_address = p_aero_info.pool_address
            WHERE t1.wallet_address = %s
                AND t1.chain = %s
                AND t1.type_dex = %s
                {active_outer_npm_filter}
                AND t1.status IN ('Active', 'Inactive');
        """.format(
            active_npm_filter=active_npm_filter,
            active_outer_npm_filter=active_npm_filter,
        )
        active_params = active_params + active_params

        closed_npm_filter = ""
        closed_params = [wallet_address, chain_name, type_dex]
        if npm_address is not None:
            closed_npm_filter = "AND npm_address = %s"
            closed_params.append(npm_address or "")
        query_closed_cache = f"""
            SELECT nft_id, COALESCE(npm_address, '') AS npm_address
            FROM nft_closed_cache
            WHERE wallet_address = %s AND chain_name = %s AND type_dex = %s AND status = 'Burned'
            {closed_npm_filter};
        """

        blacklist_filter = ""
        blacklist_params = [wallet_address, chain_name]
        if type_dex:
            blacklist_filter += " AND (type_dex = %s OR type_dex = '')"
            blacklist_params.append(type_dex)
        if npm_address is not None:
            if type_dex == "aerodrome":
                blacklist_filter += " AND (npm_address = %s OR npm_address = '')"
            else:
                blacklist_filter += " AND npm_address = %s"
            blacklist_params.append(npm_address or "")
        query_blacklist = f"""
            SELECT nft_id, COALESCE(npm_address, '') AS npm_address, COALESCE(type_dex, '') AS type_dex
            FROM nft_blacklist
            WHERE wallet_address = %s AND chain = %s
            {blacklist_filter};
        """

        cursor.execute(query_active_inactive, tuple(active_params))
        active_inactive_rows = cursor.fetchall()

        cursor.execute(query_closed_cache, tuple(closed_params))
        closed_rows = cursor.fetchall()

        cursor.execute(query_blacklist, tuple(blacklist_params))
        blacklist_rows = cursor.fetchall()

        active_inactive_map = {row['nft_id']: row['status'] for row in active_inactive_rows}
        active_inactive_identity_map = {
            (str(row['nft_id']), (row.get('npm_address') or '').lower()): row['status']
            for row in active_inactive_rows
        }
        active_inactive_identity_details = {
            (str(row['nft_id']), (row.get('npm_address') or '').lower()): {
                "status": row.get("status"),
                "pool_address": row.get("pool_address") or "",
                "factory_address": row.get("factory_address") or "",
            }
            for row in active_inactive_rows
        }
        closed_ids = [row['nft_id'] for row in closed_rows]
        closed_identities = {
            (str(row['nft_id']), (row.get('npm_address') or '').lower())
            for row in closed_rows
        }
        blacklist_ids = [row['nft_id'] for row in blacklist_rows]
        blacklist_identities = {
            (str(row['nft_id']), (row.get('npm_address') or '').lower())
            for row in blacklist_rows
        }

        return {
            "active_inactive_map": active_inactive_map,
            "active_inactive_identity_map": active_inactive_identity_map,
            "active_inactive_identity_details": active_inactive_identity_details,
            "closed_ids": closed_ids,
            "closed_identities": closed_identities,
            "blacklist_ids": blacklist_ids,
            "blacklist_identities": blacklist_identities,
        }

    except mysql.connector.Error as e:
        print(f"Error fetching NFT status data: {e}")
        return None

    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

# Fetch all pool info from the database
def fetch_all_pool_info(chain_name=None):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        base_query = """
            SELECT p.*,
                dp.status             AS copy_bot_status,
                dp.selection_source   AS copy_bot_source
            FROM pool_info p
            LEFT JOIN detected_pools dp
                ON p.chain = dp.chain AND p.pool_address = dp.pool_address
                AND dp.selection_source = 'COPY_BOT'
            WHERE p.alloc_point > 0
        """

        if chain_name is not None:
            cursor.execute(base_query + " AND p.chain = %s ORDER BY p.chain, p.pid DESC", (chain_name,))
        else:
            cursor.execute(base_query + " ORDER BY p.chain, p.pid DESC")

        results = cursor.fetchall()
        # Thêm flag boolean để template dùng dễ hơn
        for r in results:
            r['is_copy_bot_selected'] = (
                r.get('copy_bot_source') == 'COPY_BOT'
                and r.get('copy_bot_status') not in (None, 'REJECTED')
            )
        return results

    except mysql.connector.Error as e:
        print(f"Error fetching all pool info: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

# Fetch all pool sol info from the database
def fetch_all_pool_sol_info(chain_name=None):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        if chain_name is not None:
            query = """
                SELECT * FROM pool_sol_info
                ORDER BY reward_state DESC
            """
            cursor.execute(query)
            results = cursor.fetchall()
            return results
        else:
            query = """
                SELECT * FROM pool_sol_info
                ORDER BY reward_state DESC
            """
            cursor.execute(query)
            results = cursor.fetchall()

            return results

    except mysql.connector.Error as e:
        print(f"Error fetching all pool info: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_pool_sol_info(pool_account):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT *
            FROM pool_sol_info
            WHERE pool_account = %s
        """
        cursor.execute(query, (pool_account,))
        result = cursor.fetchone()
        return result  # dict hoặc None nếu không có

    except mysql.connector.Error as e:
        print(f"Error fetching pool info: {e}")
        return None

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_pool_info_evm(chain, pool_address):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT *
            FROM pool_info
            WHERE chain = %s AND pool_address = %s
        """
        cursor.execute(query, (chain, pool_address))
        result = cursor.fetchone()
        return result

    except mysql.connector.Error as e:
        print(f"Error fetching EVM pool info: {e}")
        return None

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_total_cake_per_day_each_chain():
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT chain, SUM(cake_per_day) AS total_cake_per_day
            FROM pool_info
            GROUP BY chain
        """
        cursor.execute(query)
        results = cursor.fetchall()
        return results

    except mysql.connector.Error as e:
        print(f"Error fetching pool info: {e}")
        return None

    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

def get_total_aero_per_day_each_chain():
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT chain, SUM(reward_per_day) AS total_aero_per_day
            FROM aerodrome_pool_epoch_state
            GROUP BY chain
        """
        cursor.execute(query)
        results = cursor.fetchall()
        return results

    except mysql.connector.Error as e:
        print(f"Error fetching pool info: {e}")
        return None

    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

def get_total_cake_per_day_on_chain(chain):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT SUM(cake_per_day) AS total_cake_per_day
            FROM pool_info
            WHERE chain = %s
        """
        cursor.execute(query, (chain,))
        result = cursor.fetchone()
        
        return result["total_cake_per_day"] if result and result["total_cake_per_day"] else 0

    except mysql.connector.Error as e:
        print(f"Error fetching pool info: {e}")
        return None

    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

def get_total_weekly_rewards_sol():
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT SUM(weekly_rewards) AS total_weekly_rewards
            FROM pool_sol_info
        """
        cursor.execute(query)
        result = cursor.fetchone()  # chỉ 1 row
        return result["total_weekly_rewards"] if result else 0

    except mysql.connector.Error as e:
        print(f"Error fetching pool info: {e}")
        return 0

    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

def get_weekly_reward_per_pool(chain, pool_account):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT weekly_rewards
            FROM pool_sol_info
            WHERE chain = %s AND pool_account = %s
        """
        cursor.execute(query, (chain, pool_account))
        result = cursor.fetchone()
        if result:
            return result["weekly_rewards"]
        return None

    except mysql.connector.Error as e:
        print(f"Error fetching pool info: {e}")
        return None

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()
            
def get_latest_nft_id_sol_from_db(chain: str, wallet_address: str):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT t1.nft_id
            FROM wallet_nft_position t1
            INNER JOIN (
                SELECT nft_id, MAX(created_at) AS max_created_at
                FROM wallet_nft_position
                WHERE chain = %s
                GROUP BY nft_id, COALESCE(npm_address, '')
            ) t2 
                ON t1.nft_id = t2.nft_id 
                AND t1.created_at = t2.max_created_at
            LEFT JOIN nft_blacklist b 
                ON t1.wallet_address = b.wallet_address 
                AND t1.chain = b.chain 
                AND t1.nft_id = b.nft_id
            WHERE t1.chain = %s 
              AND t1.wallet_address = %s
              AND b.nft_id IS NULL
        """
        cursor.execute(query, (chain, chain, wallet_address))
        results = cursor.fetchall()
        
        return [row["nft_id"] for row in results]

    except mysql.connector.Error as e:
        print(f"❌ Error fetching NFT IDs: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()
            
def get_all_burned_nfts_sol(wallet_address, chain_name):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT nft_id
            FROM nft_closed_cache
            WHERE chain_name = %s AND wallet_address = %s AND status = 'Burned'
        """
        cursor.execute(query, (chain_name, wallet_address))
        results = cursor.fetchall()
        
        burned_nft_ids = [row["nft_id"] for row in results]
        return burned_nft_ids

    except mysql.connector.Error as e:
        print(f"❌ Error fetching burned NFT data: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_latest_closed_nft_ids(wallet_address: str, chain: str):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT t1.nft_id
            FROM wallet_nft_position t1
            INNER JOIN (
                SELECT nft_id, MAX(created_at) AS max_created_at
                FROM wallet_nft_position
                WHERE chain = %s AND status = 'Closed'
                GROUP BY nft_id, COALESCE(npm_address, '')
            ) t2
                ON t1.nft_id = t2.nft_id
                AND t1.created_at = t2.max_created_at
            LEFT JOIN nft_blacklist b
                ON t1.wallet_address = b.wallet_address
                AND t1.chain = b.chain
                AND t1.nft_id = b.nft_id
            WHERE t1.chain = %s
              AND t1.wallet_address = %s
              AND t1.status = 'Closed'
              AND b.nft_id IS NULL
        """
        cursor.execute(query, (chain, chain, wallet_address))
        results = cursor.fetchall()

        return [row["nft_id"] for row in results]

    except mysql.connector.Error as e:
        print(f"❌ Error fetching Closed NFT IDs: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_nft_initial_amount_from_db(nft_id, chain, wallet_address, type_dex=None, npm_address="", pool_address=""):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        filters = [
            "nft_id = %s",
            "chain = %s",
            "wallet_address = %s",
            "initial_token0_amount > 0",
            "initial_token1_amount > 0",
        ]
        params = [nft_id, chain, wallet_address]
        if type_dex is not None:
            filters.append("type_dex = %s")
            params.append(type_dex)
        if type_dex is not None or npm_address:
            filters.append("npm_address = %s")
            params.append(npm_address or "")
        if pool_address:
            filters.append("pool_address = %s")
            params.append(pool_address)
        query = f"""
            SELECT initial_token0_amount, initial_token1_amount
            FROM wallet_nft_position
            WHERE {' AND '.join(filters)}
            ORDER BY created_at DESC
            LIMIT 1
        """
        cursor.execute(query, tuple(params))
        result = cursor.fetchone()

        if (
            not result
            and type_dex == "aerodrome"
            and npm_address
            and pool_address
        ):
            legacy_query = """
                SELECT initial_token0_amount, initial_token1_amount
                FROM wallet_nft_position
                WHERE nft_id = %s
                  AND chain = %s
                  AND wallet_address = %s
                  AND type_dex = 'aerodrome'
                  AND COALESCE(npm_address, '') = ''
                  AND pool_address = %s
                  AND initial_token0_amount > 0
                  AND initial_token1_amount > 0
                ORDER BY created_at DESC
                LIMIT 1
            """
            cursor.execute(legacy_query, (nft_id, chain, wallet_address, pool_address))
            result = cursor.fetchone()

        if result:
            return (
                result["initial_token0_amount"],
                result["initial_token1_amount"]
            )
        return None

    except mysql.connector.Error as e:
        print(f"❌ Error fetching initial amount for NFT {nft_id}: {e}")
        return None

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()
            
def toggle_stake_track_api(chain, pool_address):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT is_stake_tracked 
            FROM pool_info 
            WHERE chain = %s AND pool_address = %s
        """, (chain, pool_address))
        pool = cursor.fetchone()

        if not pool:
            return jsonify({'success': False, 'message': 'Pool not found'}), 404

        new_state = not bool(pool['is_stake_tracked'])

        cursor.execute("""
            UPDATE pool_info 
            SET is_stake_tracked = %s
            WHERE chain = %s AND pool_address = %s
        """, (new_state, chain, pool_address))
        conn.commit()

        print(f"✅ Updated is_stake_tracked for {chain}:{pool_address} → {new_state}")

        return jsonify({'success': True, 'is_stake_tracked': new_state})
    except Exception as e:
        print("❌ Error:", e)
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            cursor.close()
            conn.close()
        except:
            pass

### AERODROME DEX ###
def toggle_stake_track_api_aerodrome(chain, pool_address):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT is_stake_tracked
            FROM aerodrome_pool_info
            WHERE chain = %s AND pool_address = %s
        """, (chain, pool_address))
        pool = cursor.fetchone()

        if not pool:
            return jsonify({'success': False, 'message': 'Pool not found'}), 404

        new_state = not bool(pool['is_stake_tracked'])

        cursor.execute("""
            UPDATE aerodrome_pool_info 
            SET is_stake_tracked = %s
            WHERE chain = %s AND pool_address = %s
        """, (new_state, chain, pool_address))
        conn.commit()

        print(f"✅ Updated is_stake_tracked for {chain}:{pool_address} → {new_state}")

        return jsonify({'success': True, 'is_stake_tracked': new_state})
    except Exception as e:
        print("❌ Error:", e)
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            cursor.close()
            conn.close()
        except:
            pass


# ─────────────────────────────────────────────────
# Copy-Bot Pool Selection API
# ─────────────────────────────────────────────────

_SLOT0_SIG = "slot0()(uint160,int24,uint16,uint16,uint16,uint32,bool)"

_TICK_SPACING_MAP = {100: 1, 500: 10, 2500: 50, 3000: 60, 10000: 200}


def _get_slot0(chain: str, pool_address: str) -> dict | None:
    """Gọi slot0() trực tiếp lên chain, trả về {"tick": int, "sqrt_price_x96": str} hoặc None."""
    rpcs = RPC_BACKUP_LIST.get(chain, [])
    for rpc_url in rpcs:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 8}))
            if not w3.is_connected():
                continue
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(pool_address),
                abi=[{
                    "name": "slot0", "type": "function", "inputs": [],
                    "outputs": [
                        {"type": "uint160", "name": "sqrtPriceX96"},
                        {"type": "int24",   "name": "tick"},
                        {"type": "uint16",  "name": "observationIndex"},
                        {"type": "uint16",  "name": "observationCardinality"},
                        {"type": "uint16",  "name": "observationCardinalityNext"},
                        {"type": "uint32",  "name": "feeProtocol"},
                        {"type": "bool",    "name": "unlocked"},
                    ],
                    "stateMutability": "view",
                }]
            )
            result = contract.functions.slot0().call()
            return {"sqrt_price_x96": str(result[0]), "tick": result[1]}
        except Exception as e:
            print(f"[copy_bot] slot0 RPC error ({rpc_url[:30]}...): {e}")
            continue
    return None


def toggle_copy_bot_api(chain: str, pool_address: str):
    """
    Toggle trạng thái COPY_BOT cho một pool.
    - Nếu chưa chọn: fetch slot0 → upsert detected_pools với status=APPROVED, selection_source=COPY_BOT
    - Nếu đã chọn (status != REJECTED): set status=REJECTED (deselect)
    Returns JSON response.
    """
    if not chain or not pool_address:
        return jsonify({'success': False, 'message': 'Missing parameters'}), 400

    pool_address = pool_address.lower()

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # Lấy thông tin pool từ pool_info
        cursor.execute("""
            SELECT id, chain, pool_address, pid, fee,
                   token0_address, token1_address,
                   token0_symbol, token1_symbol,
                   token0_decimals, token1_decimals,
                   alloc_point, cake_per_day,
                   total_value_lock, total_staked_liquidity,
                   total_inactive_staked_liquidity
            FROM pool_info
            WHERE chain = %s AND pool_address = %s
        """, (chain, pool_address))
        pool = cursor.fetchone()

        if not pool:
            return jsonify({'success': False, 'message': 'Pool not found in pool_info'}), 404

        # Kiểm tra trạng thái hiện tại trong detected_pools
        cursor.execute("""
            SELECT status, selection_source
            FROM detected_pools
            WHERE chain = %s AND pool_address = %s
        """, (chain, pool_address))
        existing = cursor.fetchone()

        is_currently_selected = (
            existing is not None
            and existing.get('selection_source') == 'COPY_BOT'
            and existing.get('status') not in (None, 'REJECTED')
        )

        if is_currently_selected:
            # Deselect
            cursor.execute("""
                UPDATE detected_pools
                SET status = 'REJECTED', reject_reason = 'user_deselected',
                    last_analyzed_at = NOW()
                WHERE chain = %s AND pool_address = %s
            """, (chain, pool_address))
            conn.commit()
            print(f"✅ [copy_bot] Deselected {chain}:{pool_address}")
            return jsonify({'success': True, 'is_copy_bot_selected': False, 'action': 'deselected'})

        # Select: cần slot0 để lấy tick_current và sqrt_price_x96
        slot0 = _get_slot0(chain, pool_address)
        if not slot0:
            return jsonify({
                'success': False,
                'message': f'Cannot fetch slot0 from RPC for {chain}:{pool_address}'
            }), 500

        fee = pool.get('fee') or 500
        tick_spacing = _TICK_SPACING_MAP.get(fee)
        if tick_spacing is None:
            return jsonify({
                'success': False,
                'message': f'Unsupported fee tier {fee} — add to TICK_SPACING_MAP first'
            }), 400

        inactive_ratio = 0.0
        total_staked = pool.get('total_staked_liquidity') or 0
        total_inactive = pool.get('total_inactive_staked_liquidity') or 0
        if total_staked > 0:
            inactive_ratio = min(1.0, max(0.0, total_inactive / total_staked))

        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute("""
            INSERT INTO detected_pools (
                chain, pool_address, pool_info_id, pid,
                token0_address, token1_address,
                token0_symbol, token1_symbol,
                token0_decimals, token1_decimals,
                fee, tick_spacing,
                alloc_point, cake_per_day,
                total_staked_liquidity_usd, inactive_ratio,
                tick_current, sqrt_price_x96,
                pool_type, zombie_score,
                status, selection_source, reject_reason,
                last_analyzed_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                'UNKNOWN', 1.0,
                'APPROVED', 'COPY_BOT', NULL,
                %s
            )
            ON DUPLICATE KEY UPDATE
                pid                       = VALUES(pid),
                pool_info_id              = VALUES(pool_info_id),
                tick_current              = VALUES(tick_current),
                sqrt_price_x96            = VALUES(sqrt_price_x96),
                alloc_point               = VALUES(alloc_point),
                cake_per_day              = VALUES(cake_per_day),
                total_staked_liquidity_usd = VALUES(total_staked_liquidity_usd),
                inactive_ratio            = VALUES(inactive_ratio),
                tick_spacing              = VALUES(tick_spacing),
                status                    = 'APPROVED',
                selection_source          = 'COPY_BOT',
                reject_reason             = NULL,
                zombie_score              = 1.0,
                last_analyzed_at          = VALUES(last_analyzed_at)
        """, (
            chain, pool_address, pool['id'], pool.get('pid'),
            pool.get('token0_address'), pool.get('token1_address'),
            pool.get('token0_symbol'), pool.get('token1_symbol'),
            pool.get('token0_decimals'), pool.get('token1_decimals'),
            fee, tick_spacing,
            pool.get('alloc_point'), pool.get('cake_per_day'),
            pool.get('total_value_lock') or 0, inactive_ratio,
            slot0['tick'], slot0['sqrt_price_x96'],
            now_str,
        ))
        conn.commit()

        print(f"✅ [copy_bot] Selected {chain}:{pool_address} tick={slot0['tick']}")
        return jsonify({
            'success': True,
            'is_copy_bot_selected': True,
            'action': 'selected',
            'tick_current': slot0['tick'],
        })

    except Exception as e:
        print(f"❌ [copy_bot] Error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        try:
            cursor.close()
            conn.close()
        except:
            pass


def fetch_all_pool_info_aerodrome_db(chain_name="BAS"):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT * FROM aerodrome_pool_info AS p1
            INNER JOIN aerodrome_pool_epoch_state AS p2
            ON p1.chain = p2.chain 
            AND p1.pool_address = p2.pool_address
            WHERE p2.farm_active = 1 AND p2.chain = %s
            ORDER BY p2.chain DESC
        """
        cursor.execute(query, (chain_name,))
        results = cursor.fetchall()
        return results

    except mysql.connector.Error as e:
        print(f"❌ Error fetching pool info: {e}")
        return []

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()
            
def get_rewards_per_second_of_aerodrome_pool(pool_address, chain):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT reward_per_day, reward_decimals
            FROM aerodrome_pool_epoch_state
            WHERE pool_address = %s AND chain = %s
        """
        cursor.execute(query, (pool_address, chain))
        result = cursor.fetchone()
        if result:
            return result["reward_per_day"], result["reward_decimals"]
        return None

    except mysql.connector.Error as e:
        print(f"❌ Error fetching rewards per second: {e}")
        return None

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()
            
def safe_map_nft_data(data):
    if isinstance(data, dict):
        mapped = dict(data)
        mapped.setdefault("npm_address", "")
        return mapped

    # Hàm hỗ trợ lấy index an toàn
    def get_idx(lst, idx, default=0):
        try:
            return lst[idx] if lst[idx] is not None else default
        except IndexError:
            return default

    return {
        'wallet_address': get_idx(data, 0, ""),
        'chain':          get_idx(data, 1, ""),
        'nft_id':         get_idx(data, 2, 0),
        'token0_symbol':  get_idx(data, 3, ""),
        'token1_symbol':  get_idx(data, 4, ""),
        'pool_address':   get_idx(data, 5, ""),
        'price_token0':   get_idx(data, 6, 0),
        'price_token1':   get_idx(data, 7, 0),
        'status':         get_idx(data, 8, "Active"),
        'initial_token0_amount': get_idx(data, 10, 0),
        'initial_token1_amount': get_idx(data, 11, 0),
        'initial_total_value':   get_idx(data, 12, 0),
        'current_token0_amount': get_idx(data, 13, 0),
        'current_token1_amount': get_idx(data, 14, 0),
        'current_total_value':   get_idx(data, 15, 0),
        'unclaimed_fee_token0':  get_idx(data, 18, 0),
        'unclaimed_fee_token1':  get_idx(data, 19, 0),
        'pending_cake':          get_idx(data, 23, 0),
        'reward_price':          get_idx(data, 24, 0),
        'lower_price':           get_idx(data, 34, 0),
        'upper_price':           get_idx(data, 35, 0),
        'current_price':         get_idx(data, 36, 0),
        'type_dex':              get_idx(data, 37, ""),
        'npm_address':           get_idx(data, 40, "")
    }

def calculate_value_in_base_by_pool_ratio(token0_amount, token1_amount, pool_ratio, base_type):
    """
    Quy đổi LP value sang base token dùng pool ratio.
    """
    if base_type == 'token1':
        return (token0_amount * pool_ratio) + token1_amount
    elif base_type == 'token0':
        if pool_ratio > 0:
            return token0_amount + (token1_amount / pool_ratio)
        return token0_amount
    return 0

_POOL_DECIMALS_CACHE = {}

def get_pool_decimals(cursor, chain, pool_address):
    """Lấy token decimals từ DB (có cache in-memory)"""
    if not pool_address:
        return None, None
        
    cache_key = f"{chain}_{pool_address}"
    if cache_key in _POOL_DECIMALS_CACHE:
        return _POOL_DECIMALS_CACHE[cache_key]

    if chain == 'SOL':
        cursor.execute("SELECT token0_decimals, token1_decimals FROM pool_sol_info WHERE pool_account = %s", (pool_address,))
    else:
        cursor.execute("SELECT token0_decimals, token1_decimals FROM pool_info WHERE pool_address = %s AND chain = %s", (pool_address, chain))
        
    row = cursor.fetchone()
    if row and row.get('token0_decimals') is not None and row.get('token1_decimals') is not None:
        dec = (row['token0_decimals'], row['token1_decimals'])
        _POOL_DECIMALS_CACHE[cache_key] = dec
        return dec
    
    return None, None

import re

def get_base_asset_logic(data_nft):
    # Lấy symbol và đưa về viết hoa
    t0_raw = data_nft.get('token0_symbol', '').upper().strip()
    t1_raw = data_nft.get('token1_symbol', '').upper().strip()
    chain = data_nft.get('chain', '').upper()

    def normalize_token(symbol):
        if not symbol: return ""
        
        # 1. Xử lý các ký tự đặc biệt (VD: USD₮0 -> USDT0, xBTC -> XBTC)
        # Giữ lại chữ và số để xử lý các biến thể
        clean_symbol = re.sub(r'[^A-Z0-9]', '', symbol)

        # 2. Map các biến thể đặc thù về đồng cơ sở chuẩn
        # Bao gồm cả cbBTC, xBTC, USDG và các loại Wrapped phổ biến
        normalization_map = {
            # Bitcoin variants
            'WBTC': 'BTC', 'BTCB': 'BTC', 'CBBTC': 'BTC', 'XBTC': 'BTC', 'RBTC': 'BTC',
            # Ethereum variants
            'WETH': 'ETH', 'CBETH': 'ETH', 'STETH': 'ETH', 'MSETH': 'ETH',
            # Stablecoins (Gom về 3 nhóm chính hoặc USD)
            'USDT0': 'USDT', 'USD0': 'USDT', 'USDG': 'USDC', 'USDT': 'USDT', 'USDC': 'USDC',
            # Native LSTs
            'WSOL': 'SOL', 'MSOL': 'SOL', 'JITOSOL': 'SOL', 'BNSOL': 'SOL',
            'WBNB': 'BNB', 'WAVAX': 'AVAX', 'WMATIC': 'MATIC'
        }

        # Nếu nằm trong map thì trả về giá trị chuẩn
        if clean_symbol in normalization_map:
            return normalization_map[clean_symbol]
        
        # Xử lý tiền tố 'W' thủ công nếu không nằm trong map (nhưng tránh WIF meme)
        if clean_symbol.startswith('W') and clean_symbol != 'WIF' and len(clean_symbol) > 3:
            return clean_symbol[1:]
            
        return clean_symbol

    t0 = normalize_token(t0_raw)
    t1 = normalize_token(t1_raw)

    # Danh sách ưu tiên theo nền tảng (Dùng Symbol đã chuẩn hóa)
    # Tên Chain nên khớp với data truyền vào (Ví dụ: 'BASE' thay vì 'BAS' nếu đó là giá trị thực)
    priority_tokens = {
        'BNB': ['BNB', 'BTC', 'ETH', 'USDT', 'USDC'],
        'BSC': ['BNB', 'BTC', 'ETH', 'USDT', 'USDC'],
        'BAS': ['ETH', 'USDC', 'USDT', 'BTC'],
        'BASE': ['ETH', 'USDC', 'USDT', 'BTC'],
        'ARB': ['ETH', 'ARB', 'USDC', 'USDT'],
        'SOL': ['SOL', 'USDC', 'USDT', 'JUP'],
        'ETH': ['ETH', 'BTC', 'USDC', 'USDT']
    }
    
    # Lấy danh sách ưu tiên của chain, nếu không có thì dùng list mặc định
    chain_priority = priority_tokens.get(chain, ['ETH', 'BTC', 'USDC', 'USDT'])

    # So sánh với danh sách ưu tiên
    for p_token in chain_priority:
        if t0 == p_token: return 'token0', t0
        if t1 == p_token: return 'token1', t1

    # Nếu cả 2 đều là meme coin (không khớp bất kỳ đồng ưu tiên nào)
    return 'USDC', 'USDC'

from logging_setup import nft_debug_logger as logger

def process_nft_summary(data_input, external_cursor=None, position_id=None, pnl_mode=None):
    if pnl_mode is None:
        try:
            from config import PNL_MODE
            pnl_mode = PNL_MODE
        except ImportError:
            pnl_mode = 'market_price'

    # ==========================================
    # 🛠 CẤU HÌNH DEBUG
    # Nhập ID NFT bạn muốn theo dõi luồng tính toán
    TARGET_DEBUG_ID = "1980664"
    # ==========================================

    if isinstance(data_input, (tuple, list)):
        data_nft = safe_map_nft_data(data_input)
    else:
        data_nft = data_input 

    # Cờ kiểm tra xem có phải NFT cần debug không
    nft_id = str(data_nft.get('nft_id'))
    wallet_address = data_nft.get('wallet_address')
    chain = data_nft.get('chain')
    type_dex = data_nft.get('type_dex') or ""
    npm_address = data_nft.get('npm_address') or ""
    identity_params = (wallet_address, chain, type_dex, nft_id, npm_address)
    is_debug = (nft_id == TARGET_DEBUG_ID)

    if is_debug:
        logger.info(f"\n{'='*20} START DEBUG NFT: {nft_id} {'='*20}")
        logger.info(f"🕒 Time: {data_nft.get('created_at', 'Now')}")

    conn = None
    if external_cursor:
        cursor = external_cursor
    else:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True, buffered=True)
    
    status = data_nft.get('status')

    if status == 'Burned':
        if is_debug: logger.info("🔥 Status is BURNED -> Deleting record.")
        cursor.execute("""
            DELETE FROM wallet_nft_summary
            WHERE wallet_address = %s AND chain = %s AND type_dex = %s AND nft_id = %s AND npm_address = %s
        """, identity_params)
        if conn: conn.commit()
        return None

    pnl_value_usd = 0
    pnl_value_base = 0

    try:
        # 1. Lấy dữ liệu hiện tại từ DB
        cursor.execute("""
            SELECT total_cash_injected, invested_capital_base, 
                   last_lp_token0, last_lp_token1,
                   total_claimed_fee0, total_claimed_fee1, total_claimed_reward,
                   total_claimed_fee_usd, total_claimed_reward_usd,
                   total_claimed_in_base,
                   last_unclaimed_fee0, last_unclaimed_fee1, last_pending_reward
            FROM wallet_nft_summary
            WHERE wallet_address = %s AND chain = %s AND type_dex = %s AND nft_id = %s AND npm_address = %s
        """, identity_params)
        row = cursor.fetchone()

        if is_debug:
            logger.info(f"\n[1] DB STATE (BEFORE):")
            if row:
                logger.info(f"   • Last LP Stored: {row['last_lp_token0']} / {row['last_lp_token1']}")
                logger.info(f"   • Total Cash Injected: ${row['total_cash_injected']}")
                logger.info(f"   • Total Claimed USD: ${row['total_claimed_fee_usd'] + row['total_claimed_reward_usd']}")
            else:
                logger.info(f"   • New Record (Insert)")

        # Ánh xạ giá trị từ data_nft
        p0 = data_nft.get('price_token0', 0) or 0
        p1 = data_nft.get('price_token1', 0) or 0
        p_reward = data_nft.get('reward_price', 0) or 0
        
        # --- NHÓM DATA TĨNH ---
        initial_lp0 = data_nft.get('initial_token0_amount', 0) or 0
        initial_lp1 = data_nft.get('initial_token1_amount', 0) or 0
        total_initial_lp_usd = data_nft.get('initial_total_value', 0) or 0
        
        # --- NHÓM DATA ĐỘNG ---
        current_lp0 = data_nft.get('current_token0_amount', 0) or 0
        current_lp1 = data_nft.get('current_token1_amount', 0) or 0
        total_current_lp_usd = data_nft.get('current_total_value', 0) or 0
        
        u0 = data_nft.get('unclaimed_fee_token0', 0) or 0
        u1 = data_nft.get('unclaimed_fee_token1', 0) or 0
        pending_rew = data_nft.get('pending_cake', 0) or 0
        
        # Xác định đồng cơ sở
        base_type, base_symbol = get_base_asset_logic(data_nft)
        # p_base = p0 if base_type == 'token0' else (p1 if base_type == 'token1' else 0)
        p_base = p0 if base_type == 'token0' else (p1 if base_type == 'token1' else 1.0)
        
        # Xử lý PNL_MODE
        pool_ratio = 0
        if pnl_mode == 'pool_ratio':
            tick = data_nft.get('current_price', 0)
            pool_address = data_nft.get('pool_address')
            chain = data_nft.get('chain')
            
            # Tính toán ratio thực sự từ tick
            if base_symbol == 'USDC' or tick == 0 or not pool_address:
                pnl_mode = 'market_price'
            else:
                try:
                    dec0, dec1 = get_pool_decimals(cursor, chain, pool_address)
                    if dec0 is not None and dec1 is not None:
                        tick = float(tick)
                        # pool_ratio là token1 per token0
                        pool_ratio = (1.0001 ** tick) * (10 ** (dec0 - dec1))
                    else:
                        pnl_mode = 'market_price'
                except Exception as e:
                    logger.warning(f"Error converting tick to ratio: {e}")
                    pnl_mode = 'market_price'

        if is_debug:
            logger.info(f"\n[2] API INPUT DATA:")
            logger.info(f"   • Initial Amount (Principal): {initial_lp0} / {initial_lp1}")
            logger.info(f"   • Current Amount (Market): {current_lp0} / {current_lp1}")
            logger.info(f"   • Prices: P0={p0:.4f}, P1={p1:.4f}, Reward={p_reward:.4f}")
            logger.info(f"   • Current Unclaimed: Fee0={u0}, Fee1={u1}, Reward={pending_rew}")

        if not row:
            if is_debug: logger.info(f"\n[3] ACTION: INSERT NEW RECORD")
            
            # Lưu current vào last_lp cho lần đầu
            insert_lp0 = initial_lp0 if initial_lp0 > 0 else current_lp0
            insert_lp1 = initial_lp1 if initial_lp1 > 0 else current_lp1
            
            if pnl_mode == 'pool_ratio':
                init_invested_base = calculate_value_in_base_by_pool_ratio(
                    insert_lp0, insert_lp1, pool_ratio, base_type)
                init_val_usd = init_invested_base * p_base if p_base > 0 else 0
            else:
                # Fallback nếu initial = 0
                init_val_usd = total_initial_lp_usd if total_initial_lp_usd > 0 else total_current_lp_usd
                init_invested_base = init_val_usd / p_base if p_base > 0 else 0

            if pnl_mode == 'pool_ratio':
                current_lp_base = calculate_value_in_base_by_pool_ratio(current_lp0, current_lp1, pool_ratio, base_type)
                current_unclaimed_fee_base = calculate_value_in_base_by_pool_ratio(u0, u1, pool_ratio, base_type)
                current_reward_base = (pending_rew * p_reward) / p_base if p_base > 0 else 0
                current_val_base = current_lp_base + current_unclaimed_fee_base + current_reward_base
                pnl_value_base = current_val_base - init_invested_base
                pnl_value_usd = pnl_value_base * p_base if p_base > 0 else 0
            else:
                current_unclaimed_usd = (u0 * p0) + (u1 * p1) + (pending_rew * p_reward)
                current_lp_and_unclaimed_usd = total_current_lp_usd + current_unclaimed_usd
                current_val_base = current_lp_and_unclaimed_usd / p_base if p_base > 0 else 0
                pnl_value_usd = current_lp_and_unclaimed_usd - init_val_usd
                pnl_value_base = current_val_base - init_invested_base

            pnl_base_percent = 0
            if init_invested_base > 0:
                pnl_base_percent = (pnl_value_base / init_invested_base) * 100

            if is_debug:
                logger.info(f"   • Initial Capital Set To: ${init_val_usd}")
                logger.info(f"   • Base Capital Set To: {init_invested_base} {base_symbol}")

            sql_insert = """
                INSERT INTO wallet_nft_summary 
                (nft_id, wallet_address, chain, type_dex, npm_address, token0_symbol, token1_symbol, base_symbol,
                 net_invested_capital, total_cash_injected, invested_capital_base,
                 last_lp_token0, last_lp_token1, last_unclaimed_fee0, last_unclaimed_fee1, 
                 last_pending_reward, current_val_base, pnl_base_percent, status, updated_at, pnl_value_usd, pnl_value_base)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s)
            """
            cursor.execute(sql_insert, (
                nft_id, wallet_address, chain, type_dex, npm_address,
                data_nft.get('token0_symbol'), data_nft.get('token1_symbol'), base_symbol,
                init_val_usd, init_val_usd, init_invested_base, 
                insert_lp0, insert_lp1, u0, u1, pending_rew,
                current_val_base, pnl_base_percent, status, pnl_value_usd, pnl_value_base
            ))
        else:
            if is_debug: logger.info(f"\n[3] ACTION: UPDATE EXISTING RECORD")

            # --- 2. Phát hiện nạp/rút vốn (Injected) ---
            # Logic: Dùng biến INITIAL (Principal) trừ đi LAST STORED
            diff_lp0 = initial_lp0 - (row['last_lp_token0'] or 0)
            diff_lp1 = initial_lp1 - (row['last_lp_token1'] or 0)

            cash_change_usd = 0
            token_change_base = 0
            
            has_injection = False
            if abs(diff_lp0) > (row['last_lp_token0'] or 0) * 0.005 or \
               abs(diff_lp1) > (row['last_lp_token1'] or 0) * 0.005:
                has_injection = True
                if pnl_mode == 'pool_ratio':
                    token_change_base = calculate_value_in_base_by_pool_ratio(
                        diff_lp0, diff_lp1, pool_ratio, base_type)
                    cash_change_usd = token_change_base * p_base if p_base > 0 else 0
                else:
                    cash_change_usd = (diff_lp0 * p0) + (diff_lp1 * p1)
                    token_change_base = cash_change_usd / p_base if p_base > 0 else 0

            if is_debug:
                logger.info(f"   [A] CAPITAL INJECTION CHECK:")
                logger.info(f"       • Diff LP: {diff_lp0} / {diff_lp1}")
                logger.info(f"       • Detected Change: {has_injection}")
                if has_injection:
                    logger.info(f"       • Cash Change: ${cash_change_usd}")

            # # --- 3. Tính Claimed (Lãi đã rút) ---
            # diff_f0 = max(0, (row['last_unclaimed_fee0'] or 0) - u0)
            # diff_f1 = max(0, (row['last_unclaimed_fee1'] or 0) - u1)
            # diff_rew = max(0, (row['last_pending_reward'] or 0) - pending_rew)
            
            # --- 3. Tính Claimed (LOGIC MỚI CHO CRONJOB) ---
            
            # Xử lý Fee Token 0
            # Logic cũ: diff_f0 = max(0, row['last_unclaimed_fee0'] - u0) --> SAI nếu cronjob chậm
            
            last_u0 = row['last_unclaimed_fee0'] or 0
            if u0 < last_u0:
                # Nếu số hiện tại nhỏ hơn số cũ -> Đã có hành động Claim All
                # Giả định: Đã rút toàn bộ số cũ. Số hiện tại (u0) là tích lũy mới trong 2h qua.
                diff_f0 = last_u0 
            else:
                # Nếu số hiện tại lớn hơn -> Đang tích lũy thêm, chưa rút đồng nào
                diff_f0 = 0

            # Xử lý Fee Token 1
            last_u1 = row['last_unclaimed_fee1'] or 0
            if u1 < last_u1:
                diff_f1 = last_u1
            else:
                diff_f1 = 0

            # Xử lý Reward (CAKE)
            last_rew = row['last_pending_reward'] or 0
            if pending_rew < last_rew:
                diff_rew = last_rew
            else:
                diff_rew = 0

            # Tính ra usd
            if pnl_mode == 'pool_ratio':
                fee_claimed_base = calculate_value_in_base_by_pool_ratio(
                    diff_f0, diff_f1, pool_ratio, base_type)
                usd_fee_claimed = fee_claimed_base * p_base if p_base > 0 else 0
                usd_reward_claimed = diff_rew * p_reward
                
                reward_in_base = usd_reward_claimed / p_base if p_base > 0 else 0
                amount_to_add_base = fee_claimed_base + reward_in_base
            else:
                usd_fee_claimed = (diff_f0 * p0) + (diff_f1 * p1)
                usd_reward_claimed = diff_rew * p_reward
                
                amount_to_add_base = 0
                if p_base > 0:
                    amount_to_add_base = (usd_fee_claimed + usd_reward_claimed) / p_base

            if is_debug:
                logger.info(f"   [B] CLAIMED CHECK:")
                if usd_fee_claimed > 0 or usd_reward_claimed > 0:
                    logger.info(f"       • Diff Fees: {diff_f0} / {diff_f1}")
                    logger.info(f"       • Diff Reward: {diff_rew}")
                    logger.info(f"       • Fee Claimed: ${usd_fee_claimed}")
                    logger.info(f"       • Reward Claimed: ${usd_reward_claimed}")
                else:
                    logger.info(f"       • No new claims detected.")

            # --- 4. CÁC BIẾN TỔNG HỢP ---
            current_total_injected_usd = (row['total_cash_injected'] or 0) + cash_change_usd
            total_invested_base = (row['invested_capital_base'] or 0) + token_change_base
            
            if pnl_mode == 'pool_ratio':
                current_lp_base = calculate_value_in_base_by_pool_ratio(current_lp0, current_lp1, pool_ratio, base_type)
                current_unclaimed_fee_base = calculate_value_in_base_by_pool_ratio(u0, u1, pool_ratio, base_type)
                current_reward_base = (pending_rew * p_reward) / p_base if p_base > 0 else 0
                
                current_val_base = current_lp_base + current_unclaimed_fee_base + current_reward_base
                
                # Để DB tracking đúng thì cần set real_time_lp_value, current_unclaimed_usd
                real_time_lp_value = current_lp_base * p_base if p_base > 0 else 0
                current_unclaimed_usd = (current_unclaimed_fee_base + current_reward_base) * p_base if p_base > 0 else 0
                current_lp_and_unclaimed_usd = real_time_lp_value + current_unclaimed_usd
            else:
                # Tính lại giá trị LP theo giá thị trường hiện tại (Mark-to-Market)
                real_time_lp_value = total_current_lp_usd
                
                current_unclaimed_usd = (u0 * p0) + (u1 * p1) + (pending_rew * p_reward)
                current_lp_and_unclaimed_usd = real_time_lp_value + current_unclaimed_usd
                current_val_base = current_lp_and_unclaimed_usd / p_base if p_base > 0 else 0
            
            total_claimed_usd_all_time = (row['total_claimed_fee_usd'] or 0) + (row['total_claimed_reward_usd'] or 0) + usd_fee_claimed + usd_reward_claimed
            total_claimed_base = (row['total_claimed_in_base'] or 0) + amount_to_add_base

            # --- 5. TÍNH PnL ---
            if pnl_mode == 'pool_ratio':
                pnl_value_base = (current_val_base + total_claimed_base) - total_invested_base
                pnl_value_usd = pnl_value_base * p_base if p_base > 0 else 0
            else:
                pnl_value_usd = (current_lp_and_unclaimed_usd + total_claimed_usd_all_time) - current_total_injected_usd
                pnl_value_base = (current_val_base + total_claimed_base) - total_invested_base

            pnl_base_percent = 0
            if total_invested_base > 0:
                pnl_base_percent = ((current_val_base + total_claimed_base - total_invested_base) / total_invested_base) * 100

            if is_debug:
                logger.info(f"   [C] FINAL PnL CALCULATION:")
                logger.info(f"       • (1) Current LP Value (Realtime): ${real_time_lp_value:.2f}")
                logger.info(f"       • (2) Current Unclaimed: ${current_unclaimed_usd:.2f}")
                logger.info(f"       • (3) Total Claimed (All-time): ${total_claimed_usd_all_time:.2f}")
                logger.info(f"       • (3) Total Claimed (All-time) Base: {total_claimed_usd_all_time:.4f}{base_symbol}")
                logger.info(f"       • (4) Total Capital Injected: ${current_total_injected_usd:.2f}")
                logger.info(f"       • (4) Total Capital Injected Base: {total_invested_base:.4f}{base_symbol}")
                logger.info(f"       ---------------------------------------------")
                logger.info(f"       • PnL USD Formula: (1 + 2 + 3) - 4")
                logger.info(f"       • PnL USD RESULT: ${pnl_value_usd:.2f}")
                logger.info(f"       • PnL Base RESULT: {pnl_value_base:.4f} {base_symbol}")

            # --- 6. UPDATE ĐẦY ĐỦ ---
            # Lưu ý: last_lp_token0 được update bằng INITIAL để lần sau tính Diff
            sql_update = """
                UPDATE wallet_nft_summary SET 
                    total_cash_injected = total_cash_injected + %s,
                    invested_capital_base = invested_capital_base + %s,
                    
                    total_claimed_fee0 = total_claimed_fee0 + %s,
                    total_claimed_fee1 = total_claimed_fee1 + %s,
                    total_claimed_reward = total_claimed_reward + %s,
                    
                    total_claimed_fee_usd = total_claimed_fee_usd + %s,
                    total_claimed_reward_usd = total_claimed_reward_usd + %s,
                    total_claimed_in_base = total_claimed_in_base + %s,
                    
                    current_val_base = %s,
                    pnl_base_percent = %s,
                    pnl_value_usd = %s,
                    pnl_value_base = %s,
                    
                    last_lp_token0 = %s, last_lp_token1 = %s,
                    last_unclaimed_fee0 = %s, last_unclaimed_fee1 = %s, last_pending_reward = %s,
                    status = %s, updated_at = NOW()
                WHERE wallet_address = %s AND chain = %s AND type_dex = %s AND nft_id = %s AND npm_address = %s
            """
            cursor.execute(sql_update, (
                cash_change_usd, token_change_base,
                diff_f0, diff_f1, diff_rew,
                usd_fee_claimed, usd_reward_claimed,
                amount_to_add_base,
                current_val_base, pnl_base_percent,
                pnl_value_usd, pnl_value_base,
                initial_lp0, initial_lp1, # Dùng Initial để update Last
                u0, u1, pending_rew,
                status, wallet_address, chain, type_dex, nft_id, npm_address
            ))

        # --- 7. CẬP NHẬT PnL VÀO SNAPSHOT (Logic Hybrid) ---
        target_pos_id = position_id or data_nft.get('id')
        if target_pos_id:
            if is_debug: logger.info(f"   [D] UPDATING POSITION SNAPSHOT: {target_pos_id}")
            sql_pos = "UPDATE wallet_nft_position SET pnl_value_usd = %s, pnl_value_base = %s WHERE id = %s"
            cursor.execute(sql_pos, (pnl_value_usd, pnl_value_base, target_pos_id))
        
        if conn: conn.commit()
        
        if is_debug: logger.info(f"{'='*20} END DEBUG {'='*20}\n")

    except Exception as e:
        logger.error(f"❌ Error in process_nft_summary (NFT: {nft_id}): {e}")
    finally:
        if conn:
            cursor.close()
            conn.close()
        
def _legacy_backfill_summary():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True, buffered=True)
    
    try:
        logger.info("🧹 Đang làm sạch bảng Summary...")
        cursor.execute("TRUNCATE TABLE wallet_nft_summary")
        conn.commit()
        
        # Lấy bản ghi mới nhất của các NFT chưa bị Burn
        sql_active_ids = """
            SELECT t1.nft_id 
            FROM wallet_nft_position t1
            INNER JOIN (
                SELECT nft_id, MAX(created_at) as max_time
                FROM wallet_nft_position GROUP BY nft_id, COALESCE(npm_address, '')
            ) t2 ON t1.nft_id = t2.nft_id AND t1.created_at = t2.max_time
            WHERE t1.status != 'Burned'
        """
        cursor.execute(sql_active_ids)
        active_ids = [row['nft_id'] for row in cursor.fetchall()]

        if not active_ids:
            logger.warning("⚠️ Không có NFT nào để xử lý.")
            return

        # Lấy lịch sử theo thứ tự thời gian để tính toán lũy kế chính xác
        format_strings = ','.join(['%s'] * len(active_ids))
        sql_history = f"""
            SELECT * FROM wallet_nft_position 
            WHERE nft_id IN ({format_strings})
            ORDER BY nft_id, created_at ASC
        """
        cursor.execute(sql_history, tuple(active_ids))
        rows = cursor.fetchall()

        logger.info(f"🚀 Đang tính toán lại dữ liệu cho {len(active_ids)} NFT...")
        for r in rows:
            # R là dictionary, truyền thẳng vào hàm xử lý
            process_nft_summary(r, external_cursor=cursor)
        
        conn.commit()
        logger.info(f"✅ Backfill thành công!")
    
    except Exception as e:
        logger.error(f"❌ Error in backfill_summary: {e}")
    finally:
        cursor.close()
        conn.close()
        
def backfill_summary():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True, buffered=True)

    try:
        logger.info("Rebuilding wallet_nft_summary by full NFT identity...")
        cursor.execute("TRUNCATE TABLE wallet_nft_summary")
        conn.commit()

        sql_active_ids = """
            SELECT t1.wallet_address, t1.chain, t1.type_dex, t1.nft_id, COALESCE(t1.npm_address, '') AS npm_address
            FROM wallet_nft_position t1
            INNER JOIN (
                SELECT wallet_address, chain, type_dex, nft_id, COALESCE(npm_address, '') AS npm_address, MAX(created_at) AS max_time
                FROM wallet_nft_position
                GROUP BY wallet_address, chain, type_dex, nft_id, COALESCE(npm_address, '')
            ) t2 ON t1.wallet_address = t2.wallet_address
                AND t1.chain = t2.chain
                AND t1.type_dex = t2.type_dex
                AND t1.nft_id = t2.nft_id
                AND COALESCE(t1.npm_address, '') = t2.npm_address
                AND t1.created_at = t2.max_time
            WHERE t1.status != 'Burned'
        """
        cursor.execute(sql_active_ids)
        active_identities = cursor.fetchall()
        if not active_identities:
            logger.warning("No NFT identities to backfill.")
            return

        identity_conditions = []
        params = []
        for identity in active_identities:
            identity_conditions.append(
                "(wallet_address = %s AND chain = %s AND type_dex = %s AND nft_id = %s AND COALESCE(npm_address, '') = %s)"
            )
            params.extend([
                identity["wallet_address"],
                identity["chain"],
                identity["type_dex"],
                identity["nft_id"],
                identity.get("npm_address") or "",
            ])

        sql_history = f"""
            SELECT *
            FROM wallet_nft_position
            WHERE {' OR '.join(identity_conditions)}
            ORDER BY wallet_address, chain, type_dex, nft_id, COALESCE(npm_address, ''), created_at ASC
        """
        cursor.execute(sql_history, tuple(params))
        rows = cursor.fetchall()

        logger.info(f"Recalculating summary for {len(active_identities)} NFT identities...")
        for row in rows:
            process_nft_summary(row, external_cursor=cursor)

        conn.commit()
        logger.info("Backfill summary completed.")

    except Exception as e:
        logger.error(f"Error in identity-aware backfill_summary: {e}")
    finally:
        cursor.close()
        conn.close()

def process_batch_nft_summary(list_nft_data, list_ids=None, pnl_mode=None):
    """
    Hàm Wrapper để xử lý hàng loạt NFT cùng lúc.
    Tối ưu hóa connection: Chỉ mở 1 lần, chạy hết list, rồi đóng.
    
    Args:
        list_nft_data (list): Danh sách các dictionary hoặc tuple chứa thông tin NFT.
        list_ids (list, optional): Danh sách ID từ wallet_nft_position tương ứng.
    """
    if not list_nft_data or not isinstance(list_nft_data, list):
        logger.warning("⚠️ process_batch_nft_summary received empty or invalid list.")
        return

    logger.info(f"🚀 START BATCH PROCESSING: {len(list_nft_data)} NFTs...")
    
    # 1. Mở Connection chung 1 lần duy nhất cho cả lô
    conn = get_connection()
    # Dùng buffered=True để tránh lỗi "Unread result found" khi loop nhiều query
    cursor = conn.cursor(dictionary=True, buffered=True) 
    
    success_count = 0
    error_count = 0
    
    try:
        # 2. Loop qua từng NFT trong danh sách
        for i, nft_item in enumerate(list_nft_data):
            # Lấy ID để log nếu lỗi (dùng get an toàn)
            nft_id_log = "Unknown"
            if isinstance(nft_item, dict):
                nft_id_log = nft_item.get('nft_id', 'Unknown')
            elif isinstance(nft_item, (list, tuple)) and len(nft_item) > 2:
                nft_id_log = nft_item[2] # Giả định vị trí index 2 là ID như trong safe_map

            try:
                # Extract position_id if stored in nft_item or provided in list_ids
                pos_id = None
                if list_ids and i < len(list_ids):
                    pos_id = list_ids[i]
                
                if pos_id is None and isinstance(nft_item, dict):
                    pos_id = nft_item.get('id')

                process_nft_summary(nft_item, cursor, position_id=pos_id, pnl_mode=pnl_mode)
                success_count += 1
                
                # (Tuỳ chọn) Commit mỗi 50 items để tránh transaction quá lớn nếu list dài
                if i > 0 and i % 50 == 0:
                    conn.commit()
                    
            except Exception as e:
                error_count += 1
                logger.error(f"⚠️ Failed processing item {nft_id_log}: {e}")
        
        # 4. Commit lần cuối cho những item còn lại
        conn.commit()
        logger.info(f"✅ BATCH FINISHED. Success: {success_count}/{len(list_nft_data)} - Errors: {error_count}")
        
    except Exception as e:
        logger.critical(f"❌ CRITICAL BATCH ERROR: {e}")
        # Nếu lỗi connection nghiêm trọng thì rollback
        if conn: conn.rollback()
    finally:
        # 5. Đóng Connection chung
        if cursor: cursor.close()
        if conn: conn.close()
