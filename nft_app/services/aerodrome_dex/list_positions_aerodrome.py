# import sys
# import os
# # Lấy path tới root của project
# PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
# sys.path.append(PROJECT_ROOT)

import requests
import json
from web3 import Web3
from datetime import datetime, timedelta, timezone
import os
import math
from services.helper import *
from services.db_connect import get_connection
import time
from concurrent.futures import ThreadPoolExecutor
from w3multicall.multicall import W3Multicall
from logging_setup import aerodrome_evm_logger as log
from services.pool_stake.stake_liquidity import get_positions_multicall_aerodrome, get_current_tick
from config import *
from services.execute_data import (
    get_last_pending_cake_info, get_last_unclaimed_fee_token, insert_nft_closed_cache, update_nft_status_to_burned
)
from services.update_query import get_nft_initial_amount_from_db, get_aerodrome_pool_info, get_nft_status_data, get_rewards_per_second_of_aerodrome_pool
from services.pancake_api import get_price_tokens, get_aero_price_usd
from services.event_history.increase_liquidity_history import get_increase_liquidity_history
from services.event_history.decrease_liquidity_history import get_decrease_liquidity_history
from services.event_history.stake_liquidity_history import get_stake_time
from services.event_history.collect_fee_history import get_last_collect_time
from services.helper import *

# Send message to Telegram
def send_telegram_message(message, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        log.warning(f"❌ Failed to send Telegram message: {e}")

def send_discord_webhook_message(message: str, webhook_url: str = DISCORD_WEBHOOK_URL):
    data = {"content": message}
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.post(webhook_url, json=data, headers=headers, timeout=5)
        log.info(f"✅ Discord webhook sent: {response.status_code}")
    except Exception as e:
        log.warning(f"❌ Failed to send Discord webhook: {e}")

def get_list_gauges_address(chain_name: str) -> set:
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    
    sql = """
        SELECT DISTINCT gauge_address
        FROM aerodrome_pool_epoch_state
        WHERE chain = %s AND farm_active = 1 AND gauge_address IS NOT NULL
    """
    try:
        cursor.execute(sql, (chain_name,))
        results = cursor.fetchall()
        return {r['gauge_address'].lower() for r in results}
    except Exception as e:
        log.error(f"❌ Error fetching gauge addresses from DB: {e}")
        return set()

# Get Web3 with backup RPCs
def get_web3(chain_name: str, timeout: int = 5) -> Web3:
    """
    Trả về Web3 provider hoạt động được (ưu tiên RPC chính, sau đó backup).
    """
    urls = [RPC_URLS.get(chain_name)] + RPC_BACKUP_LIST.get(chain_name, [])
    urls = [u for u in urls if u]

    for rpc_url in urls:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': timeout}))
            if w3.is_connected():
                log.info(f"[OK] Connected to {chain_name} RPC: {rpc_url}")
                return w3
            else:
                log.warning(f"[WARN] {chain_name} RPC not responding: {rpc_url}")
        except Exception as e:
            log.error(f"[ERROR] {chain_name} RPC failed: {rpc_url} -> {e}")
        time.sleep(0.5)

    raise Exception(f"[FATAL] No working RPC found for {chain_name}")

# Get ABI of contract address
abi_memory_cache = {}
def get_abi(chain, contract_address):
    global abi_memory_cache
    key = f"{chain}_{contract_address.lower()}"

    # ✅ Ưu tiên dùng cache trong bộ nhớ
    if key in abi_memory_cache:
        log.info(f"✅ Loaded ABI from memory cache for {contract_address}")
        return abi_memory_cache[key]

    # ✅ Tạo đường dẫn cache file
    abi_cache_dir = "./abi_cached"
    os.makedirs(abi_cache_dir, exist_ok=True)
    cache_path = os.path.join(abi_cache_dir, f"{key}.json")

    # ✅ Nếu có file cache → dùng luôn
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r") as f:
                abi = json.load(f)
                abi_memory_cache[key] = abi  # cache vào bộ nhớ
                log.info(f"✅ Loaded ABI from file cache for {contract_address}")
                return abi
        except Exception as e:
            log.warning(f"⚠️ Error reading cached ABI: {e}, retrying from API...")

    # ✅ Nếu không có → gọi API
    if chain not in API_URLS or chain not in API_KEYS:
        log.warning(f"❌ No API URL or API Key for {chain}")
        return None

    etherscan_url = API_URLS[chain]
    params = {
        "module": "contract",
        "action": "getabi",
        "address": contract_address,
        "apikey": API_KEYS[chain]  # Bạn có thể sửa theo chain nếu cần
    }

    try:
        response = requests.get(etherscan_url, params=params)
        response_json = response.json()

        if response.status_code == 200 and response_json["status"] == "1":
            try:
                abi = json.loads(response_json["result"])
                abi_memory_cache[key] = abi  # cache vào bộ nhớ

                # ✅ Lưu vào file cache
                with open(cache_path, "w") as f:
                    json.dump(abi, f)

                log.info(f"✅ Fetched and cached ABI for {contract_address}")
                return abi
            except json.JSONDecodeError:
                log.error("❌ Error while decoding JSON ABI")
                return None
        else:
            log.error(f"❌ Failed to fetch ABI: {response_json.get('result')}")
            return None

    except requests.exceptions.RequestException as e:
        log.error(f"❌ Error retrieving contract ABI: {e}")
        return None

def get_contract(w3, contract_address, abi):
    if not abi:
        log.error(f"❌ No ABI provided for contract {contract_address}")
        return None
    return w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=abi)

# Get block of 6 months ago
def get_block_by_timestamp(chain, timestamp, retries=3, timeout=20):
    if chain not in API_URLS or chain not in API_KEYS:
        log.error(f"❌ No API URL or API Key for {chain}")
        return None
    
    url = API_URLS[chain]
    params = {
        "module": "block",
        "action": "getblocknobytime",
        "timestamp": timestamp,
        "closest": "before",
        "apikey": API_KEYS[chain]
    }

    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response_json = response.json()

            if response.status_code == 200 and response_json.get("status") == "1":
                return int(response_json["result"])
            else:
                log.warning(f"⚠️ Attempt {attempt}: API error {response_json.get('message')}, result={response_json.get('result')}")
        except requests.exceptions.Timeout:
            log.warning(f"⚠️ Attempt {attempt}: Timeout while fetching block by timestamp")
        except requests.exceptions.RequestException as e:
            log.warning(f"⚠️ Attempt {attempt}: Error fetching block by timestamp: {e}")

        # Backoff tránh spam API
        time.sleep(2 * attempt)

    log.error("❌ Failed to retrieve block by timestamp after retries")
    return None

def get_nft_txs_data(chain, wallet_address, contract_address, start_block, retries=3, timeout=30):
    """
    Lấy toàn bộ lịch sử giao dịch NFT, sử dụng chiến lược 'Block Walking' để vượt qua giới hạn 10k kết quả.
    Thay vì tăng Page, ta sẽ cập nhật start_block dựa trên giao dịch cuối cùng lấy được.
    """
    if chain not in API_URLS or chain not in API_KEYS:
        log.error(f"❌ No API URL or API Key for {chain}")
        return None
    
    url = API_URLS[chain]
    all_txs = []
    seen_hashes = set() # Để lọc trùng lặp khi overlap block
    
    current_start_block = start_block
    offset = 10000 # Max limit per request
    
    while True:
        # Luôn request page 1, nhưng startblock tịnh tiến dần
        params = {
            "module": "account",
            "action": "tokennfttx",
            "address": wallet_address,
            "startblock": current_start_block,
            "endblock": 999999999,
            "sort": "asc",
            "page": 1, 
            "offset": offset,
            "apikey": API_KEYS.get(chain, "")
        }

        if contract_address:
            params["contractaddress"] = contract_address

        batch_txs = None
        
        # Retry logic
        for attempt in range(1, retries + 1):
            try:
                response = requests.get(url, params=params, timeout=timeout)
                response_json = response.json()
                
                status = response_json.get("status")
                message = response_json.get("message")
                result = response_json.get("result")

                if response.status_code == 200:
                    if status == "1" and isinstance(result, list):
                        batch_txs = result
                        break
                    elif message == "No transactions found":
                        return all_txs # Đã hết dữ liệu
                    else:
                        log.warning(f"⚠️ Attempt {attempt}: API Error: {message}")
                else:
                    log.warning(f"⚠️ Attempt {attempt}: HTTP {response.status_code}")

            except requests.exceptions.Timeout:
                log.warning(f"⚠️ Attempt {attempt}: Timeout fetching block >= {current_start_block}")
            except requests.exceptions.RequestException as e:
                log.warning(f"⚠️ Attempt {attempt}: Request Error: {e}")

            time.sleep(2 * attempt)
        
        # Nếu sau retry mà vẫn không có data -> Dừng
        if batch_txs is None:
            log.error(f"❌ Failed to retrieve txs starting from block {current_start_block}. Returning collected data.")
            break

        # Xử lý dữ liệu lấy được
        new_items_count = 0
        for tx in batch_txs:
            # Tạo unique key để tránh trùng lặp khi startblock trùng với block cũ
            # Dùng hash + logIndex (nếu có) hoặc tokenID + transactionIndex
            unique_id = f"{tx.get('hash')}_{tx.get('tokenID')}_{tx.get('transactionIndex')}"
            
            if unique_id not in seen_hashes:
                seen_hashes.add(unique_id)
                all_txs.append(tx)
                new_items_count += 1
        
        log.info(f"📥 Fetched {len(batch_txs)} txs (New: {new_items_count}) from block {current_start_block}...")

        # Điều kiện dừng: Nếu số lượng lấy về nhỏ hơn offset -> Đã là mẻ cuối cùng
        if len(batch_txs) < offset:
            break
            
        # Cập nhật start_block cho vòng lặp sau
        last_tx = batch_txs[-1]
        last_block = int(last_tx['blockNumber'])
        
        # Logic cập nhật block để tránh lặp vô tận:
        if last_block == current_start_block:
            # Trường hợp hiếm: Cả 10000 txs đều nằm trong cùng 1 block -> Buộc phải nhảy qua block này
            # (Chấp nhận mất data trong block này vì API không hỗ trợ offset trong block)
            current_start_block = last_block + 1
        else:
            # Gán bằng last_block để lấy nốt các tx còn lại trong block đó (sẽ lọc trùng bằng seen_hashes)
            current_start_block = last_block
            
        # Nghỉ một chút để tránh rate limit
        time.sleep(0.5)

    return all_txs

MAX_RETRIES = 3
BACKOFF_INITIAL = 2  # seconds

def get_block_by_timestamp_moralis(chain: str, timestamp: int) -> int | None:
    url = "https://deep-index.moralis.io/api/v2/dateToBlock"
    
    # Chuyển timestamp sang UTC+7 (hoặc UTC nếu muốn)
    UTC_PLUS_7 = timezone(timedelta(hours=7))
    iso_date = datetime.fromtimestamp(timestamp, UTC_PLUS_7).isoformat()
    
    headers = {"X-API-Key": MORALIS_API_KEY}
    params = {"chain": chain, "date": iso_date}

    backoff = BACKOFF_INITIAL
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code == 429:
                log.warning(f"⚠️ Moralis rate limit exceeded (attempt {attempt}), retrying in {backoff}s...")
                time.sleep(backoff)
                backoff *= 2
                continue
            response.raise_for_status()
            data = response.json()
            
            if "block" in data:
                return int(data["block"])
            else:
                log.error(f"❌ Moralis không trả về block cho {chain}, {iso_date}: {data}")
                return None

        except requests.exceptions.RequestException as e:
            log.error(f"⚠️ Error calling Moralis (attempt {attempt}): {e}")
            time.sleep(backoff)
            backoff *= 2

    log.error("❌ Failed to get block after retries")
    return None

def get_nft_txs_data_moralis(chain, wallet_address, contract_address=None, start_block=None):
    base_url = f"https://deep-index.moralis.io/api/v2.2/{wallet_address}/nft/transfers"
    headers = {"X-API-Key": MORALIS_API_KEY}
    
    params = {
        "chain": chain,
        "format": "decimal",
        "limit": 100
    }
    
    if start_block:
        params["from_block"] = start_block

    all_results = []
    cursor = None

    while True:
        if cursor:
            params["cursor"] = cursor
        
        backoff = BACKOFF_INITIAL
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(base_url, headers=headers, params=params, timeout=10)
                if resp.status_code == 429:
                    log.warning(f"⚠️ Moralis rate limit exceeded (attempt {attempt}), retrying in {backoff}s...")
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                resp.raise_for_status()
                data = resp.json()
                break  # nếu thành công, thoát loop retry
            except requests.exceptions.RequestException as e:
                log.warning(f"⚠️ Error retrieving NFT txs (attempt {attempt}): {e}")
                time.sleep(backoff)
                backoff *= 2
        else:
            log.error("❌ Failed to retrieve NFT transactions after retries")
            return None

        if "result" in data:
            all_results.extend(data["result"])
        
        cursor = data.get("cursor")
        if not cursor:
            break

    return all_results

def get_current_owned_token_ids(tx_list, wallet_address, npm_address):
    """
    Phân tích log transaction để tìm ID đang sở hữu.
    Logic: "Vào là Của mình", "Ra mà không chết (Burn) thì vẫn là Của mình (đang Stake)".
    """
    owned = set()
    wallet_address = wallet_address.lower()
    null_address = "0x0000000000000000000000000000000000000000"
    
    for tx in tx_list:
        # Xử lý sự khác biệt tên trường giữa Etherscan và Moralis
        token_id = tx.get("tokenID", tx.get("token_id"))
        from_addr = tx.get("from", tx.get("from_address")).lower()
        to_addr = tx.get("to", tx.get("to_address")).lower()
        token_address = (
            tx.get("contractAddress")
            or tx.get("contract_address")
            or tx.get("token_address")
        )
        
        if token_address is not None:
            token_address = token_address.lower()
            if token_address != npm_address.lower():
                continue
        
        # 1. NFT đi vào ví (Mint, Unstake, Mua...) -> GHI NHẬN
        if to_addr.lower() == wallet_address:
            owned.add(token_id)
            
        # 2. NFT đi ra khỏi ví -> CHỈ XÓA KHI BURN (Chuyển về 0x00...0)
        # Nếu chuyển sang Gauge (Stake) hoặc ví khác -> Vẫn giữ trong list
        elif from_addr.lower() == wallet_address:
            if to_addr == null_address:
                owned.discard(token_id)
            # else: Token đang được Stake hoặc chuyển đi đâu đó, vẫn tính là sở hữu quyền lợi
            
    return owned

# Get position status
def get_position_status(liquidity, tick_lower, tick_upper, current_tick, tokens_owed0, tokens_owed1):
    if liquidity > 0:
        if tick_lower <= current_tick <= tick_upper:
            return "Active"
        else:
            return "Inactive"
    elif tokens_owed0 > 0 or tokens_owed1 > 0:
        return "Unclaimed"
    else:
        return "Burned"

# Calculate amount token0 and token1 from liquidity in smart contract Position
def get_current_amounts(liquidity, sqrt_price_x96, tick_lower, tick_upper):
    sqrt_price = float(sqrt_price_x96) / 2**96
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

# tODO HEAVY TASK
def get_nft_ids_by_all_status_aerodrome(
    w3,
    chain_name,
    chain_api,
    sorted_owned_token_ids,
    npm_contract,
    factory_contract,
    factory_address=None,
):
    active_nft_ids = []
    inactive_nft_ids = []
    unknown_nft_ids = []
    status_map = {}
    position_map = {}

    sorted_owned_token_ids = sorted(map(int, sorted_owned_token_ids))
    
    positions_data = get_positions_multicall_aerodrome(w3, sorted_owned_token_ids, chain_name, npm_contract)
    print(f"- Positions data: {positions_data}")

    pool_info_cache = {}
    pool_context_by_nft = {}
    pool_addresses = set()

    for nft_id, position_data in positions_data.items():
        position_map[str(nft_id)] = position_data

        token0 = Web3.to_checksum_address(position_data["token0"])
        token1 = Web3.to_checksum_address(position_data["token1"])
        tick_spacing = position_data["tickSpacing"]
        try:
            pool_key = (token0, token1, tick_spacing, factory_address or "")
            pool_info = pool_info_cache.get(pool_key)
            if pool_info is None:
                pool_info = get_aerodrome_pool_info(
                    chain_name,
                    token0,
                    token1,
                    tick_spacing,
                    factory_address=factory_address,
                )
                pool_info_cache[pool_key] = pool_info
            if not pool_info:
                log.warning("⚠️ Không lấy được thông tin pool.")
                status_map[str(nft_id)] = "Unknown"
                unknown_nft_ids.append(str(nft_id))
                continue

            pool_address = Web3.to_checksum_address(pool_info["pool_address"])
            pool_addresses.add(pool_address)
            pool_context_by_nft[nft_id] = pool_address
        except Exception as e:
            log.error(f"[Pool Error] NFT {nft_id} → {e}")
            status_map[str(nft_id)] = "Unknown"
            unknown_nft_ids.append(str(nft_id))

    def _fetch_pool_tick(pool_address):
        try:
            current_tick, sqrt_price_x96 = get_current_tick(
                w3,
                pool_address,
                ABI_SLOT0_AERODROME,
                rpc_list=RPC_BACKUP_LIST.get(chain_name, [])
            )
            return pool_address, current_tick, sqrt_price_x96
        except Exception as exc:
            log.error(f"[Pool Error] slot0 {pool_address} → {exc}")
            return pool_address, None, None

    pool_tick_cache = {}
    if pool_addresses:
        max_workers = min(8, len(pool_addresses))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for pool_address, current_tick, sqrt_price_x96 in ex.map(_fetch_pool_tick, sorted(pool_addresses)):
                if current_tick is not None:
                    pool_tick_cache[pool_address] = (current_tick, sqrt_price_x96)
                    log.info(f"✅ Pool Address: {pool_address} → Current Tick: {current_tick}")

    for nft_id, position_data in positions_data.items():
        pool_address = pool_context_by_nft.get(nft_id)
        if not pool_address:
            status_map.setdefault(str(nft_id), "Unknown")
            if str(nft_id) not in unknown_nft_ids:
                unknown_nft_ids.append(str(nft_id))
            continue

        liquidity = position_data["liquidity"]
        tick_lower = position_data["tickLower"]
        tick_upper = position_data["tickUpper"]
        tokens_owed0 = position_data["tokensOwed0"]
        tokens_owed1 = position_data["tokensOwed1"]

        try:
            pool_tick = pool_tick_cache.get(pool_address)
            if pool_tick is None:
                current_tick, sqrt_price_x96 = get_current_tick(
                    w3,
                    pool_address,
                    ABI_SLOT0_AERODROME,
                    rpc_list=RPC_BACKUP_LIST.get(chain_name, [])
                )
                pool_tick_cache[pool_address] = (current_tick, sqrt_price_x96)
                log.info(f"✅ Pool Address: {pool_address} → Current Tick: {current_tick}")
            else:
                current_tick, sqrt_price_x96 = pool_tick

            status_position = get_position_status(
                liquidity, tick_lower, tick_upper, current_tick,
                tokens_owed0, tokens_owed1
            )
        except Exception as e:
            log.error(f"[Pool Error] NFT {nft_id} → {e}")
            status_position = "Unknown"

        status_map[str(nft_id)] = status_position
        if status_position == 'Active':
            active_nft_ids.append(str(nft_id))
        elif status_position == 'Inactive':
            inactive_nft_ids.append(str(nft_id))
        elif status_position == "Unknown":
            unknown_nft_ids.append(str(nft_id))

    return active_nft_ids, inactive_nft_ids, unknown_nft_ids, status_map, position_map

def notify_inactive_nft(nft_id, chain_name, wallet_address, token0_name, token1_name, current_token0_amount, current_token1_amount, current_amount, farm_apr):
    """
    Notify khi NFT position bị chuyển sang trạng thái Inactive.
    """
    # Format số cho đẹp
    current_token0_amount_fmt = f"{current_token0_amount:,.3f}"
    current_token1_amount_fmt = f"{current_token1_amount:,.3f}"
    current_amount_fmt = f"${current_amount:,.2f}"
    farm_apr_fmt = f"{farm_apr:.2f}"

    # Tạo URL
    nft_url = f"https://aerodrome.finance/dash"
    wallet_url = f"{CHAIN_SCAN_URLS[chain_name]}{wallet_address}"

    # Gửi Discord
    send_discord_webhook_message(
        f'ID [{nft_id}]({nft_url}) {chain_name} '
        f'(({token0_name} {current_token0_amount_fmt})-({token1_name} {current_token1_amount_fmt}), '
        f'{current_amount_fmt}, {farm_apr_fmt}%) '
        f'[Wallet {wallet_address[:6]}...{wallet_address[-4:]}]({wallet_url}) ✅ Active ➜ ❌ Inactive.'
    )

def get_pending_fees_aerodrome(w3, pool_address, position_data, token0_decimals, token1_decimals, current_tick=None):
    try:
        # --- CẬP NHẬT ABI CHUẨN CHO AERODROME SLIPSTREAM ---
        AERODROME_POOL_ABI = [
            # 1. slot0: Bỏ 'feeProtocol' (chỉ còn 6 output)
            {
                "inputs": [],
                "name": "slot0",
                "outputs": [
                    {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
                    {"internalType": "int24", "name": "tick", "type": "int24"},
                    {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
                    {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
                    {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
                    {"internalType": "bool", "name": "unlocked", "type": "bool"} 
                ],
                "stateMutability": "view",
                "type": "function"
            },
            # 2. ticks: Thêm 'stakedLiquidityNet' và 'rewardGrowthOutsideX128'
            {
                "inputs": [{"internalType": "int24", "name": "tick", "type": "int24"}],
                "name": "ticks",
                "outputs": [
                    {"internalType": "uint128", "name": "liquidityGross", "type": "uint128"},
                    {"internalType": "int128", "name": "liquidityNet", "type": "int128"},
                    {"internalType": "int128", "name": "stakedLiquidityNet", "type": "int128"}, # <-- Mới
                    {"internalType": "uint256", "name": "feeGrowthOutside0X128", "type": "uint256"},
                    {"internalType": "uint256", "name": "feeGrowthOutside1X128", "type": "uint256"},
                    {"internalType": "uint256", "name": "rewardGrowthOutsideX128", "type": "uint256"}, # <-- Mới
                    {"internalType": "int56", "name": "tickCumulativeOutside", "type": "int56"},
                    {"internalType": "uint160", "name": "secondsPerLiquidityOutsideX128", "type": "uint160"},
                    {"internalType": "uint32", "name": "secondsOutside", "type": "uint32"},
                    {"internalType": "bool", "name": "initialized", "type": "bool"}
                ],
                "stateMutability": "view",
                "type": "function"
            },
            # Global Fee Growth (Giữ nguyên)
            {"inputs": [], "name": "feeGrowthGlobal0X128", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
            {"inputs": [], "name": "feeGrowthGlobal1X128", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}
        ]

        pool_address = Web3.to_checksum_address(pool_address)
        pool_contract = w3.eth.contract(address=pool_address, abi=AERODROME_POOL_ABI)

        tick_lower = position_data['tickLower']
        tick_upper = position_data['tickUpper']
        fee_growth_global_0 = None
        fee_growth_global_1 = None
        tick_info_lower = None
        tick_info_upper = None

        try:
            mc = W3Multicall(w3)
            result_keys = []

            if current_tick is None:
                mc.add(W3Multicall.Call(pool_address, "slot0()(uint160,int24,uint16,uint16,uint16,bool)"))
                result_keys.append("slot0")

            mc.add(W3Multicall.Call(pool_address, "feeGrowthGlobal0X128()(uint256)"))
            result_keys.append("fee_growth_global_0")
            mc.add(W3Multicall.Call(pool_address, "feeGrowthGlobal1X128()(uint256)"))
            result_keys.append("fee_growth_global_1")
            mc.add(W3Multicall.Call(pool_address, "ticks(int24)(uint128,int128,int128,uint256,uint256,uint256,int56,uint160,uint32,bool)", tick_lower))
            result_keys.append("tick_info_lower")
            mc.add(W3Multicall.Call(pool_address, "ticks(int24)(uint128,int128,int128,uint256,uint256,uint256,int56,uint160,uint32,bool)", tick_upper))
            result_keys.append("tick_info_upper")

            mc_results = dict(zip(result_keys, mc.call()))
            if current_tick is None:
                current_tick = mc_results["slot0"][1]
            fee_growth_global_0 = mc_results["fee_growth_global_0"]
            fee_growth_global_1 = mc_results["fee_growth_global_1"]
            tick_info_lower = mc_results["tick_info_lower"]
            tick_info_upper = mc_results["tick_info_upper"]
        except Exception as mc_error:
            log.warning(f"Multicall pending fee fallback failed for pool {pool_address}: {mc_error}")

        # --- LOGIC TÍNH TOÁN (Giữ nguyên thuật toán, chỉ thay đổi index lấy data) ---
        
        # 1. Get Slot0
        if current_tick is None:
            slot0 = pool_contract.functions.slot0().call()
        else:
            slot0 = (None, current_tick)
        current_tick = slot0[1] # Index 1 là tick
        
        # 2. Get Global Fee
        if fee_growth_global_0 is None:
            fee_growth_global_0 = pool_contract.functions.feeGrowthGlobal0X128().call()
        if fee_growth_global_1 is None:
            fee_growth_global_1 = pool_contract.functions.feeGrowthGlobal1X128().call()

        # 3. Get Ticks Info
        tick_lower = position_data['tickLower']
        tick_upper = position_data['tickUpper']
        
        if tick_info_lower is None:
            tick_info_lower = pool_contract.functions.ticks(tick_lower).call()
        if tick_info_upper is None:
            tick_info_upper = pool_contract.functions.ticks(tick_upper).call()

        # Lưu ý: Index trong mảng ticks bây giờ đã thay đổi do thêm biến mới
        # Index 3: feeGrowthOutside0X128
        # Index 4: feeGrowthOutside1X128
        fee_growth_outside_0_lower = tick_info_lower[3]
        fee_growth_outside_1_lower = tick_info_lower[4]
        
        fee_growth_outside_0_upper = tick_info_upper[3]
        fee_growth_outside_1_upper = tick_info_upper[4]

        # 4. Tính toán Fee Inside (Giữ nguyên logic cũ)
        def get_fee_growth_inside(fee_global, fee_outside_lower, fee_outside_upper, tick_current, t_lower, t_upper):
            if tick_current >= t_lower:
                fee_below = fee_outside_lower
            else:
                fee_below = fee_global - fee_outside_lower

            if tick_current < t_upper:
                fee_above = fee_outside_upper
            else:
                fee_above = fee_global - fee_outside_upper

            fee_inside = fee_global - fee_below - fee_above
            if fee_inside < 0: fee_inside += 2**256
            return fee_inside

        fee_growth_inside_0_current = get_fee_growth_inside(
            fee_growth_global_0, fee_growth_outside_0_lower, fee_growth_outside_0_upper, 
            current_tick, tick_lower, tick_upper
        )
        
        fee_growth_inside_1_current = get_fee_growth_inside(
            fee_growth_global_1, fee_growth_outside_1_lower, fee_growth_outside_1_upper, 
            current_tick, tick_lower, tick_upper
        )

        # 5. Final Calculation
        liquidity = position_data['liquidity']
        fee_growth_inside_0_last = position_data['feeGrowthInside0LastX128']
        fee_growth_inside_1_last = position_data['feeGrowthInside1LastX128']

        diff_0 = fee_growth_inside_0_current - fee_growth_inside_0_last
        if diff_0 < 0: diff_0 += 2**256
        
        diff_1 = fee_growth_inside_1_current - fee_growth_inside_1_last
        if diff_1 < 0: diff_1 += 2**256

        unclaimed_0_raw = (liquidity * diff_0) // (2**128) # Dùng // cho phép chia nguyên
        unclaimed_1_raw = (liquidity * diff_1) // (2**128)

        fee_0 = unclaimed_0_raw / (10 ** token0_decimals)
        fee_1 = unclaimed_1_raw / (10 ** token1_decimals)
        log.info(f"✅ Pending Fees: Token0={fee_0}, Token1={fee_1}")
        
        return fee_0, fee_1

    except Exception as e:
        print(f"❌ Error Aerodrome Math: {e}")
        import traceback
        traceback.print_exc()
        return 0, 0

def process_nft_mint_data_evm_aerodrome(chain_name, wallet_address, nft_id, status_map, position_map, 
                            w3, npm_address, npm_contract, npm_abi, inactived_nft_ids, aero_price, mode,
                            slot0_cache=None, factory_address=None):
    
    log.info(f"🔍 Processing NFT ID {nft_id} on {chain_name} for wallet {wallet_address}")
    try:
        if not factory_address:
            factory_address = _get_aerodrome_factory_address(chain_name, npm_address)
        log.info(
            f"Processing Aerodrome NFT {nft_id} on {chain_name} for wallet {wallet_address} "
            f"npm={npm_address} factory={factory_address}"
        )
        # position_data = npm_contract.functions.positions(int(nft_id)).call()
        position_data = position_map.get(str(nft_id))
        if not position_data:
            position_data = call_with_fallback(
                npm_contract.functions.positions(int(nft_id)),
                RPC_BACKUP_LIST.get(chain_name, []),
                contract_abi=npm_abi,
                w3_main=w3
            )
            if position_data:
                operator = Web3.to_checksum_address(position_data[1])
                token0 = Web3.to_checksum_address(position_data[2])
                token1 = Web3.to_checksum_address(position_data[3])
                tick_spacing = position_data[4]
                tick_lower = position_data[5]
                tick_upper = position_data[6]
                liquidity = position_data[7]
                tokens_owed0 = position_data[10]
                tokens_owed1 = position_data[11]
        else:
            operator = Web3.to_checksum_address(position_data['operator'])
            token0 = Web3.to_checksum_address(position_data['token0'])
            token1 = Web3.to_checksum_address(position_data['token1'])
            tick_spacing = position_data['tickSpacing']
            liquidity = position_data['liquidity']
            tick_lower = position_data['tickLower']
            tick_upper = position_data['tickUpper']
        
        pool_info = get_aerodrome_pool_info(
            chain_name,
            token0,
            token1,
            tick_spacing,
            factory_address=factory_address,
        )
        if not pool_info:
            log.warning("⚠️ Không lấy được thông tin pool.")
            return

        POOL_ADDRESS = pool_info["pool_address"]
        log.info(f"Resolved Aerodrome pool={POOL_ADDRESS} factory={factory_address}")
        token0_symbol = pool_info["token0_symbol"]
        token1_symbol = pool_info["token1_symbol"]
        token0_decimal = pool_info["token0_decimals"]
        token1_decimal = pool_info["token1_decimals"]
        pool_gauge_address = pool_info["gauge_address"]
        log.info(f"🔍 Pool Address: {POOL_ADDRESS}, Gauge Address: {pool_gauge_address}, Token0: {token0_symbol}, Token1: {token1_symbol}")
        
        # --- LẤY THÔNG TIN THANH KHOẢN POOL (Dùng cho Hybrid Snapshot) ---
        total_active_staked_usd = 0
        total_pool_liquidity_usd = 0
        if pool_info:
            total_active_staked_usd = float(pool_info.get("total_staked_liquidity", 0))
            total_pool_liquidity_usd = float(pool_info.get("total_value_lock", 0))
            
        log.info(f" - Snapshot Liquidity Aerodrome: Active Stake=${total_active_staked_usd}, Total Liq=${total_pool_liquidity_usd}")
        
        pool_abi = get_abi(chain_name, Web3.to_checksum_address(POOL_ADDRESS))
        pool_contract = w3.eth.contract(address=Web3.to_checksum_address(POOL_ADDRESS), abi=pool_abi)
        
        if pool_gauge_address is None:
            log.warning("⚠️ Pool gauge address is None.")
            return None
        
        gauge_abi = get_abi(chain_name, Web3.to_checksum_address(pool_gauge_address))
        gauge_contract = w3.eth.contract(address=Web3.to_checksum_address(pool_gauge_address), abi=gauge_abi)
            
        # --- OPTIMIZATION STEP 4: SLOT0 CACHE PER-POOL ---
        # Tránh việc gọi RPC lặp lại cho cùng một pool trong cùng một phiên quét.
        # Nếu pool đã được lấy slot0 trước đó, ta dùng kết quả từ cache.
        # Step 4: Kiểm tra cache trước khi gọi RPC slot0 (tránh lặp lại call cho cùng 1 pool)
        _pool_key = POOL_ADDRESS.lower()
        if slot0_cache is not None and _pool_key in slot0_cache:
            slot0 = slot0_cache[_pool_key]
            log.info(f"✅ slot0 cache hit for pool {_pool_key[:10]}...")
        else:
            slot0 = call_with_fallback(
                pool_contract.functions.slot0(),
                RPC_BACKUP_LIST.get(chain_name, []),
                contract_abi=pool_abi,
                w3_main=w3
            )
            if slot0 is not None and slot0_cache is not None:
                slot0_cache[_pool_key] = slot0
        if slot0 is None:
            log.warning("⚠️ Không lấy được slot0.")
            return None
        
        sqrt_price_x96 = slot0[0]
        current_tick = slot0[1]
        log.info(f"✅ Pool slot0: sqrtPriceX96={sqrt_price_x96}, current_tick={current_tick}")
        
        # Get reward per second of each pool
        reward_per_second_pool_raw, reward_decimals = get_rewards_per_second_of_aerodrome_pool(pool_address=POOL_ADDRESS, chain=chain_name)
        reward_per_second_pool = ((reward_per_second_pool_raw / (10 ** reward_decimals)) / 86400) if reward_per_second_pool_raw is not None else 0
        log.info(f"🍰 Aero per second for pool {POOL_ADDRESS}: {reward_per_second_pool}")
        
        status_position = status_map.get(str(nft_id), "Unknown")
        is_active = 1 if status_position == 'Active' else 0

        # --- OPTIMIZATION STEP 3: SONG SONG HÓA 4 ETHERSCAN CALLS ---
        # Sử dụng ThreadPoolExecutor để fetch 4 loại event history đồng thời.
        # Giảm thời gian xử lý mỗi NFT từ 4x latency xuống còn 1x latency lớn nhất.
        # --- Get event-log history song song (4 Etherscan calls độc lập) ---
        _gauge_for_stake = pool_gauge_address if pool_gauge_address else NPM_ADDRESSES.get(chain_name, npm_address)
        with ThreadPoolExecutor(max_workers=4) as _ex:
            _f_inc = _ex.submit(
                safe_api_call, get_increase_liquidity_history,
                API_URLS, API_KEYS, chain_name, npm_address, int(nft_id),
                mode=mode, retry_empty_results=(mode != "auto"), default=([], None, None, 0, 0)
            )
            _f_dec = _ex.submit(
                safe_api_call, get_decrease_liquidity_history,
                API_URLS, API_KEYS, chain_name, npm_address, int(nft_id),
                default=([], None, None, 0, 0)
            )
            _f_stk = _ex.submit(
                safe_api_call, get_stake_time,
                API_URLS, API_KEYS, chain_name, _gauge_for_stake, int(nft_id),
                default=None
            )
            _f_col = _ex.submit(
                safe_api_call, get_last_collect_time,
                API_URLS, API_KEYS, chain_name, npm_address, int(nft_id),
                default=None
            )
            mint_transactions, latest_time_add, first_time_add, initial_amount_token0, initial_amount_token1 = _f_inc.result()
            decrease_transaction, latest_time_decrease, first_time_decrease, decrease_amount_token0, decrease_amount_token1 = _f_dec.result()
            latest_time_stake = _f_stk.result()
            latest_time_collect = _f_col.result()

        log.info(f"✅ Latest time: add={latest_time_add}, collect={latest_time_collect}, decrease={latest_time_decrease}, stake={latest_time_stake}")
        
        # --- Convert thời gian sang timestamp an toàn ---
        tz_vn = timezone(timedelta(hours=7))
        now_vn = datetime.now(tz_vn)
        time_current = int(now_vn.timestamp())
        created_at_for_db = now_vn.replace(tzinfo=None)
        future_limit_ts = time_current + 300

        def _db_datetime_to_vn_timestamp(value, label):
            if value is None:
                return None

            try:
                if isinstance(value, datetime):
                    value_dt = value
                else:
                    value_dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")

                if value_dt.tzinfo is None or value_dt.tzinfo.utcoffset(value_dt) is None:
                    value_dt = value_dt.replace(tzinfo=tz_vn)
                else:
                    value_dt = value_dt.astimezone(tz_vn)

                return int(value_dt.timestamp())
            except Exception as exc:
                log.warning(f"Cannot parse {label} timestamp for NFT {nft_id}: {value} ({exc})")
                return None

        def _valid_elapsed_minutes(label, previous_ts):
            if previous_ts is None:
                log.warning(f"Cannot compute {label} for NFT {nft_id}: missing previous timestamp")
                return None
            if previous_ts > future_limit_ts:
                log.warning(
                    f"Cannot compute {label} for NFT {nft_id}: previous timestamp is in the future "
                    f"({datetime.fromtimestamp(previous_ts, tz=tz_vn)})"
                )
                return None

            elapsed_minutes = (time_current - previous_ts) / 60
            if elapsed_minutes <= 0:
                log.warning(
                    f"Cannot compute {label} for NFT {nft_id}: non-positive elapsed time "
                    f"({elapsed_minutes} minutes)"
                )
                return None

            return max(elapsed_minutes, 1)

        latest_time_add_ts = safe_to_timestamp_with_fallback(
            latest_time_add,
            int(nft_id),
            chain_name,
            wallet_address,
            "date_add_liquidity",
            assume_db_tz=tz_vn,
            reject_future=True,
            reject_after_created=True,
            future_tolerance_seconds=300,
            type_dex="aerodrome",
            npm_address=npm_address,
            pool_address=POOL_ADDRESS,
        )
        latest_time_collect_ts = safe_to_timestamp(latest_time_collect) or 0
        latest_time_decrease_ts = safe_to_timestamp(latest_time_decrease) or 0
        latest_time_stake_ts = safe_to_timestamp(latest_time_stake) or 0

        def _reject_future_timestamp(label, ts):
            if ts and ts > future_limit_ts:
                log.warning(
                    f"Ignore future {label} timestamp for NFT {nft_id}: "
                    f"{datetime.fromtimestamp(ts, tz=tz_vn)}"
                )
                return 0
            return ts

        latest_time_add_ts = _reject_future_timestamp("add", latest_time_add_ts)
        latest_time_collect_ts = _reject_future_timestamp("collect", latest_time_collect_ts)
        latest_time_decrease_ts = _reject_future_timestamp("decrease", latest_time_decrease_ts)
        latest_time_stake_ts = _reject_future_timestamp("stake", latest_time_stake_ts)
        
        log.info(f"✅ Timestamps: add={latest_time_add_ts}, collect={latest_time_collect_ts}, decrease={latest_time_decrease_ts}, stake={latest_time_stake_ts}")

        # --- Tính max thời gian ---
        max_latest_time_add_collect_ts = max(latest_time_add_ts, latest_time_collect_ts)
        max_latest_time_add_remove_ts = max(latest_time_add_ts, latest_time_decrease_ts)
        max_latest_time_add_stake_ts = max(latest_time_add_ts, latest_time_stake_ts)

        log.info(f"✅ Latest timestamps: add={latest_time_add_ts}, collect={latest_time_collect_ts}, decrease={latest_time_decrease_ts}, stake={latest_time_stake_ts}")
        log.info(f"✅ Max timestamps: add_collect={max_latest_time_add_collect_ts}, add_remove={max_latest_time_add_remove_ts}, add_stake{max_latest_time_add_stake_ts}")
        
        # Get token price of token0 and token1
        price_token0 = get_price_tokens(chain_name, token0, current_tick, token0, token1, token0_decimal, token1_decimal) or 0
        price_token1 = get_price_tokens(chain_name, token1, current_tick, token0, token1, token0_decimal, token1_decimal) or 0
        log.info(f"🔍 Price token0: {price_token0}, token1: {price_token1}")
        
        has_invalid_price = (not price_token0 or price_token0 <= 0 or
                    not price_token1 or price_token1 <= 0)
        
        # Initial Amount tokens and total liquidity
        if initial_amount_token0 == 0 or initial_amount_token1 == 0:
            log.warning(f"⚠️ API initial amount = 0 → fallback DB cho NFT {nft_id}")
            db_result = get_nft_initial_amount_from_db(
                nft_id,
                chain_name,
                wallet_address,
                type_dex="aerodrome",
                npm_address=npm_address,
                pool_address=POOL_ADDRESS,
            )
            if db_result:
                initial_amount_token0, initial_amount_token1 = db_result
            else:
                initial_amount_token0, initial_amount_token1 = 0, 0
            
            initial_amount_token0_decimal = initial_amount_token0
            initial_amount_token1_decimal = initial_amount_token1
            log.info(f"🔍 (DB)Initial amount token0: {initial_amount_token0}, token1: {initial_amount_token1}")
            
        else:
            log.info(f"✅ API trả initial amount hợp lệ cho NFT {nft_id}")
            initial_amount_token0_decimal = initial_amount_token0 / 10**token0_decimal
            initial_amount_token1_decimal = initial_amount_token1 / 10**token1_decimal
        
        # Decrease amount tokens and total liquidity
        decrease_amount_token0_decimal = decrease_amount_token0 / 10**token0_decimal
        decrease_amount_token1_decimal = decrease_amount_token1 / 10**token1_decimal
        
        delta_initial_amount_token0_decimal = initial_amount_token0_decimal - decrease_amount_token0_decimal
        delta_initial_amount_token1_decimal = initial_amount_token1_decimal - decrease_amount_token1_decimal
        
        price_initial_amount_token0 = delta_initial_amount_token0_decimal * price_token0
        price_initial_amount_token1 = delta_initial_amount_token1_decimal * price_token1
        total_initial_amount_token = price_initial_amount_token0 + price_initial_amount_token1
        
        # Current Amount tokens and total liquidity
        current_amount_token0, current_amount_token1 = get_current_amounts(liquidity, sqrt_price_x96, tick_lower, tick_upper)
        amount_token0_decimal = current_amount_token0 / 10**token0_decimal
        amount_token1_decimal = current_amount_token1 / 10**token1_decimal
        price_current_amount_token0 = price_token0 * float(amount_token0_decimal)
        price_current_amount_token1 = price_token1 * float(amount_token1_decimal)
        total_current_amount_token = price_current_amount_token0 + price_current_amount_token1
        log.info(f"- Total current amount token: {total_current_amount_token}")

        # Delta of initial and current amount    
        delta_amount_token0 = float(amount_token0_decimal) - delta_initial_amount_token0_decimal
        delta_amount_token1 = float(amount_token1_decimal) - delta_initial_amount_token1_decimal
        delta_initial_current_amount = (delta_amount_token0*price_token0) + (delta_amount_token1*price_token1)
        log.info(f"- Delta initial current amount: {delta_initial_current_amount}")
        
        denominator = total_current_amount_token - delta_initial_current_amount
        log.info(f"🔍 Denominator: {denominator}")
        if denominator and abs(denominator) > 1e-6:
            percent_delta = (delta_initial_current_amount / denominator) * 100
        else:
            percent_delta = 0
        
        # Amount tokens unclaimed and Unclaimed fees
        # fees = npm_contract.functions.collect(
        #     (int(nft_id), operator, 2**128-1, 2**128-1)
        # ).call()
        fees = call_with_fallback(
            npm_contract.functions.collect((int(nft_id), operator, 2**128-1, 2**128-1)),
            RPC_BACKUP_LIST.get(chain_name, []),
            contract_abi=npm_abi,
            w3_main=w3
        )
        log.info(f"✅ Collected fees from contract: {fees}")
        
        if fees is None or fees[0] == 0 and fees[1] == 0:
            log.warning(f"⚠️ API collect fee = None")
            unclaimed_fee_token0, unclaimed_fee_token1 = get_pending_fees_aerodrome(
                w3, 
                POOL_ADDRESS,
                position_data,
                token0_decimal, 
                token1_decimal,
                current_tick=current_tick
            )
            unclaimed_fee_token0 = unclaimed_fee_token0 or 0
            unclaimed_fee_token1 = unclaimed_fee_token1 or 0
            total_unclaimed_fee_token = (unclaimed_fee_token0*price_token0) + (unclaimed_fee_token1*price_token1)
            log.info(f"🔍 (Fallback) Unclaimed fee token0: {unclaimed_fee_token0}, token1: {unclaimed_fee_token1}, total unclaimed fee token: {total_unclaimed_fee_token}")
        else:
            unclaimed_fee_token0 = fees[0] / 10**token0_decimal
            unclaimed_fee_token1 = fees[1] / 10**token1_decimal
            total_unclaimed_fee_token = (unclaimed_fee_token0*price_token0) + (unclaimed_fee_token1*price_token1)
            log.info(f"🔍 Unclaimed fee token0: {unclaimed_fee_token0}, token1: {unclaimed_fee_token1}, total unclaimed fee token: {total_unclaimed_fee_token}")
        
        # Get delta time
        time_current_formated = datetime.fromtimestamp(time_current, tz=tz_vn)
        log.info(f"📅 Time Current: {time_current_formated}")

        vietnam_time_current_formatted = created_at_for_db
        log.info(f"📅 Time Current: {vietnam_time_current_formatted}")
        
        # Calculate time instance
        delta_time = time_current - max_latest_time_add_collect_ts
        delta_time_in_day = delta_time / 60 # minutes
        safe_minutes = delta_time_in_day if delta_time_in_day >= 1 else 1  # at least 1 minute
        # print(f"📅 Time Elapsed add liquidity: {round(delta_time_in_day, 2)} days")
    
        if total_current_amount_token and abs(total_current_amount_token) > 1e-6:
            lp_fee_apr = ((total_unclaimed_fee_token / safe_minutes * 60 * 24 * 365) / total_current_amount_token) * 100
        else:
            lp_fee_apr = 0
        
        ### LP FEE APR 1H ###
        fee_data = get_last_unclaimed_fee_token(
            int(nft_id),
            wallet_address=wallet_address,
            chain=chain_name,
            type_dex="aerodrome",
            npm_address=npm_address,
        )
        if fee_data:
            try:
                unclaimed_fee_token0_ago = float(fee_data["unclaimed_fee_token0"])
            except (ValueError, TypeError):
                unclaimed_fee_token0_ago = 0.0

            try:
                unclaimed_fee_token1_ago = float(fee_data["unclaimed_fee_token1"])
            except (ValueError, TypeError):
                unclaimed_fee_token1_ago = 0.0

            created_at = fee_data["created_at"]
            log.info(f"- Unclaimed fee token0 ago: {unclaimed_fee_token0_ago}, Unclaimed fee token1 ago: {unclaimed_fee_token1_ago}, Created at: {created_at}")
            
            delta_unclaimed_fee_token0 = unclaimed_fee_token0 - unclaimed_fee_token0_ago
            delta_unclaimed_fee_token1 = unclaimed_fee_token1 - unclaimed_fee_token1_ago
            total_delta_fee_usd = delta_unclaimed_fee_token0 * price_token0 + delta_unclaimed_fee_token1 * price_token1
            log.info(f"- Total delta fee usd: {total_delta_fee_usd}")
            
            created_at_ts = _db_datetime_to_vn_timestamp(created_at, "fee APR 1h created_at")
            safe_time_minutes = _valid_elapsed_minutes("fee APR 1h", created_at_ts)
            log.info(f"- Time Elapsed fee apr 1h: {safe_time_minutes}")

            if safe_time_minutes is None:
                lp_fee_apr_1h = 0
            elif denominator and abs(denominator) > 1e-6:
                lp_fee_apr_1h = (total_delta_fee_usd / safe_time_minutes * 60 * 24 * 365) / denominator * 100
            else:
                lp_fee_apr_1h = 0
        else:
            log.warning(f"Cannot compute fee APR 1h for NFT {nft_id}: missing previous fee snapshot")
            lp_fee_apr_1h = 0
        
        # Get Reward 
        log.info(f"🔎 Checking NFT ID: {nft_id} ({type(nft_id)}) - repr: {repr(nft_id)}")
        
        if gauge_contract is None and gauge_abi is None:
            log.error("❌ Gauge contract is None.")
            pending_cake = None
        else:
            pending_cake = call_with_fallback(
                gauge_contract.functions.earned(wallet_address, int(nft_id)),
                RPC_BACKUP_LIST.get(chain_name, []),
                contract_abi=gauge_abi,
                w3_main=w3
            )
        
        if pending_cake is None:
            log.warning(f"⚠️ API pending cake = None")
            pending_cake = 0
        
        pending_cake_amount = round((pending_cake/10**18), 6)
        pending_cake_price = round((pending_cake/10**18) * aero_price, 6)
        log.info(f"🎉 Pending Cake: {pending_cake_amount} ({pending_cake_price} USD)")
        
        # user_position_infos = masterchef_contract.functions.userPositionInfos(int(nft_id)).call()
        boost = 1.0
        log.info(f"🔍 Boost: {boost}")
        
        ### Time latest stake liquidity
        
        # if latest_time_stake:
        #     latest_time_stake = datetime.strptime(latest_time_stake, "%m-%d-%Y %H:%M:%S")
        #     latest_time_stake_timestamp = int(latest_time_stake.timestamp())
        # else:
        #     latest_time_stake_timestamp = 0
        
        time_elapsed_stake_days = (time_current - max_latest_time_add_stake_ts) / (3600 * 24)
        log.info(f"⏳ Time Elapsed stake liquidity: {round(time_elapsed_stake_days)} days")
        
        # Farm APR All
        if denominator and abs(denominator) > 1e-6 and time_elapsed_stake_days > 0:
            apr_all = (((pending_cake_price / time_elapsed_stake_days) * 365) / denominator * 100) * boost
        else:
            apr_all = 0
        
        ### FARM APR 1H ###
        pending_cake_info = get_last_pending_cake_info(
            int(nft_id),
            wallet_address=wallet_address,
            chain=chain_name,
            type_dex="aerodrome",
            npm_address=npm_address,
        )
        last_pending_cake_timestamp = None
        pending_cake_ago = 0.0
        delta_time_hour = None
        delta_pending_cake_amount = 0.0
        log.info(f"Pending_cake_info={pending_cake_info} ({type(pending_cake_info)}) repr={repr(pending_cake_info)}")
        
        if pending_cake_info:
            pending_cake_ago = pending_cake_info.get("pending_cake", 0.0)
            last_pending_cake_timestamp = pending_cake_info.get("created_at", None)
            log.info(f"⏳ Time Elapsed pending CAKE ago: {last_pending_cake_timestamp}")
            
            last_pending_cake_ts = _db_datetime_to_vn_timestamp(
                last_pending_cake_timestamp,
                "farm APR 1h created_at"
            )
            delta_time_hour = _valid_elapsed_minutes("farm APR 1h", last_pending_cake_ts)
                
            log.info(f"⏳ Time Elapsed pending CAKE: {delta_time_hour} minutes")
            log.info(f"📉 total current amount: ${total_current_amount_token}")
                
            delta_pending_cake_amount = pending_cake_amount - pending_cake_ago
            log.info(f"📉 Pending CAKE Reward: ${pending_cake_amount}")
            log.info(f"📉 Pending CAKE Reward ago: ${pending_cake_ago}")
            log.info(f"📉 Delta pending cake amount: {delta_pending_cake_amount} %")
            
            if delta_time_hour is None:
                apr_1h = 0
            elif denominator and abs(denominator) > 1e-6:
                apr_1h = (delta_pending_cake_amount * aero_price / delta_time_hour * 60 * 24 * 365) / denominator * 100
            elif denominator == 0:
                apr_1h = (delta_pending_cake_amount * aero_price / delta_time_hour * 60 * 24 * 365) / total_current_amount_token * 100
            else:
                apr_1h = 0
        else:
            log.warning(f"Cannot compute farm APR 1h for NFT {nft_id}: missing previous reward snapshot")
            apr_1h = 0
        
        if max_latest_time_add_remove_ts == 0:
            latest_time_add_datetime = None
        else:
            latest_time_add_datetime = datetime.fromtimestamp(max_latest_time_add_remove_ts, tz=tz_vn)
            
        log.info(f"⏳ Time Elapsed add liquidity: {latest_time_add_datetime}")
        
        ### CAKE REWARD 1H ###
        if reward_per_second_pool > 0 and delta_time_hour and delta_time_hour > 0:
            cake_reward_1h = float(delta_pending_cake_amount) / (float(reward_per_second_pool) * (delta_time_hour * 60)) * 100
            log.info(f"📉 Cake Reward 1h: {cake_reward_1h} CAKE")
        else:
            cake_reward_1h = 0
            log.warning(f"⚠️ Cannot compute Cake Reward: reward_per_second_pool={reward_per_second_pool}, delta_time_hour={delta_time_hour}")
            
        ### FEE APR 1H ###
        wallet_url_db = f"{CHAIN_SCAN_URLS[chain_name]}{wallet_address}"
        nft_url_db = f"https://aerodrome.finance/dash"
        
        # Tick deviation 
        position_tick_lower = tick_lower
        position_tick_upper = tick_upper
        pool_current_tick = current_tick
        log.info(f"🔍 Ticks: position lower={position_tick_lower}, position upper={position_tick_upper}, pool current={pool_current_tick}")
        
        if inactived_nft_ids:
            if nft_id in inactived_nft_ids:
                if ((amount_token0_decimal <= 0 and amount_token1_decimal > 0) or
                    (amount_token1_decimal <= 0 and amount_token0_decimal > 0)):
                    
                    notify_inactive_nft(nft_id, chain_name, wallet_address, token0_symbol, token1_symbol, amount_token0_decimal, amount_token1_decimal, total_current_amount_token, apr_all)
                else:
                    status_position = "Active"
                    is_active = 1
        
        data_nft = (
            wallet_address,
            chain_name,
            nft_id,
            token0_symbol,
            token1_symbol,
            POOL_ADDRESS,
            price_token0,
            price_token1,
            status_position,
            latest_time_add_datetime,
            delta_initial_amount_token0_decimal,
            delta_initial_amount_token1_decimal,
            round(total_initial_amount_token, 2),
            round(amount_token0_decimal, 12),
            round(amount_token1_decimal, 12),
            round(total_current_amount_token, 2),
            round(delta_initial_current_amount, 2),
            round(percent_delta, 2),
            round(unclaimed_fee_token0, 12),
            round(unclaimed_fee_token1, 12),
            round(total_unclaimed_fee_token, 2),
            round(lp_fee_apr, 2),
            round(lp_fee_apr_1h, 2),
            pending_cake_amount,
            aero_price,
            cake_reward_1h,
            boost,
            round(apr_1h, 2),
            round(apr_all, 2),
            is_active,
            wallet_url_db,
            nft_url_db,
            vietnam_time_current_formatted,
            has_invalid_price,
            position_tick_lower,
            position_tick_upper,
            pool_current_tick,
            "aerodrome",
            total_active_staked_usd,
            total_pool_liquidity_usd,
            npm_address
        )
        return data_nft

    except (ZeroDivisionError, ValueError) as e:
        log.warning(f"⚠️ Calculation error: {e}")
        return None

def _normalize_aerodrome_npm_addresses(npm_addresses):
    if not npm_addresses:
        return []
    if not isinstance(npm_addresses, (list, tuple, set)):
        npm_addresses = [npm_addresses]

    normalized = []
    seen = set()
    for address in npm_addresses:
        if not address:
            continue
        if hasattr(address, "address"):
            address = address.address
        try:
            checksum = Web3.to_checksum_address(address)
        except Exception as exc:
            log.warning(f"Skip invalid Aerodrome NPM address {address}: {exc}")
            continue
        key = checksum.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(checksum)
    return normalized

def _get_aerodrome_factory_address(chain_name, npm_address):
    try:
        npm_checksum = Web3.to_checksum_address(npm_address)
    except Exception as exc:
        log.warning(f"Invalid Aerodrome NPM address {npm_address}: {exc}")
        return None

    factory_map = AERODROME_NPM_FACTORY_ADDRESSES.get(chain_name, {})
    factory_address = factory_map.get(npm_checksum)
    if factory_address:
        return factory_address

    npm_key = npm_checksum.lower()
    for configured_npm, configured_factory in factory_map.items():
        if str(configured_npm).lower() == npm_key:
            return configured_factory
    return None

def _is_blacklisted_aerodrome_nft(nft_id, blacklist_ids):
    try:
        return int(nft_id) in blacklist_ids
    except (TypeError, ValueError):
        return str(nft_id) in {str(item) for item in blacklist_ids}

def _mark_aerodrome_burned(wallet_address, chain_name, nft_id, npm_address, pool_address=None, cache_npm_address=None):
    cache_npm_address = npm_address if cache_npm_address is None else cache_npm_address
    updated_rows = update_nft_status_to_burned(
        wallet_address,
        chain_name,
        nft_id,
        "aerodrome",
        npm_address or "",
        pool_address=pool_address,
    ) or 0
    log.info(
        f"Aerodrome burned update nft={nft_id} npm={npm_address or ''} "
        f"pool={pool_address or ''} rowcount={updated_rows}"
    )
    if updated_rows <= 0:
        log.warning(
            f"Skip Aerodrome closed cache insert because burned update matched 0 rows: "
            f"wallet={wallet_address}, chain={chain_name}, nft={nft_id}, "
            f"npm={npm_address or ''}, pool={pool_address or ''}"
        )
        return False

    insert_nft_closed_cache(wallet_address, chain_name, nft_id, "Burned", "aerodrome", cache_npm_address or "")
    return True

def get_aerodrome_nft_data_all_npms(wallet_address, npm_addresses, chain_name, reward_price_usd, months_lookback=6):
    npm_addresses = _normalize_aerodrome_npm_addresses(npm_addresses)
    if not npm_addresses:
        log.error(f"No Aerodrome NPM addresses configured for {chain_name}")
        return []
    if len(npm_addresses) == 1:
        return get_aerodrome_nft_data(wallet_address, npm_addresses[0], chain_name, reward_price_usd, months_lookback)

    def make_identity(nft_id, npm_address):
        return (str(nft_id), (npm_address or "").lower())

    w3 = get_web3(chain_name)
    start_timestamp = int((datetime.now() - timedelta(days=30 * months_lookback)).timestamp())
    log.info(
        f"Scan Aerodrome wallet {wallet_address} with {len(npm_addresses)} NPM contracts "
        f"from {datetime.fromtimestamp(start_timestamp)}"
    )

    nft_status_data = get_nft_status_data(wallet_address, chain_name, "aerodrome") or {}
    db_identity_map = nft_status_data.get("active_inactive_identity_map", {})
    db_identity_details = nft_status_data.get("active_inactive_identity_details", {})
    closed_identities = set(nft_status_data.get("closed_identities", set()))
    blacklist_identities = set(nft_status_data.get("blacklist_identities", set()))

    contexts = {}
    transfer_ids_by_npm = {}
    failed_npms = set()
    start_block = None

    try:
        start_block = get_block_by_timestamp(chain_name, start_timestamp)
    except Exception as exc:
        log.warning(f"Cannot resolve Aerodrome start block for {chain_name}: {exc}")

    for npm_address in npm_addresses:
        npm_key = npm_address.lower()
        try:
            factory_address = _get_aerodrome_factory_address(chain_name, npm_address)
            if not factory_address:
                raise RuntimeError(f"Missing Aerodrome factory mapping for NPM {npm_address}")
            npm_abi = get_abi(chain_name, npm_address)
            npm_contract = get_contract(w3, npm_address, npm_abi)
            factory_abi = get_abi(chain_name, factory_address)
            factory_contract = get_contract(w3, factory_address, factory_abi)
            if npm_contract is None:
                raise RuntimeError("NPM contract initialization failed")
            if factory_contract is None:
                raise RuntimeError("Factory contract initialization failed")
            contexts[npm_key] = {
                "npm_address": npm_address,
                "factory_address": factory_address,
                "npm_abi": npm_abi,
                "npm_contract": npm_contract,
                "factory_contract": factory_contract,
            }
        except Exception as exc:
            failed_npms.add(npm_key)
            log.warning(f"Skip Aerodrome NPM {npm_address}: {exc}")
            continue

        tx_list = []
        try:
            if start_block:
                tx_list = get_nft_txs_data(chain_name, wallet_address, npm_address, start_block)
        except Exception as exc:
            log.warning(f"Explorer API failed for Aerodrome NPM {npm_address}: {exc}")
            try:
                chain_key_moralis = CHAIN_KEY_MORALIS_EVM.get(chain_name)
                moralis_start_block = get_block_by_timestamp_moralis(chain_key_moralis, start_timestamp)
                if moralis_start_block:
                    tx_list = get_nft_txs_data_moralis(chain_key_moralis, wallet_address, npm_address, moralis_start_block)
            except Exception as moralis_exc:
                log.warning(f"Moralis fallback failed for Aerodrome NPM {npm_address}: {moralis_exc}")

        valid_ids = get_current_owned_token_ids(tx_list, wallet_address, npm_address) if tx_list else set()
        transfer_ids_by_npm[npm_key] = {str(token_id) for token_id in valid_ids}
        log.info(f"Aerodrome NPM {npm_address[:10]} wallet transfer IDs: {sorted(transfer_ids_by_npm[npm_key])}")

    candidate_identities = set()
    for npm_key, token_ids in transfer_ids_by_npm.items():
        candidate_identities.update(make_identity(token_id, npm_key) for token_id in token_ids)

    factory_to_npm_key = {
        str(context["factory_address"]).lower(): npm_key
        for npm_key, context in contexts.items()
        if context.get("factory_address")
    }
    db_specific_identities = set()
    db_legacy_matched_identities = set()
    db_legacy_pool_by_identity = {}
    for identity, _status in db_identity_map.items():
        nft_id, npm_key = identity
        if npm_key:
            if npm_key in contexts:
                candidate_identities.add(identity)
                db_specific_identities.add(identity)
            else:
                log.warning(f"DB Aerodrome identity {identity} uses unconfigured NPM; skip")
        else:
            detail = db_identity_details.get(identity, {})
            pool_address = (detail.get("pool_address") or "").lower()
            factory_address = (detail.get("factory_address") or "").lower()
            matched_npm_key = factory_to_npm_key.get(factory_address)
            if matched_npm_key:
                matched_identity = make_identity(nft_id, matched_npm_key)
                candidate_identities.add(matched_identity)
                db_legacy_matched_identities.add(matched_identity)
                if pool_address:
                    db_legacy_pool_by_identity[matched_identity] = pool_address
            else:
                log.warning(
                    f"Skip expanding legacy Aerodrome identity {identity}: "
                    f"pool={pool_address or 'missing'} factory={factory_address or 'missing'}"
                )

    filtered_identities = []
    for identity in candidate_identities:
        nft_id, npm_key = identity
        legacy_identity = make_identity(nft_id, "")
        is_db_active_identity = identity in db_specific_identities or identity in db_legacy_matched_identities
        is_closed_by_cache = identity in closed_identities or legacy_identity in closed_identities
        if is_closed_by_cache and not is_db_active_identity:
            continue
        if is_closed_by_cache and is_db_active_identity:
            log.warning(f"Verify Aerodrome stale closed-cache identity {identity}: DB latest is still Active/Inactive")
        if identity in blacklist_identities or legacy_identity in blacklist_identities:
            continue
        if npm_key not in contexts:
            continue
        filtered_identities.append(identity)

    filtered_identities = sorted(filtered_identities, key=lambda item: (item[1], item[0]))
    log.info(f"Aerodrome multi-NPM candidate identities: {filtered_identities}")
    if not filtered_identities:
        log.info("No Aerodrome NFT candidates found across configured NPMs.")
        return []

    ids_by_npm = {}
    for nft_id, npm_key in filtered_identities:
        ids_by_npm.setdefault(npm_key, set()).add(nft_id)

    resolved_status_map = {}
    resolved_position_map = {}
    unknown_identities = set()

    for npm_key, candidate_ids in ids_by_npm.items():
        context = contexts[npm_key]
        npm_address = context["npm_address"]
        try:
            active_ids, inactive_ids, unknown_nft_ids, status_map, position_map = get_nft_ids_by_all_status_aerodrome(
                w3,
                chain_name,
                CHAIN_API_MAP[chain_name],
                sorted(candidate_ids),
                context["npm_contract"],
                context["factory_contract"],
                factory_address=context["factory_address"],
            )
            log.info(
                f"Aerodrome NPM {npm_address[:10]} status: "
                f"Active={len(active_ids)}, Inactive={len(inactive_ids)}, Unknown={len(unknown_nft_ids)}"
            )
        except Exception as exc:
            failed_npms.add(npm_key)
            log.warning(f"Aerodrome NPM verification failed for {npm_address}: {exc}")
            continue

        for nft_id in candidate_ids:
            nft_id_str = str(nft_id)
            identity = make_identity(nft_id_str, npm_key)
            status = status_map.get(nft_id_str) or status_map.get(int(nft_id_str)) if str(nft_id_str).isdigit() else status_map.get(nft_id_str)
            if status == "Unknown":
                unknown_identities.add(identity)
                continue
            if status not in {"Active", "Inactive", "Burned"}:
                continue
            resolved_status_map[identity] = status
            resolved_position_map[identity] = position_map.get(nft_id_str) or (
                position_map.get(int(nft_id_str)) if str(nft_id_str).isdigit() else None
            )

    results = []
    slot0_cache = {}
    for identity in filtered_identities:
        nft_id, npm_key = identity
        context = contexts[npm_key]
        status = resolved_status_map.get(identity)

        if status == "Burned":
            log.info(f"Aerodrome NFT {nft_id} npm={context['npm_address']} is Burned. Updating DB/cache.")
            if identity in db_specific_identities:
                _mark_aerodrome_burned(wallet_address, chain_name, nft_id, context["npm_address"])
            if identity in db_legacy_matched_identities:
                legacy_pool_address = db_legacy_pool_by_identity.get(identity)
                if not legacy_pool_address:
                    log.warning(f"Skip legacy Burned update for {identity}: missing matched pool_address")
                else:
                    _mark_aerodrome_burned(
                        wallet_address,
                        chain_name,
                        nft_id,
                        "",
                        pool_address=legacy_pool_address,
                        cache_npm_address=context["npm_address"],
                    )
            continue

        if not status:
            if identity in db_specific_identities or identity in db_legacy_matched_identities:
                if npm_key in failed_npms or identity in unknown_identities:
                    log.warning(
                        f"Skip Burned update for Aerodrome identity {identity}: "
                        f"failed={npm_key in failed_npms}, unknown={identity in unknown_identities}"
                    )
                    continue
                log.info(f"Aerodrome identity {identity} not found in configured NPM. Mark as Burned.")
                if identity in db_specific_identities:
                    _mark_aerodrome_burned(wallet_address, chain_name, nft_id, context["npm_address"])
                if identity in db_legacy_matched_identities:
                    legacy_pool_address = db_legacy_pool_by_identity.get(identity)
                    if not legacy_pool_address:
                        log.warning(f"Skip legacy Burned update for {identity}: missing matched pool_address")
                    else:
                        _mark_aerodrome_burned(
                            wallet_address,
                            chain_name,
                            nft_id,
                            "",
                            pool_address=legacy_pool_address,
                            cache_npm_address=context["npm_address"],
                        )
            continue

        if status == "Unknown":
            log.warning(f"Aerodrome identity {identity} returned Unknown. Skip.")
            continue

        inactived_ids_to_notify = []
        if db_identity_map.get(identity) == "Active" and status == "Inactive":
            inactived_ids_to_notify = [nft_id]

        log.info(
            f"Processing Aerodrome NFT {nft_id} status={status} "
            f"npm={context['npm_address']} factory={context['factory_address']}"
        )
        nft_data = process_nft_mint_data_evm_aerodrome(
            chain_name,
            wallet_address,
            nft_id,
            {nft_id: status},
            {nft_id: resolved_position_map.get(identity)},
            w3,
            context["npm_address"],
            context["npm_contract"],
            context["npm_abi"],
            inactived_ids_to_notify,
            reward_price_usd,
            "cron",
            slot0_cache=slot0_cache,
            factory_address=context["factory_address"],
        )
        if nft_data:
            results.append(nft_data)

    log.info(f"Aerodrome multi-NPM scan complete. Updated positions={len(results)}")
    return results

def get_aerodrome_nft_data(wallet_address, npm_address, chain_name, reward_price_usd, months_lookback=6):
    if isinstance(npm_address, (list, tuple, set)):
        return get_aerodrome_nft_data_all_npms(
            wallet_address,
            npm_address,
            chain_name,
            reward_price_usd,
            months_lookback,
        )

    w3 = get_web3(chain_name)
    
    if not npm_address:
        log.error(f"❌ Chưa cấu hình địa chỉ NPM cho {chain_name}")
        return []

    # 1. Khởi tạo các Contract cần thiết
    factory_address = _get_aerodrome_factory_address(chain_name, npm_address)
    if not factory_address:
        log.error(f"Missing Aerodrome factory mapping for NPM {npm_address} on {chain_name}")
        return []
    npm_abi = get_abi(chain_name, npm_address)
    npm_contract = get_contract(w3, npm_address, npm_abi)
    factory_abi = get_abi(chain_name, factory_address)
    factory_contract = get_contract(w3, factory_address, factory_abi)

    # 2. Thu thập danh sách ID ứng viên (Candidates) từ nhiều nguồn
    start_timestamp = int((datetime.now() - timedelta(days=30 * months_lookback)).timestamp())
    tx_list = []
    log.info(f"📅 Quét lịch sử giao dịch cho ví {wallet_address} từ: {datetime.fromtimestamp(start_timestamp)}")

    try:
        start_block = get_block_by_timestamp(chain_name, start_timestamp)
        if start_block:
            log.info(f"🔎 Start Block: {start_block}")
            tx_list = get_nft_txs_data(chain_name, wallet_address, npm_address, start_block)
    except Exception as e:
        log.warning(f"⚠️ Explorer API lỗi ({e}), fallback sang Moralis...")
        chain_key_moralis = CHAIN_KEY_MORALIS_EVM.get(chain_name)
        start_block = get_block_by_timestamp_moralis(chain_key_moralis, start_timestamp)
        if start_block:
             tx_list = get_nft_txs_data_moralis(chain_key_moralis, wallet_address, npm_address, start_block)
    
    # IDs từ ví (Dựa trên lịch sử Transfer)
    valid_ids_from_wallet = {
        str(token_id)
        for token_id in (get_current_owned_token_ids(tx_list, wallet_address, npm_address) if tx_list else set())
    }
    log.info(f"📥 IDs từ lịch sử Transfer ví: {sorted(list(valid_ids_from_wallet))}")
    
    # IDs đã biết trong Database
    nft_status_data = get_nft_status_data(wallet_address, chain_name, "aerodrome", npm_address=npm_address)
    db_active_inactive_map = {
        str(nft_id): status
        for nft_id, status in nft_status_data.get("active_inactive_map", {}).items()
    }
    db_closed_set = set(map(str, nft_status_data.get("closed_ids", [])))
    db_blacklist_set = set(map(str, nft_status_data.get("blacklist_ids", [])))
    db_known_ids = set(db_active_inactive_map.keys())
    log.info(f"💾 IDs đang lưu trong Database: {sorted(list(db_known_ids))}")

    # Hợp nhất danh sách và lọc bỏ những ID đã đóng hoặc nằm trong blacklist
    all_candidate_ids = ((valid_ids_from_wallet - db_closed_set) | db_known_ids)
    filtered_candidates = [tid for tid in all_candidate_ids if str(tid) not in db_blacklist_set]
    log.info(f"📦 Tổng số NFT ID cần xác minh qua RPC: {len(filtered_candidates)}")
    log.info(f"📦 Tổng số NFT ID cần xác minh: {sorted(list(filtered_candidates))}")

    if not filtered_candidates:
        log.info("✅ Không có NFT nào cần xử lý.")
        return []

    # 3. Giai đoạn Xác minh (Verification via Multicall)
    log.info("📡 Đang chạy Multicall lấy trạng thái On-chain...")
    active_ids, inactive_ids, unknown_ids, status_map, position_map = get_nft_ids_by_all_status_aerodrome(
        w3,
        chain_name,
        CHAIN_API_MAP[chain_name],
        filtered_candidates,
        npm_contract,
        factory_contract,
        factory_address=factory_address,
    )
    log.info(f"✅ Kết quả Multicall: Active={len(active_ids)}, Inactive={len(inactive_ids)}, Unknown={len(unknown_ids)}")
    log.info(f"✅ Active IDs: {sorted(list(active_ids))}")
    log.info(f"✅ Inactive IDs: {sorted(list(inactive_ids))}")
    log.info(f"✅ Unknown IDs: {sorted(list(unknown_ids))}")

    # Xác định danh sách ID Inactive để notify (Chỉ những cái trước đó là Active trong DB)
    db_active_ids = [tid for tid, stat in db_active_inactive_map.items() if stat == "Active"]
    inactived_ids_to_notify = [tid for tid in db_active_ids if tid in inactive_ids]
    if inactived_ids_to_notify:
        log.info(f"🔔 Phát hiện {len(inactived_ids_to_notify)} NFT chuyển sang Inactive.")
        log.info(f"🔔 Inactive IDs: {sorted(list(inactived_ids_to_notify))}")

    results = []
    slot0_cache = {}  # Cache slot0 per-pool trong suốt 1 run (tối ưu Bước 4)
    
    # 4. Giai đoạn Phân loại và Xử lý (Vòng lặp tập trung)
    for nft_id in filtered_candidates:
        nft_id_str = str(nft_id)
        status = status_map.get(nft_id_str)

        # TRƯỜNG HỢP 1: NFT đã bị đóng (Xác nhận Burned từ Smart Contract)
        if status == "Burned":
            log.info(f"🔥 NFT ID: {nft_id_str} -> Trạng thái: BURNED. Đang cập nhật DB & Cache...")
            _mark_aerodrome_burned(wallet_address, chain_name, nft_id_str, npm_address)
            continue

        # TRƯỜNG HỢP 2: Lỗi RPC (Unknown) - Không được đánh dấu Burned để bảo vệ dữ liệu
        if status == "Unknown":
            log.warning(f"⚠️ NFT ID: {nft_id_str} -> Trạng thái: UNKNOWN (RPC Error). Bỏ qua...")
            continue

        # TRƯỜNG HỢP 3: NFT không tồn tại trong kết quả Multicall
        if not status:
            if nft_id_str in db_known_ids:
                 log.info(f"🧹 NFT ID: {nft_id_str} -> Không tìm thấy trên RPC nhưng có trong DB. Dọn dẹp (Mark as Burned)...")
                 _mark_aerodrome_burned(wallet_address, chain_name, nft_id_str, npm_address)
            continue

        # TRƯỜNG HỢP 4: NFT Hợp lệ (Active/Inactive) -> Tính toán dữ liệu chi tiết
        log.info(f"⚙️ Đang xử lý dữ liệu chi tiết NFT ID: {nft_id_str} ({status})")
        nft_data = process_nft_mint_data_evm_aerodrome(
            chain_name, wallet_address, nft_id, status_map, position_map,
            w3, npm_address, npm_contract, npm_abi, inactived_ids_to_notify,
            reward_price_usd, "cron", slot0_cache=slot0_cache, factory_address=factory_address
        )
        if nft_data:
            results.append(nft_data)

    log.info(f"🏁 Hoàn tất chu kỳ quét. Tổng số NFT đã cập nhật thành công: {len(results)}")
    return results
