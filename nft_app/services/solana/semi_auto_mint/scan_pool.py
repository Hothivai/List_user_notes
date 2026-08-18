from solders import pubkey
from solana.rpc.api import Client
from services.solana.helper import *
from config import *
import struct
from services.solana.decode_account import decode_pubkey, decode_u128, decode_i128, decode_metadata_pda
from solana.rpc.types import MemcmpOpts
import requests
import math
from services.update_query import get_pool_sol_info
from services.pancake_api import get_price_tokens_coingecko, get_token_price_token_by_cmc
from services.db_connect import get_connection
import mysql.connector
import base64

HELIUS_RPC = "https://mainnet.helius-rpc.com/"
API_KEY = "bb4fcdca-d41d-4930-ada1-6490968dabe4"

POOLS_ALWAYS_FETCH_BITMAP = [
    "5YPxToTobawvkbn5rkWKYDhZqHf5v6LAtRLNPGiq6U2A",   
]

headers = {"Content-Type": "application/json"}
querystring = {"api-key": API_KEY}

def decode_tick_state_from_raw(data, offset):
    tick, = struct.unpack_from("<i", data, offset)
    offset += 4

    liquidity_net, offset = decode_i128(data, offset)
    liquidity_gross, offset = decode_u128(data, offset)
    fee_growth_outside_0_x64, offset = decode_u128(data, offset)
    fee_growth_outside_1_x64, offset = decode_u128(data, offset)

    reward_growths = []
    for _ in range(3):
        val, offset = decode_u128(data, offset)
        reward_growths.append(val)

    # padding 32 or 52 byte? --> skip để align 160 byte
    offset += 52  

    return {
        "tick": tick,
        "liquidity_net": liquidity_net,
        "liquidity_gross": liquidity_gross,
        "fee_growth_outside_0_x64": fee_growth_outside_0_x64,
        "fee_growth_outside_1_x64": fee_growth_outside_1_x64,
        "reward_growths_outside_x64": reward_growths
    }, offset

def decode_tick_array_state_from_raw(data):
    # ---- Kiểm tra độ dài trước ----
    if not data or len(data) < 10240:
        print(f"⚠️ Skip account: invalid data length ({len(data) if data else 0})")
        return {
            "pool_id": None,
            "start_tick_index": None,
            "initialized_tick_count": None,
            "recent_epoch": None,
            "ticks": None
        }

    try:
        offset = 8
        pool_id, offset = decode_pubkey(data, offset)
        start_tick_index = struct.unpack_from("<i", data, offset)[0]
        offset += 4

        ticks = []
        for _ in range(60):
            tick_state, offset = decode_tick_state_from_raw(data, offset)
            ticks.append(tick_state)

        initialized_tick_count = data[offset]
        offset += 1

        recent_epoch, = struct.unpack_from("<Q", data, offset)
        offset += 8

        return {
            "pool_id": pool_id,
            "start_tick_index": start_tick_index,
            "initialized_tick_count": initialized_tick_count,
            "recent_epoch": recent_epoch,
            "ticks": ticks
        }

    except Exception as e:
        print(f"⚠️ Decode failed (skip account): {type(e).__name__}: {e}")
        return {
            "pool_id": None,
            "start_tick_index": None,
            "initialized_tick_count": None,
            "recent_epoch": None,
            "ticks": None
        }

def decode_reward_info_pool_from_raw(data: bytes, offset: int):
    rewards = []
    for _ in range(3):
        reward_state = struct.unpack_from("<B", data, offset)[0]; offset += 1
        open_time, end_time, last_update_time = struct.unpack_from("<QQQ", data, offset); offset += 24
        emissions_per_second_x64, offset = decode_u128(data, offset)
        reward_total_emissioned, reward_claimed = struct.unpack_from("<QQ", data, offset); offset += 16
        token_mint, offset = decode_pubkey(data, offset)
        token_vault, offset = decode_pubkey(data, offset)
        authority, offset = decode_pubkey(data, offset)
        reward_growth_global_x64, offset = decode_u128(data, offset)

        rewards.append({
            "reward_state": reward_state,
            "open_time": open_time,
            "end_time": end_time,
            "last_update_time": last_update_time,
            "emissions_per_second_x64": emissions_per_second_x64,
            "reward_total_emissioned": reward_total_emissioned,
            "reward_claimed": reward_claimed,
            "token_mint": token_mint,
            "token_vault": token_vault,
            "authority": authority,
            "reward_growth_global_x64": reward_growth_global_x64,
        })
    return rewards, offset

def decode_pool_state_from_raw(raw_data):
    """Parse raw data from a pool account (safe decode)."""
    try:
        if len(raw_data) < 1544:
            raise ValueError(f"Invalid data length: {len(raw_data)}")
        
        offset = 8
        bump = int.from_bytes(raw_data[offset:offset+1], "little"); offset += 1
        
        amm_config, offset = decode_pubkey(raw_data, offset)
        owner, offset = decode_pubkey(raw_data, offset)
        token_mint_0, offset = decode_pubkey(raw_data, offset)
        token_mint_1, offset = decode_pubkey(raw_data, offset)
        token_vault_0, offset = decode_pubkey(raw_data, offset)
        token_vault_1, offset = decode_pubkey(raw_data, offset)
        observation_key, offset = decode_pubkey(raw_data, offset)

        mint_decimals_0 = int.from_bytes(raw_data[offset:offset + 1], "little"); offset += 1
        mint_decimals_1 = int.from_bytes(raw_data[offset:offset + 1], "little"); offset += 1

        tick_spacing, = struct.unpack_from("<H", raw_data, offset); offset += 2
        liquidity, offset = decode_u128(raw_data, offset)
        sqrt_price_x64, offset = decode_u128(raw_data, offset)

        tick_current, = struct.unpack_from("<i", raw_data, offset); offset += 4

        offset += 4  # skip paddings

        fee_growth_global_0_x64, offset = decode_u128(raw_data, offset)
        fee_growth_global_1_x64, offset = decode_u128(raw_data, offset)

        offset += 16  # skip protocol fees
        swap_in_amount_token_0, offset = decode_u128(raw_data, offset)
        swap_out_amount_token_1, offset = decode_u128(raw_data, offset)
        swap_in_amount_token_1, offset = decode_u128(raw_data, offset)
        swap_out_amount_token_0, offset = decode_u128(raw_data, offset)

        status = int.from_bytes(raw_data[offset:offset + 1], "little"); offset += 8

        reward_infos, offset = decode_reward_info_pool_from_raw(raw_data, offset)

        return {
            "token_mint_0": str(token_mint_0),
            "token_mint_1": str(token_mint_1),
            "mint_decimals_0": mint_decimals_0,
            "mint_decimals_1": mint_decimals_1,
            "liquidity": liquidity,
            "sqrt_price_x64": sqrt_price_x64,
            "tick_current": tick_current,
            "fee_growth_global_0_x64": fee_growth_global_0_x64,
            "fee_growth_global_1_x64": fee_growth_global_1_x64,
            "tick_spacing": tick_spacing,
            "token_vault_0": str(token_vault_0),
            "token_vault_1": str(token_vault_1),
            "reward_infos": reward_infos,
        }

    except Exception as e:
        print(f"⚠️ Skip account decode error: {type(e).__name__} → {e}")
        return {
            "token_mint_0": None,
            "token_mint_1": None,
            "mint_decimals_0": None,
            "mint_decimals_1": None,
            "liquidity": None,
            "sqrt_price_x64": None,
            "tick_current": None,
            "fee_growth_global_0_x64": None,
            "fee_growth_global_1_x64": None,
            "tick_spacing": None,
            "reward_infos": None
        }

def decode_personal_position_state_from_raw(data: bytes):
    """Decode Raydium CLMM PersonalPositionState safely."""
    try:
        # Check minimum length
        if len(data) < 281:
            raise ValueError(f"Invalid account data length: {len(data)}")

        offset = 8  # skip discriminator
        bump = int.from_bytes(data[offset:offset + 1], "little"); offset += 1

        nft_mint, offset = decode_pubkey(data, offset)
        pool_id, offset = decode_pubkey(data, offset)

        tick_lower_index = struct.unpack_from("<i", data, offset)[0]; offset += 4
        tick_upper_index = struct.unpack_from("<i", data, offset)[0]; offset += 4

        liquidity, offset = decode_u128(data, offset)
        fee_growth_inside_0_last_x64, offset = decode_u128(data, offset)
        fee_growth_inside_1_last_x64, offset = decode_u128(data, offset)

        token_fees_owed_0 = struct.unpack_from("<Q", data, offset)[0]; offset += 8
        token_fees_owed_1 = struct.unpack_from("<Q", data, offset)[0]; offset += 8

        return {
            "nft_mint": str(nft_mint),
            "pool_id": str(pool_id),
            "tick_lower_index": tick_lower_index,
            "tick_upper_index": tick_upper_index,
            "liquidity": liquidity,
            "fee_growth_inside_0_last_x64": fee_growth_inside_0_last_x64,
            "fee_growth_inside_1_last_x64": fee_growth_inside_1_last_x64,
            "token_fees_owed_0": token_fees_owed_0,
            "token_fees_owed_1": token_fees_owed_1
        }

    except Exception as e:
        print(f"⚠️ Skip personal position decode error: {type(e).__name__} → {e}")
        return {
            "nft_mint": None,
            "pool_id": None,
            "tick_lower_index": None,
            "tick_upper_index": None,
            "liquidity": None,
            "fee_growth_inside_0_last_x64": None,
            "fee_growth_inside_1_last_x64": None,
            "token_fees_owed_0": None,
            "token_fees_owed_1": None
        }

def get_current_amounts(liquidity, sqrt_price_x96, tick_lower, tick_upper):
    sqrt_price = float(sqrt_price_x96) / 2**64
    sqrt_price_lower = math.sqrt(1.0001 ** tick_lower)
    sqrt_price_upper = math.sqrt(1.0001 ** tick_upper)
    
    if sqrt_price <= sqrt_price_lower:
        amount0 = liquidity * (sqrt_price_upper - sqrt_price_lower) / (sqrt_price_lower * sqrt_price_upper)
        amount1 = 0
    elif sqrt_price < sqrt_price_upper:
        amount0 = liquidity * (sqrt_price_upper - sqrt_price) / (sqrt_price * sqrt_price_upper)
        amount1 = liquidity * (sqrt_price - sqrt_price_lower)
    else:
        amount0 = 0
        amount1 = liquidity * (sqrt_price_upper - sqrt_price_lower)

    return amount0, amount1

def get_position_status(liquidity, tick_lower, tick_upper, current_tick, tokens_owed0, tokens_owed1):
    if liquidity > 0:
        if tick_lower <= current_tick <= tick_upper:
            return "Active"
        else:
            return "Inactive"
    elif tokens_owed0 > 0 or tokens_owed1 > 0:
        return "Unclaimed"
    else:
        return "Closed"

def update_db_extreme_price_ranges(pool_pubkey, tick_lower, tick_upper, min_price, max_price, tick_array_bitmap_extension_account):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        query = """
            INSERT INTO extreme_price_range_pool_sol (pool_id, tick_lower, tick_upper, min_price, max_price, tick_array_bitmap_extension_account)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                tick_lower = VALUES(tick_lower),
                tick_upper = VALUES(tick_upper),
                min_price = VALUES(min_price),
                max_price = VALUES(max_price)
        """
        cursor.execute(query, (pool_pubkey, tick_lower, tick_upper, min_price, max_price, tick_array_bitmap_extension_account))
        conn.commit()
        
    except mysql.connector.Error as e:
        print(f"Database error: {e}")
    finally:
        cursor.close()
        conn.close()

def get_full_range_ticks_for_pool(tick_spacing):
    """
    Tính full range ticks hợp lệ dựa trên tick_spacing của pool
    """
    MIN_TICK = -443636
    MAX_TICK = 443636
    
    tick_lower = math.ceil(MIN_TICK / tick_spacing) * tick_spacing
    tick_upper = math.floor(MAX_TICK / tick_spacing) * tick_spacing
    
    return tick_lower, tick_upper

def is_full_range_position(tick_lower, tick_upper, tick_spacing):
    """
    Kiểm tra xem position có phải full range không
    """
    expected_lower, expected_upper = get_full_range_ticks_for_pool(tick_spacing)
    
    return (
        tick_lower == expected_lower and 
        tick_upper == expected_upper
    )

def get_signature_from_nft_mint(mint_address, api_key):
    url = "https://mainnet.helius-rpc.com/"
    headers = {"Content-Type": "application/json"}
    querystring = {"api-key": api_key}

    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "getSignaturesForAddress",
        "params": [
            mint_address,
            {"limit": 1}
        ]
    }

    resp = requests.post(url, json=payload, headers=headers, params=querystring).json()
    result = resp.get("result")
    if not result or len(result) == 0:
        raise Exception("No signatures found for the given mint address")

    signature = result[0]["signature"]
    return signature

def find_tick_array_bitmap_ext(signature, api_key):
    url = "https://mainnet.helius-rpc.com/"
    headers = {"Content-Type": "application/json"}
    querystring = {"api-key": api_key}

    # Bước 1: Lấy transaction
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "getTransaction",
        "params": [
            signature,
            {
                "commitment": "finalized",
                "maxSupportedTransactionVersion": 0,
                "encoding": "jsonParsed"
            }
        ]
    }

    resp = requests.post(url, json=payload, headers=headers, params=querystring).json()
    result = resp.get("result")
    if not result:
        raise Exception("Transaction not found")

    # Lấy account keys - cần xử lý cả 2 trường hợp: string hoặc object
    raw_account_keys = result["transaction"]["message"]["accountKeys"]
    
    # Chuyển đổi thành list các pubkey string
    account_keys = []
    for key in raw_account_keys:
        if isinstance(key, str):
            account_keys.append(key)
        elif isinstance(key, dict):
            account_keys.append(key.get("pubkey"))
        else:
            continue
    
    print(f"Found {len(account_keys)} accounts to check")
    
    # Bước 2: Kiểm tra từng account để tìm account có size 1832 bytes
    for account_key in account_keys:
        if not account_key:
            continue
            
        account_payload = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "getAccountInfo",
            "params": [
                account_key,
                {"encoding": "base64"}
            ]
        }
        
        account_resp = requests.post(url, json=account_payload, headers=headers, params=querystring).json()
        
        if "error" in account_resp:
            print(f"Error checking {account_key}: {account_resp['error']}")
            continue
            
        account_result = account_resp.get("result")
        
        if account_result and account_result.get("value"):
            data = account_result["value"]["data"][0]
            data_bytes = base64.b64decode(data)
            
            print(f"Account {account_key}: {len(data_bytes)} bytes")
            
            if len(data_bytes) == 1832:
                return account_key
    
    return None

def get_position_owner(client: Client, nft_mint: str):
    """
    Tìm địa chỉ ví đang sở hữu NFT Position.
    """
    try:
        mint_pubkey = Pubkey.from_string(nft_mint)
        
        # 1. Tìm Token Account đang giữ NFT (Largest holder, vì NFT supply = 1)
        largest_accounts = client.get_token_largest_accounts(mint_pubkey)
        
        if not largest_accounts.value:
            return None
            
        # Lấy địa chỉ Token Account chứa NFT
        token_account_pubkey = largest_accounts.value[0].address
        
        # 2. Lấy thông tin của Token Account để tìm Owner thực sự (Ví user)
        acc_info = client.get_account_info(token_account_pubkey)
        
        if not acc_info.value:
            return None
            
        data = acc_info.value.data
        
        owner_bytes = data[32:64]
        owner_pubkey = Pubkey(owner_bytes)
        
        return str(owner_pubkey)
        
    except Exception as e:
        print(f"⚠️ Error finding owner for NFT {nft_mint}: {e}")
        return None

def get_price_ranges_pool_by_personal_position(bytes_length: int, pool_pubkey: Pubkey,  client: Client, program_id: Pubkey, mint_decimals_0: int, mint_decimals_1: int, tick_spacing):
    filters = [
        bytes_length
    ]
    
    resp = client.get_program_accounts(
        program_id,
        filters=[
            281,
            MemcmpOpts(offset=41, bytes=str(pool_pubkey))
        ]
    )
    
    print(f"Total personal position accounts: {len(resp.value)}")
    accounts = resp.value
    pool_pubkey_bytes = bytes(pool_pubkey)
    
    accounts_pubkeys = [acc.pubkey for acc in accounts if pool_pubkey_bytes in acc.account.data]
    batches = [accounts_pubkeys[i:i+100] for i in range(0, len(accounts_pubkeys), 100)]
    
    # check has get tick array bitmap extension account in DB
    has_fetched_special_pool_bitmap = False
    
    price_ranges = []
    for batch in batches:
        try:
            res = client.get_multiple_accounts(batch)
            accounts = res.value  # danh sách account info theo thứ tự batch
            for pubkey, acc in zip(batch, accounts):
                if acc is None:
                    print(f"{pubkey} is None (might be closed or invalid)")
                    continue
                data = acc.data 
                personal_position_state = decode_personal_position_state_from_raw(data)
                tick_lower_index = personal_position_state["tick_lower_index"]
                tick_upper_index = personal_position_state["tick_upper_index"]
                liquidity = personal_position_state["liquidity"]
                token_fees_owed_0 = personal_position_state["token_fees_owed_0"]
                token_fees_owed_1 = personal_position_state["token_fees_owed_1"]
                nft_mint = personal_position_state["nft_mint"]
                
                price_lower = tick_to_price(tick_lower_index, mint_decimals_0, mint_decimals_1)
                price_upper = tick_to_price(tick_upper_index, mint_decimals_0, mint_decimals_1)
                
                is_full_range = is_full_range_position(tick_lower_index, tick_upper_index, tick_spacing)
                is_special_pool = str(pool_pubkey) in POOLS_ALWAYS_FETCH_BITMAP
                
                if is_full_range or (is_special_pool and not has_fetched_special_pool_bitmap):
                    signatute = get_signature_from_nft_mint(nft_mint, API_KEY)
                    tick_array_bitmap_account = find_tick_array_bitmap_ext(signatute, API_KEY)
                    print(f"Full range position found! Personal Position: {pubkey}, NFT Mint: {nft_mint}, Tick Array Bitmap Account: {tick_array_bitmap_account}")
                    
                    if tick_array_bitmap_account:
                        update_db_extreme_price_ranges(
                            str(pool_pubkey),
                            tick_lower_index,
                            tick_upper_index,
                            price_lower,
                            price_upper,
                            str(tick_array_bitmap_account)
                        )
                    
                    if is_special_pool:
                        has_fetched_special_pool_bitmap = True
                
                price_ranges.append({
                    "pubkey": str(pubkey),
                    "tick_low": tick_lower_index,
                    "tick_up": tick_upper_index,
                    "price_low": price_lower,
                    "price_up": price_upper,
                    "liquidity": liquidity,
                    "token_fees_owed_0": token_fees_owed_0,
                    "token_fees_owed_1": token_fees_owed_1,
                    "nft_mint": (nft_mint),
                })
        except Exception as e:
            print("Batch error:", e)
    
    return len(accounts_pubkeys), price_ranges

def get_tick_arrays_from_pool(client, program_id, pool_id, filters=None, batch_size=100):
    if filters is None:
        filters = [10240]

    resp = client.get_program_accounts(
        program_id,
        filters=[
            10240,
            MemcmpOpts(offset=8, bytes=str(pool_id))    
        ]
    )
    
    print(f"Total tick array accounts: {len(resp.value)}")
    pool_bytes = bytes(pool_id)

    tick_pubkeys = [acc.pubkey for acc in resp.value if pool_bytes in acc.account.data]
    batches = [tick_pubkeys[i:i+batch_size] for i in range(0, len(tick_pubkeys), batch_size)]

    tick_arrays_data = []

    for batch in batches:
        try:
            res = client.get_multiple_accounts(batch)
            accounts = res.value
            for pubkey, acc in zip(batch, accounts):
                if acc is None:
                    continue
                data = acc.data
                tick_array_state = decode_tick_array_state_from_raw(data)
                tick_arrays_data.append({
                    "pubkey": str(pubkey),
                    "start_tick_index": tick_array_state["start_tick_index"],
                    "ticks": tick_array_state["ticks"]
                })
        except Exception as e:
            print("Batch error:", e)

    return tick_arrays_data

def analyze_pool_ticks(client, program_id, pool_id, batch_size=100):
    # 1. Lấy pool info
    resp = client.get_account_info(pool_id)
    account_info = resp.value
    data = account_info.data
    pool_info = decode_pool_state_from_raw(data)
    
    mint_decimals_0 = pool_info["mint_decimals_0"]
    mint_decimals_1 = pool_info["mint_decimals_1"]
    tick_spacing    = pool_info["tick_spacing"]
    token_mint_0    = pool_info["token_mint_0"]
    token_mint_1    = pool_info["token_mint_1"]
    sqrt_price_x64  = pool_info["sqrt_price_x64"]
    current_tick    = pool_info["tick_current"]
    liquidity       = pool_info["liquidity"]
    pool_info["pool_id"] = str(pool_id)
    print(f"Pool info: mint_decimals_0={mint_decimals_0}, mint_decimals_1={mint_decimals_1}, tick_spacing={tick_spacing}, token_mint_0={token_mint_0}, token_mint_1={token_mint_1}, sqrt_price_x64={sqrt_price_x64}")
    
    # 4. Personal position check
    total_personal_accounts, personal_price_ranges = get_price_ranges_pool_by_personal_position(
        281, pool_id, client, program_id, mint_decimals_0, mint_decimals_1, tick_spacing
    )
    
    pool_db_info = get_pool_sol_info(str(pool_id))
    if pool_db_info:
        token_mint_0_symbol = str(pool_db_info.get("token0_symbol", "Unknown"))
        token_mint_1_symbol = str(pool_db_info.get("token1_symbol", "Unknown"))
    else:
        token_mint_0_info = decode_metadata_pda(client, str(token_mint_0))
        token_mint_1_info = decode_metadata_pda(client, str(token_mint_1)) 
        token_mint_0_symbol = str(token_mint_0_info.get("symbol") if token_mint_0_info else str(token_mint_0))
        token_mint_1_symbol = str(token_mint_1_info.get("symbol") if token_mint_1_info else str(token_mint_1))
    
    # Get tokens price
    token0_price = get_token_price_token_by_cmc("SOL", token_mint_0)
    token1_price = get_token_price_token_by_cmc("SOL", token_mint_1)
    if token0_price is None:
        token0_price = get_price_tokens_coingecko("SOL", token_mint_0)
    if token1_price is None:
        token1_price = get_price_tokens_coingecko("SOL", token_mint_1)
    
    for r in personal_price_ranges:
        tick_lower_index = r["tick_low"]
        tick_upper_index = r["tick_up"]
        liquidity = r["liquidity"]
        token_fees_owed_0 = r["token_fees_owed_0"]
        token_fees_owed_1 = r["token_fees_owed_1"]
        
        amount0_raw, amount1_raw = get_current_amounts(liquidity, sqrt_price_x64, tick_lower_index, tick_upper_index)
        amount0 = amount0_raw / (10 ** mint_decimals_0)
        amount1 = amount1_raw / (10 ** mint_decimals_1)
        total_value = amount0 * token0_price + amount1 * token1_price
        
        status = get_position_status(liquidity, tick_lower_index, tick_upper_index, current_tick, token_fees_owed_0, token_fees_owed_1)
        
        r["status"] = status
        r["amount0"] = amount0
        r["amount1"] = amount1
        r["total_value"] = total_value
        r["token0_price"] = token0_price
        r["token1_price"] = token1_price
    
    return {
        "pool_info": pool_info,
        "total_personal_positions": total_personal_accounts,
        "personal_price_ranges": personal_price_ranges,
        "token0_symbol": token_mint_0_symbol,
        "token1_symbol": token_mint_1_symbol
    }
    
    