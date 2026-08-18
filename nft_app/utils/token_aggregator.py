import mysql.connector
from datetime import datetime, timezone, timedelta
from services.db_connect import get_connection
from services.update_query import (
    fetch_all_pool_info,
    fetch_all_pool_sol_info,
    fetch_all_pool_info_aerodrome_db
)

UTC_PLUS_7 = timezone(timedelta(hours=7))

def ensure_token_groups_tables(cursor):
    """Tự động tạo 2 bảng token_groups và token_group_members chuẩn hóa nếu chưa có trong Database"""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS token_groups (
            group_id INT AUTO_INCREMENT PRIMARY KEY,
            primary_symbol VARCHAR(255),
            primary_identifier VARCHAR(255) NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS token_group_members (
            id INT AUTO_INCREMENT PRIMARY KEY,
            group_id INT NOT NULL,
            symbol VARCHAR(255),
            token_identifier VARCHAR(255) NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES token_groups(group_id) ON DELETE CASCADE
        );
    """)

def ensure_token_group_mappings_table(cursor):
    """Giữ hàm tương thích cũ và đảm bảo khởi tạo 2 bảng mới"""
    ensure_token_groups_tables(cursor)

def format_compact_usd(val):
    """Hỗ trợ format tiền USD rút gọn K, M, B, T"""
    if val is None or val <= 0:
        return "-"
    val = float(val)
    if val >= 1_000_000_000_000:
        return f"${val / 1_000_000_000_000:,.2f}T"
    elif val >= 1_000_000_000:
        return f"${val / 1_000_000_000:,.2f}B"
    elif val >= 1_000_000:
        return f"${val / 1_000_000:,.2f}M"
    elif val >= 1_000:
        return f"${val / 1_000:,.2f}K"
    return f"${val:,.2f}"

def fetch_and_aggregate_all_tokens():
    """
    1. Lấy dữ liệu token từ 3 bảng pool khác nhau:
       - EVM PancakeSwap (pool_info)
       - Solana (pool_sol_infor)
       - Aerodrome (aerodrome_pool_info)
    2. Loại bỏ token trùng (chain + contract_address)
    3. Trả về danh sách token thô đã được enrich metadata (Note, Perpetual, MC, FDV)
    """
    evm_pools = fetch_all_pool_info()
    sol_pools_raw = fetch_all_pool_sol_info()
    sol_pools = [p for p in sol_pools_raw if p.get('weekly_rewards', 0) > 0]
    aero_pools = fetch_all_pool_info_aerodrome_db()

    all_tokens = []
    for pool in evm_pools:
        if pool.get('token0_address'):
            all_tokens.append({
                'chain': pool['chain'],
                'contract_address': pool['token0_address'],
                'symbol': pool['token0_symbol'],
                'pool_address': pool.get('pool_address', '')
            })
        if pool.get('token1_address'):
            all_tokens.append({
                'chain': pool['chain'],
                'contract_address': pool['token1_address'],
                'symbol': pool['token1_symbol'],
                'pool_address': pool.get('pool_address', '')
            })

    for pool in sol_pools:
        if pool.get('token0_mint'):
            all_tokens.append({
                'chain': pool['chain'],
                'contract_address': pool['token0_mint'],
                'symbol': pool['token0_symbol'],
                'pool_address': pool.get('pool_account', '')
            })
        if pool.get('token1_mint'):
            all_tokens.append({
                'chain': pool['chain'],
                'contract_address': pool['token1_mint'],
                'symbol': pool['token1_symbol'],
                'pool_address': pool.get('pool_account', '')
            })

    for pool in aero_pools:
        if pool.get('token0_address'):
            all_tokens.append({
                'chain': pool['chain'],
                'contract_address': pool['token0_address'],
                'symbol': pool['token0_symbol'],
                'pool_address': pool.get('pool_address', '')
            })
        if pool.get('token1_address'):
            all_tokens.append({
                'chain': pool['chain'],
                'contract_address': pool['token1_address'],
                'symbol': pool['token1_symbol'],
                'pool_address': pool.get('pool_address', '')
            })

    # Deduplicate theo (chain, contract_address) và làm sạch symbol
    unique_tokens = {}
    for token in all_tokens:
        chain_str = str(token['chain']).strip().upper()
        addr_str = str(token['contract_address']).strip().lower()
        sym_str = str(token.get('symbol', '')).replace('\x00', '').strip()
        token['symbol'] = sym_str
        token['chain'] = chain_str
        token['contract_address'] = addr_str
        token['identifier'] = f"{chain_str}:{addr_str}"
        key = (chain_str, addr_str)
        if key not in unique_tokens:
            unique_tokens[key] = token

    tokens = list(unique_tokens.values())

    # Query bổ sung User note, Perpetual, MC, FDV
    conn = get_connection()
    cursor = conn.cursor()

    ensure_token_group_mappings_table(cursor)
    conn.commit()

    query = """
        SELECT 
            un.id,
            un.user_note,
            un.updated_at,
            tfm.exchange,
            tcm.market_cap_usd,
            tcm.fdv_usd
        FROM (SELECT %s AS chain, %s AS contract_address) t
        LEFT JOIN user_note un 
            ON un.chain = t.chain AND un.contract_address = t.contract_address
        LEFT JOIN token_futures_market tfm 
            ON tfm.chain = t.chain AND tfm.token_address = t.contract_address
        LEFT JOIN token_cmc_map tcm 
            ON tcm.chain = t.chain AND tcm.token_address = t.contract_address
        LIMIT 1
    """

    for token in tokens:
        try:
            cursor.execute(query, (token['chain'], token['contract_address']))
            result = cursor.fetchone()
            if result:
                token['note_id'] = result[0]
                token['user_note'] = result[1] or ''
                if result[2]:
                    utc_time = result[2]
                    if utc_time.tzinfo is None:
                        utc_time = utc_time.replace(tzinfo=timezone.utc)
                    token['note_updated_at'] = utc_time.astimezone(UTC_PLUS_7).strftime('%Y-%m-%d %H:%M:%S')
                else:
                    token['note_updated_at'] = None

                token['perpetual'] = result[3] if result[3] else '-'
                token['market_cap'] = result[4]
                token['fdv'] = result[5]
                token['market_cap_formatted'] = format_compact_usd(result[4])
                token['fdv_formatted'] = format_compact_usd(result[5])
            else:
                token['note_id'] = None
                token['user_note'] = ''
                token['note_updated_at'] = None
                token['perpetual'] = '-'
                token['market_cap'] = None
                token['fdv'] = None
                token['market_cap_formatted'] = '-'
                token['fdv_formatted'] = '-'
        except Exception:
            token['note_id'] = None
            token['user_note'] = ''
            token['note_updated_at'] = None
            token['perpetual'] = '-'
            token['market_cap'] = None
            token['fdv'] = None
            token['market_cap_formatted'] = '-'
            token['fdv_formatted'] = '-'

    conn.close()
    return tokens

from services.token_grouper import extract_base_symbol

def get_grouped_token_list():
    """
    Hàm gộp token chuẩn theo Symbol (base symbol):
    - Gộp tất cả các biến thể token (USDT, USDC, WETH, WBTC, CAKE, ...) từ mọi chain & prefix/suffix
    - Tích hợp thêm mapping thủ công từ bảng token_group_mappings nếu có
    - Mỗi dòng chỉ đại diện cho 1 nhóm token, Dropdown chứa danh sách tất cả các biến thể (variants) của token đó.
    """
    raw_tokens = fetch_and_aggregate_all_tokens()
    token_by_id = {t['identifier']: t for t in raw_tokens}

    conn = get_connection()
    cursor = conn.cursor()
    ensure_token_groups_tables(cursor)

    cursor.execute("""
        SELECT m.token_identifier, g.primary_identifier
        FROM token_group_members m
        JOIN token_groups g ON m.group_id = g.group_id
    """)
    db_rows = cursor.fetchall()
    conn.close()

    db_sub_to_primary = {row[0]: row[1] for row in db_rows}
    db_primary_to_subs = {}
    for sub_id, prim_id in db_rows:
        if prim_id not in db_primary_to_subs:
            db_primary_to_subs[prim_id] = []
        db_primary_to_subs[prim_id].append(sub_id)

    # 1. Gom nhóm tự động theo Base Symbol
    groups_by_base = {}
    for t in raw_tokens:
        t_id = t['identifier']
        if t_id in db_sub_to_primary:
            continue
        base = extract_base_symbol(t['symbol']).upper().strip()
        if not base:
            base = t['symbol'].upper().strip()
        
        if base not in groups_by_base:
            groups_by_base[base] = []
        groups_by_base[base].append(t)

    grouped_tokens = []
    processed_ids = set()

    for token in raw_tokens:
        t_id = token['identifier']
        if t_id in processed_ids or t_id in db_sub_to_primary:
            continue

        base = extract_base_symbol(token['symbol']).upper().strip() or token['symbol'].upper().strip()
        group_items = groups_by_base.get(base, [token])

        # Ưu tiên chọn primary token: ARB -> ETH -> BNB -> BAS -> SOL
        CHAIN_PRIORITY = {'ARB': 1, 'ETH': 2, 'BNB': 3, 'BAS': 4, 'SOL': 5}
        group_items_sorted = sorted(group_items, key=lambda x: (
            CHAIN_PRIORITY.get(x['chain'], 99),
            0 if x['symbol'].upper().strip() == base else 1
        ))
        primary = group_items_sorted[0]

        # Gom tất cả variants (dùng dict để khử trùng theo identifier)
        variant_dict = {}
        for item in group_items:
            variant_dict[item['identifier']] = item
            processed_ids.add(item['identifier'])

        # Nếu có DB sub-token mapping thủ công -> gộp vào
        if primary['identifier'] in db_primary_to_subs:
            for sub_id in db_primary_to_subs[primary['identifier']]:
                if sub_id in token_by_id:
                    variant_dict[sub_id] = token_by_id[sub_id]
                    processed_ids.add(sub_id)

        variants = list(variant_dict.values())

        # Sắp xếp variants: token primary đặt đầu tiên, các token khác xếp sau theo Chain
        primary_id = primary['identifier']
        variants_sorted = [v for v in variants if v['identifier'] == primary_id] + \
                          sorted([v for v in variants if v['identifier'] != primary_id], key=lambda x: (x['chain'], x['symbol']))

        primary_copy = dict(primary)
        primary_copy['variants'] = variants_sorted
        grouped_tokens.append(primary_copy)

    return grouped_tokens

