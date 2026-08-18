import requests
import json
from web3 import Web3
from datetime import datetime, timedelta, timezone
from services.event_history.increase_liquidity_history import get_increase_liquidity_history
from services.event_history.decrease_liquidity_history import get_decrease_liquidity_history
from services.event_history.stake_liquidity_history import get_stake_time
from services.event_history.collect_fee_history import get_last_collect_time
from services.execute_data import (
    create_database_and_table, get_last_pending_cake_info, get_data_inactive_nft_id, 
    update_nft_status_to_burned, insert_nft_closed_cache, get_last_unclaimed_fee_token
)
from services.update_query import get_pool_info_with_fallback, get_nft_status_data, get_total_alloc_point_each_chain, get_total_cake_per_day_on_chain, get_nft_initial_amount_from_db
import math
from config import *
from services.pancake_api import get_price_tokens, get_data_pool_bsc, get_cake_price_usd
from services.pool_stake.stake_liquidity import get_positions_multicall, get_current_tick
import os
from services.helper import *
import time
from logging_setup import evm_logger as log

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
    if chain not in API_URLS or chain not in API_KEYS:
        log.error(f"❌ No API URL or API Key for {chain}")
        return None
    
    etherscan_url = API_URLS[chain]
    params = {
        "module": "account",
        "action": "tokennfttx",
        "address": wallet_address,
        "startblock": start_block,
        "endblock": 999999999,
        "sort": "asc",
        "page": 1,
        "offset": 10000,
        "apikey": API_KEYS[chain]
    }

    if contract_address:
        params["contractaddress"] = contract_address

    for attempt in range(1, retries + 1):
        try:
            response = requests.get(etherscan_url, params=params, timeout=timeout)
            response_json = response.json()

            if response.status_code == 200 and response_json["status"] == "1":
                return response_json["result"]
            else:
                log.warning(f"⚠️ Attempt {attempt}: API returned error: {response_json.get('result')}")
        except requests.exceptions.Timeout:
            log.warning(f"⚠️ Attempt {attempt}: Timeout when retrieving NFT transactions")
        except requests.exceptions.RequestException as e:
            log.warning(f"⚠️ Attempt {attempt}: Error retrieving NFT transactions: {e}")

        # backoff chờ lâu hơn mỗi lần retry
        time.sleep(2 * attempt)

    log.error("❌ Failed to retrieve NFT transactions after retries")
    return None

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
    # if contract_address:
    #     params["token_addresses[]"] = contract_address

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

def get_current_owned_token_ids_moralis(chain_name, tx_list, wallet_address, npm_address):
    owned = set()
    wallet_address = wallet_address.lower()
    masterchef_address = MASTERCHEF_ADDRESSES.get(chain_name, "unknown").lower()
    npm_address = npm_address.lower()
    
    tx_list.sort(key=lambda x: x.get("block_timestamp", 0)) 

    for tx in tx_list:
        token_id = str(tx["token_id"])
        from_addr = tx["from_address"].lower()
        to_addr = tx["to_address"].lower()
        token_address = tx["token_address"].lower()
        
        # Chỉ xử lý các NFT thuộc contract NonfungiblePositionManager
        if token_address != npm_address:
            continue
        
        # Trường hợp 1: Nhận NFT (Từ người khác gửi đến hoặc rút từ Masterchef về)
        if to_addr == wallet_address:
            owned.add(token_id)
            
        # Trường hợp 2: Gửi NFT đi từ ví
        elif from_addr == wallet_address:
            if to_addr != masterchef_address:
                owned.discard(token_id)
                
    return owned

def get_current_owned_token_ids(chain_name, tx_list, wallet_address):
    owned = set()
    wallet_address = wallet_address.lower()
    masterchef_address = MASTERCHEF_ADDRESSES.get(chain_name, "unknown").lower()
    
    SPAM_THRESHOLD = 10**15 
    tx_list.sort(key=lambda x: int(x["timeStamp"])) 

    for tx in tx_list:
        token_id = tx["tokenID"]
        if int(token_id) >= SPAM_THRESHOLD:
            continue

        from_addr = tx["from"].lower()
        to_addr = tx["to"].lower()

        # TRƯỜNG HỢP 1: Nhận NFT vào ví (hoặc rút từ Masterchef về ví)
        if to_addr == wallet_address:
            owned.add(token_id)
        
        # TRƯỜNG HỢP 2: Gửi NFT từ ví đi
        elif from_addr == wallet_address:
            if to_addr != masterchef_address:
                owned.discard(token_id)
            
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

# Get name of token contract address
def get_name_contract_address(chain, pool_address, contract_address):
    pool_data = get_data_pool_bsc(chain, pool_address)
    if pool_data:
        if pool_data.get("token0") and pool_data["token0"].get("id") == contract_address:
            return pool_data["token0"].get("name", "Unknown Token Name")
        elif pool_data.get("token1") and pool_data["token1"].get("id") == contract_address:
            return pool_data["token1"].get("name", "Unknown Token Name")
    return "Unknown Token Name"

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

# def get_nft_ids_by_all_status(chain, sorted_owned_token_ids, npm_contract, factory_contract):
#     active_nft_ids = []
#     inactive_nft_ids = []
#     status_map = {}
#     position_map = {}

#     for nft_id in sorted_owned_token_ids:
#         position_data = npm_contract.functions.positions(int(nft_id)).call()
#         position_map[str(nft_id)] = position_data

#         token0 = Web3.to_checksum_address(position_data[2])
#         token1 = Web3.to_checksum_address(position_data[3])
#         feeTier = position_data[4]
#         liquidity = position_data[7]
#         tick_lower = position_data[5]
#         tick_upper = position_data[6]
#         tokens_owed0 = position_data[10]
#         tokens_owed1 = position_data[11]

#         POOL_ADDRESS = factory_contract.functions.getPool(token0, token1, feeTier).call()
#         pool_data = get_data_pool_bsc(chain, POOL_ADDRESS)
#         current_tick = pool_data["tick"]

#         status_position = get_position_status(liquidity, tick_lower, tick_upper, current_tick, tokens_owed0, tokens_owed1)
        
#         status_map[str(nft_id)] = status_position

#         if status_position == 'Active':
#             active_nft_ids.append(nft_id)
#         elif status_position == 'Inactive':
#             inactive_nft_ids.append(nft_id)

#     return active_nft_ids, inactive_nft_ids, status_map, position_map

# tODO HEAVY TASK
def get_nft_ids_by_all_status(w3, chain_name, chain_api, sorted_owned_token_ids, npm_contract, factory_contract):
    active_nft_ids = []
    inactive_nft_ids = []
    unknown_nft_ids = []
    status_map = {}
    position_map = {}

    sorted_owned_token_ids = sorted(map(int, sorted_owned_token_ids))
    
    positions_data = get_positions_multicall(w3, sorted_owned_token_ids, chain_name)
    
    for nft_id, position_data in positions_data.items():
        position_map[str(nft_id)] = position_data

        token0 = Web3.to_checksum_address(position_data["token0"])
        token1 = Web3.to_checksum_address(position_data["token1"])
        feeTier = position_data["fee"]
        liquidity = position_data["liquidity"]
        tick_lower = position_data["tickLower"]
        tick_upper = position_data["tickUpper"]
        tokens_owed0 = position_data["tokensOwed0"]
        tokens_owed1 = position_data["tokensOwed1"]

        try:
            # POOL_ADDRESS = factory_contract.functions.getPool(token0, token1, feeTier).call()
            pool_info = get_pool_info_with_fallback(factory_contract, chain_name, chain_api, token0, token1, feeTier)
            if not pool_info:
                log.warning("⚠️ Không lấy được thông tin pool.")
                return

            POOL_ADDRESS = pool_info["pool_address"]
            
            # pool_data = get_data_pool_bsc(chain_api, POOL_ADDRESS)
            current_tick, sqrt_price_x96 = get_current_tick(w3, POOL_ADDRESS, ABI_SLOT0, rpc_list=RPC_BACKUP_LIST.get(chain_name, []))
            log.info(f"✅ Pool Address: {POOL_ADDRESS} → Current Tick: {current_tick}")

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

def get_inactive_status(chain, nft_id, npm_contract, factory_contract):
    
    position_data = npm_contract.functions.positions(int(nft_id)).call()

    token0 = Web3.to_checksum_address(position_data[2])
    token1 = Web3.to_checksum_address(position_data[3])
    fee = position_data[4]
    liquidity = position_data[7]
    tick_lower = position_data[5]
    tick_upper = position_data[6]
    tokens_owed0 = position_data[10]
    tokens_owed1 = position_data[11]

    POOL_ADDRESS = factory_contract.functions.getPool(token0, token1, fee).call()
    
    pool_data = get_data_pool_bsc(chain, POOL_ADDRESS)
    current_tick = pool_data["tick"]
    
    status_position = get_position_status(liquidity, tick_lower, tick_upper, current_tick, tokens_owed0, tokens_owed1)
            
    return status_position

def get_contract(w3, contract_address, abi):
    if not abi:
        log.error(f"❌ No ABI provided for contract {contract_address}")
        return None
    return w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=abi)

def get_token_info(w3, token_address):
    token_address = Web3.to_checksum_address(token_address)
    
    abi = [
        {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
        {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
    ]
    
    contract = w3.eth.contract(address=token_address, abi=abi)

    try:
        symbol = contract.functions.symbol().call()
    except Exception:
        symbol = "Unknow"

    try:
        decimals = contract.functions.decimals().call()
    except Exception:
        decimals = 18

    return {"symbol": symbol, "decimals": decimals}

def get_pool_data_from_contract(w3, pool_address):
    pool_address = Web3.to_checksum_address(pool_address)

    abi = [
        {
            "inputs": [],
            "name": "slot0",
            "outputs": [
            {
                "internalType": "uint160",
                "name": "sqrtPriceX96",
                "type": "uint160"
            },
            {
                "internalType": "int24",
                "name": "tick",
                "type": "int24"
            },
            {
                "internalType": "uint16",
                "name": "observationIndex",
                "type": "uint16"
            },
            {
                "internalType": "uint16",
                "name": "observationCardinality",
                "type": "uint16"
            },
            {
                "internalType": "uint16",
                "name": "observationCardinalityNext",
                "type": "uint16"
            },
            {
                "internalType": "uint32",
                "name": "feeProtocol",
                "type": "uint32"
            },
            {
                "internalType": "bool",
                "name": "unlocked",
                "type": "bool"
            }
            ],
            "stateMutability": "view",
            "type": "function"
        }
    ]

    try:
        contract = w3.eth.contract(address=pool_address, abi=abi)

        slot0 = contract.functions.slot0().call()
        tick = slot0[1]
        sqrt_price = slot0[0]
        liquidity = contract.functions.liquidity().call()
        token0 = contract.functions.token0().call()
        token1 = contract.functions.token1().call()
        fee = contract.functions.fee().call()

        return {
            "id": pool_address,
            "tick": tick,
            "sqrtPriceX96": sqrt_price,
            "liquidity": liquidity,
            "token0": token0,
            "token1": token1,
            "feeTier": fee,
            "source": "contract"
        }

    except Exception as e:
        log.error(f"❌ Fallback failed: {e}")
        return None

def make_pool_key(token0, token1, fee):
    t0, t1 = sorted([token0.lower(), token1.lower()])
    return f"{t0}_{t1}_{fee}"

# Create database and insert data
create_database_and_table()

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
    nft_url = f"https://pancakeswap.finance/liquidity/{nft_id}?chain={CHAIN_NAME_PANCAKE[chain_name]}"
    wallet_url = f"{CHAIN_SCAN_URLS[chain_name]}{wallet_address}"

    # Gửi Discord
    send_discord_webhook_message(
        f'ID [{nft_id}]({nft_url}) {chain_name} '
        f'(({token0_name} {current_token0_amount_fmt})-({token1_name} {current_token1_amount_fmt}), '
        f'{current_amount_fmt}, {farm_apr_fmt}%) '
        f'[Wallet {wallet_address[:6]}...{wallet_address[-4:]}]({wallet_url}) ✅ Active ➜ ❌ Inactive.'
    )

def process_nft_mint_data_evm(chain_name, wallet_address, nft_id, status_map, position_map, factory_contract, 
                          w3, chain_api, multiplier_chain, cake_per_second, npm_contract, masterchef_contract,
                          inactived_nft_ids, npm_abi, masterchef_abi, mode):
    
    log.info(f"🔍 Processing NFT ID {nft_id} on {chain_name} for wallet {wallet_address}")
    try:
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
                fee = position_data[4]
                tick_lower = position_data[5]
                tick_upper = position_data[6]
                liquidity = position_data[7]
                tokens_owed0 = position_data[10]
                tokens_owed1 = position_data[11]
        else:
            operator = Web3.to_checksum_address(position_data['operator'])
            token0 = Web3.to_checksum_address(position_data['token0'])
            token1 = Web3.to_checksum_address(position_data['token1'])
            fee = position_data['fee']
            liquidity = position_data['liquidity']
            tick_lower = position_data['tickLower']
            tick_upper = position_data['tickUpper']
        
        pool_info = get_pool_info_with_fallback(factory_contract, chain_name, chain_api, token0, token1, fee)
        if not pool_info:
            log.warning("⚠️ Không lấy được thông tin pool.")
            return

        POOL_ADDRESS = pool_info["pool_address"]
        token0_symbol = pool_info["token0_symbol"]
        token1_symbol = pool_info["token1_symbol"]
        token0_decimal = pool_info["token0_decimals"]
        token1_decimal = pool_info["token1_decimals"]
        alloc_point = pool_info["alloc_point"]
        log.info(f" - Pool address: {POOL_ADDRESS}, chain: {chain_name}")
        
        # --- LẤY THÔNG TIN THANH KHOẢN POOL (Dùng cho Hybrid Snapshot) ---
        total_active_staked_usd = 0
        total_pool_liquidity_usd = 0
        from services.update_query import get_pool_info_evm
        p_db = get_pool_info_evm(chain_name, POOL_ADDRESS)
        if p_db:
            total_active_staked_usd = float(p_db.get("total_staked_liquidity", 0))
            if total_active_staked_usd > 0:
                total_pool_liquidity_usd = total_active_staked_usd + float(p_db.get("total_inactive_staked_liquidity", 0))
            else:
                total_pool_liquidity_usd = float(p_db.get("total_value_lock"))
            
        log.info(f" - Snapshot Liquidity: Active Stake=${total_active_staked_usd}, Total Liq=${total_pool_liquidity_usd}")
        log.info(f"🔍 Pool Address: {POOL_ADDRESS}, Token0: {token0_symbol}, Token1: {token1_symbol}, Alloc Point: {alloc_point}")
        
        pool_abi = get_abi(chain_name, Web3.to_checksum_address(POOL_ADDRESS))
        pool_contract = w3.eth.contract(address=Web3.to_checksum_address(POOL_ADDRESS), abi=pool_abi)
            
        # slot0 = pool_contract.functions.slot0().call()
        slot0 = call_with_fallback(
            pool_contract.functions.slot0(),
            RPC_BACKUP_LIST.get(chain_name, []),
            contract_abi=pool_abi,
            w3_main=w3
        )
        if slot0 is None:
            log.warning("⚠️ Không lấy được slot0.")
            return None
        
        sqrt_price_x96 = slot0[0]
        current_tick = slot0[1]
        log.info(f"✅ Pool slot0: sqrtPriceX96={sqrt_price_x96}, current_tick={current_tick}")
        
        # Get cake per second of each pool
        if multiplier_chain == 0:
            log.warning(f"⚠️ Multiplier for {chain_name} is zero. Cannot compute cake_per_second_pool.")
            cake_per_second_pool = 0
        else:
            cake_per_second_pool = float(cake_per_second * alloc_point) / float(multiplier_chain)
            log.info(f"🍰 Cake per second for pool {POOL_ADDRESS}: {cake_per_second_pool}")
        
        # Get position status
        status_position = status_map.get(str(nft_id))
        if not status_position:
            status_position = get_position_status(
                liquidity, tick_lower, tick_upper, current_tick,
                tokens_owed0, tokens_owed1
            )
        is_active = 1 if status_position == 'Active' else 0

        # --- Get Mint transaction ---
        mint_transactions, latest_time_add, first_time_add, initial_amount_token0, initial_amount_token1 = safe_api_call(
            get_increase_liquidity_history,
            API_URLS, API_KEYS, chain_name, NPM_ADDRESSES[chain_name], int(nft_id), mode=mode,
            default=([], None, None, 0, 0)
        )

        # --- Get Decrease transaction ---
        decrease_transaction, latest_time_decrease, first_time_decrease, decrease_amount_token0, decrease_amount_token1 = safe_api_call(
            get_decrease_liquidity_history,
            API_URLS, API_KEYS, chain_name, NPM_ADDRESSES[chain_name], int(nft_id),
            default=([], None, None, 0, 0)
        )

        # --- Get latest time stake liquidity to farm ---
        latest_time_stake = safe_api_call(
            get_stake_time,
            API_URLS, API_KEYS, chain_name, MASTERCHEF_ADDRESSES[chain_name], int(nft_id),
            default=None
        )

        # --- Get latest time collect fee ---
        latest_time_collect = safe_api_call(
            get_last_collect_time,
            API_URLS, API_KEYS, chain_name, NPM_ADDRESSES[chain_name], int(nft_id),
            default=None
        )

        log.info(f"✅ Latest time: add={latest_time_add}, collect={latest_time_collect}, decrease={latest_time_decrease}, stake={latest_time_stake}")
        
        # --- Convert thời gian sang timestamp an toàn ---
        latest_time_add_ts = safe_to_timestamp_with_fallback(latest_time_add, int(nft_id), chain_name, wallet_address, "date_add_liquidity")
        latest_time_collect_ts = safe_to_timestamp(latest_time_collect) or 0
        latest_time_decrease_ts = safe_to_timestamp(latest_time_decrease) or 0
        latest_time_stake_ts = safe_to_timestamp(latest_time_stake) or 0
        
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
            db_result = get_nft_initial_amount_from_db(nft_id, chain_name, wallet_address)
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
        if fees is None:
            log.warning(f"⚠️ API collect fee = None")
            unclaimed_fee_token0 = 0
            unclaimed_fee_token1 = 0         

        unclaimed_fee_token0 = fees[0] / 10**token0_decimal
        unclaimed_fee_token1 = fees[1] / 10**token1_decimal
        total_unclaimed_fee_token = (unclaimed_fee_token0*price_token0) + (unclaimed_fee_token1*price_token1)
        log.info(f"🔍 Unclaimed fee token0: {unclaimed_fee_token0}, token1: {unclaimed_fee_token1}, total unclaimed fee token: {total_unclaimed_fee_token}")

        # Current time in Vietnam timezone
        vietnam_timezone = timezone(timedelta(hours=7))
        now_vn = datetime.now(vietnam_timezone)
        vietnam_time_current_formatted = now_vn.strftime("%Y-%m-%d %H:%M:%S")
        vietnam_time_current = int(now_vn.replace(tzinfo=None).timestamp())
        log.info(f"📅 Time Current: {vietnam_time_current_formatted}")
        
        # Calculate time instance
        delta_time = vietnam_time_current - max_latest_time_add_collect_ts
        delta_time_in_day = delta_time / 60 # minutes
        safe_minutes = delta_time_in_day if delta_time_in_day >= 1 else 1  # at least 1 minute
        # print(f"📅 Time Elapsed add liquidity: {round(delta_time_in_day, 2)} days")
    
        if total_current_amount_token and abs(total_current_amount_token) > 1e-6:
            lp_fee_apr = ((total_unclaimed_fee_token / safe_minutes * 60 * 24 * 365) / total_current_amount_token) * 100
        else:
            lp_fee_apr = 0
        
        ### LP FEE APR 1H ###
        fee_data = get_last_unclaimed_fee_token(int(nft_id))
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
            if not isinstance(created_at, datetime):
                try:
                    created_at = datetime.strptime(str(created_at), "%Y-%m-%d %H:%M:%S")
                except Exception:
                    created_at = datetime.now()
            log.info(f"- Unclaimed fee token0 ago: {unclaimed_fee_token0_ago}, Unclaimed fee token1 ago: {unclaimed_fee_token1_ago}, Created at: {created_at}")
            
            delta_unclaimed_fee_token0 = unclaimed_fee_token0 - unclaimed_fee_token0_ago
            delta_unclaimed_fee_token1 = unclaimed_fee_token1 - unclaimed_fee_token1_ago
            total_delta_fee_usd = delta_unclaimed_fee_token0 * price_token0 + delta_unclaimed_fee_token1 * price_token1
            log.info(f"- Total delta fee usd: {total_delta_fee_usd}")
            
            delta_time_minutes = (vietnam_time_current - int(created_at.timestamp())) / 60 # minutes
            safe_time_minutes = max(delta_time_minutes, 1)  # at least 1 minute
            log.info(f"- Time Elapsed fee apr 1h: {safe_time_minutes}")
            
            if denominator and abs(denominator) > 1e-6:
                lp_fee_apr_1h = (total_delta_fee_usd / safe_time_minutes * 60 * 24 * 365) / denominator * 100
            else:
                lp_fee_apr_1h = 0
        else:
            lp_fee_apr_1h = lp_fee_apr
        
        # Cake Reward 
        cake_price = get_cake_price_usd()
        # pending_cake = masterchef_contract.functions.pendingCake(int(nft_id)).call()
        
        log.info(f"🔎 Checking NFT ID: {nft_id} ({type(nft_id)}) - repr: {repr(nft_id)}")
        
        if masterchef_contract is None and masterchef_abi is None:
            log.error("❌ MasterChef contract is None.")
            pending_cake = None
        else:
            pending_cake = call_with_fallback(
                masterchef_contract.functions.pendingCake(int(nft_id)),
                RPC_BACKUP_LIST.get(chain_name, []),
                contract_abi=masterchef_abi,
                w3_main=w3
            )
        
        if pending_cake is None:
            log.warning(f"⚠️ API pending cake = None")
            pending_cake = 0
        
        pending_cake_amount = round((pending_cake/10**18), 6)
        pending_cake_price = round((pending_cake/10**18) * cake_price, 6)
        log.info(f"🎉 Pending Cake: {pending_cake_amount} ({pending_cake_price} USD)")
        
        # user_position_infos = masterchef_contract.functions.userPositionInfos(int(nft_id)).call()
        
        if masterchef_contract is None and masterchef_abi is None:
            log.error("❌ MasterChef contract is None.")
            user_position_infos = None
        else:
            user_position_infos = call_with_fallback(
                masterchef_contract.functions.userPositionInfos(int(nft_id)),
                RPC_BACKUP_LIST.get(chain_name, []),
                contract_abi=masterchef_abi,
                w3_main=w3
            )
        
        if user_position_infos is None:
            log.warning(f"⚠️ API user position infos = None")
            boost = 1.0
            log.info(f"🎉 Boost: {boost} (default)")
        else:
            boost = round((user_position_infos[8] / 10**12), 2)
            log.info(f"🎉 Boost: {boost}")
        
        ### Time latest stake liquidity
        
        # if latest_time_stake:
        #     latest_time_stake = datetime.strptime(latest_time_stake, "%m-%d-%Y %H:%M:%S")
        #     latest_time_stake_timestamp = int(latest_time_stake.timestamp())
        # else:
        #     latest_time_stake_timestamp = 0
        
        time_elapsed_stake_days = (vietnam_time_current - max_latest_time_add_stake_ts) / (3600 * 24)
        log.info(f"⏳ Time Elapsed stake liquidity: {round(time_elapsed_stake_days)} days")
        
        # Farm APR All
        if denominator and abs(denominator) > 1e-6 and time_elapsed_stake_days > 0:
            apr_all = (((pending_cake_price / time_elapsed_stake_days) * 365) / denominator * 100) * boost
        else:
            apr_all = 0
        
        ### FARM APR 1H ###
        pending_cake_info = get_last_pending_cake_info(int(nft_id))
        last_pending_cake_timestamp = None
        pending_cake_ago = 0.0
        log.info(f"Pending_cake_info={pending_cake_info} ({type(pending_cake_info)}) repr={repr(pending_cake_info)}")
        
        if pending_cake_info:
            pending_cake_ago = pending_cake_info.get("pending_cake", 0.0)
            last_pending_cake_timestamp = pending_cake_info.get("created_at", None)
            log.info(f"⏳ Time Elapsed pending CAKE ago: {last_pending_cake_timestamp}")
            
            if last_pending_cake_timestamp:
                if isinstance(last_pending_cake_timestamp, datetime):
                    last_pending_cake_timestamp = int(last_pending_cake_timestamp.timestamp())

                delta_time_hour = max((vietnam_time_current - last_pending_cake_timestamp) / 60, 1)
            else:
                delta_time_hour = 1
                
            log.info(f"⏳ Time Elapsed pending CAKE: {delta_time_hour} minutes")
            log.info(f"📉 total current amount: ${total_current_amount_token}")
                
            delta_pending_cake_amount = pending_cake_amount - pending_cake_ago
            log.info(f"📉 Pending CAKE Reward: ${pending_cake_amount}")
            log.info(f"📉 Pending CAKE Reward ago: ${pending_cake_ago}")
            log.info(f"📉 Delta pending cake amount: {delta_pending_cake_amount} %")
            
            if denominator and abs(denominator) > 1e-6:                    
                apr_1h = (delta_pending_cake_amount * cake_price / delta_time_hour * 60 * 24 * 365) / denominator * 100
            elif denominator == 0:
                apr_1h = (delta_pending_cake_amount * cake_price / delta_time_hour * 60 * 24 * 365) / total_current_amount_token * 100
            else:
                apr_1h = 0
        else:
            apr_1h = apr_all
        
        tz_vn = timezone(timedelta(hours=7))
        if max_latest_time_add_remove_ts == 0:
            latest_time_add_datetime = datetime.fromtimestamp(vietnam_time_current)
        else:
            latest_time_add_datetime = datetime.fromtimestamp(max_latest_time_add_remove_ts, tz=tz_vn)
            
        log.info(f"⏳ Time Elapsed add liquidity: {latest_time_add_datetime}")
        
        ### CAKE REWARD 1H ###
        if pending_cake_info is None:
            delta_time_hour = (vietnam_time_current - max_latest_time_add_remove_ts) / 60 # minutes
            log.info(f"⏳ Time Elapsed add liquidity(cake reward 1h): {delta_time_hour} minutes")
            
            delta_pending_cake_amount = pending_cake_amount
            log.info(f"📉 Pending CAKE Reward: ${pending_cake_amount}")
            log.info(f"📉 Pending CAKE Reward ago: ${pending_cake_ago}")
            log.info(f"📉 Delta pending cake amount: {delta_pending_cake_amount} %")
            
        if cake_per_second_pool > 0 and delta_time_hour > 0:
            cake_reward_1h = float(delta_pending_cake_amount) / (float(cake_per_second_pool) * (delta_time_hour * 60)) * 100
            log.info(f"📉 Cake Reward 1h: {cake_reward_1h} CAKE")
        else:
            cake_reward_1h = 0
            log.warning(f"⚠️ Cannot compute Cake Reward: cake_per_second_pool={cake_per_second_pool}, delta_time_hour={delta_time_hour}")

        wallet_url_db = f"{CHAIN_SCAN_URLS[chain_name]}{wallet_address}"
        nft_url_db = f"https://pancakeswap.finance/liquidity/{nft_id}?chain={CHAIN_NAME_PANCAKE[chain_name]}"
        
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
            cake_price,
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
            "pancakeswap",
            total_active_staked_usd,
            total_pool_liquidity_usd
        )
        return data_nft

    except (ZeroDivisionError, ValueError) as e:
        log.warning(f"⚠️ Calculation error: {e}")
        return None

# Get NFT data by wallet address
def get_nft_data(WALLET_ADDRESS, chain_name, six_months_ago=None):
    """
    Hàm lấy dữ liệu NFT cho PancakeSwap với logic đã được tối ưu hóa tương tự Aerodrome.
    """
    w3 = get_web3(chain_name)
    chain_api = CHAIN_API_MAP.get(chain_name, "unknown")
    
    # Lấy thông số phần thưởng của chain
    multiplier_chain = get_total_alloc_point_each_chain(chain=chain_name)
    total_cake_per_day = get_total_cake_per_day_on_chain(chain_name)
    cake_per_second = total_cake_per_day / 86400
    log.info(f"🔍 [Chain Info] {chain_name}: Multiplier={multiplier_chain}, Cake/Day={total_cake_per_day}")

    # Khởi tạo địa chỉ và contract
    npm_address = NPM_ADDRESSES.get(chain_name, "unknown")
    factory_address = FACTORY_ADDRESSES.get(chain_name, "unknown")
    masterchef_address = MASTERCHEF_ADDRESSES.get(chain_name, "unknown")
    
    npm_abi = get_abi(chain_name, npm_address)
    factory_abi = get_abi(chain_name, factory_address)
    masterchef_abi = get_abi(chain_name, masterchef_address)
    
    npm_contract = get_contract(w3, npm_address, npm_abi)
    factory_contract = get_contract(w3, factory_address, factory_abi)
    masterchef_contract = get_contract(w3, masterchef_address, masterchef_abi)

    # 1. Thu thập danh sách ID ứng viên (Candidates) từ ví và Database
    if six_months_ago is None:
        six_months_ago = int((datetime.now() - timedelta(days=30 * 6)).timestamp())
    
    log.info(f"📅 Quét lịch sử giao dịch cho ví {WALLET_ADDRESS} từ: {datetime.fromtimestamp(six_months_ago)}")

    tx_list = []
    try:
        start_block = get_block_by_timestamp(chain_name, six_months_ago)
        if start_block:
            log.info(f"🔎 Start Block: {start_block}")
            tx_list = get_nft_txs_data(chain_name, WALLET_ADDRESS, npm_address, start_block)
    except Exception as e:
        log.warning(f"⚠️ xxxScan lỗi ({e}), chuyển sang Moralis...")
        chain_key_moralis = CHAIN_KEY_MORALIS_EVM.get(chain_name, "unknown")
        start_block = get_block_by_timestamp_moralis(chain_key_moralis, six_months_ago)
        if start_block:
             tx_list = get_nft_txs_data_moralis(chain_key_moralis, WALLET_ADDRESS, npm_address, start_block)

    # IDs từ ví
    ids_from_wallet = set()
    if tx_list:
        if "moralis" in str(tx_list): # Cách phân biệt nhanh dựa trên cấu trúc data
            ids_from_wallet = get_current_owned_token_ids_moralis(chain_name, tx_list, WALLET_ADDRESS, npm_address)
        else:
            ids_from_wallet = get_current_owned_token_ids(chain_name, tx_list, WALLET_ADDRESS)
    
    log.info(f"📥 IDs từ lịch sử ví: {sorted(list(ids_from_wallet))}")

    # IDs từ Database
    nft_status_data = get_nft_status_data(WALLET_ADDRESS, chain_name, "pancakeswap")
    db_active_inactive_map = nft_status_data.get("active_inactive_map", {})
    db_closed_set = set(map(str, nft_status_data.get("closed_ids", [])))
    db_blacklist_set = set(map(int, nft_status_data.get("blacklist_ids", [])))
    db_known_ids = set(db_active_inactive_map.keys())
    log.info(f"💾 IDs trong Database: {sorted(list(db_known_ids))}")

    # Hợp nhất và lọc (Bỏ qua những cái đã đóng hoặc blacklist)
    all_candidate_ids = (ids_from_wallet | db_known_ids) - db_closed_set
    filtered_candidates = [tid for tid in all_candidate_ids if int(tid) not in db_blacklist_set]
    log.info(f"📦 Tổng số NFT cần xác minh On-chain: {len(filtered_candidates)}")
    log.info(f"📦 NFT ID cần xác minh: {sorted(list(filtered_candidates))}")

    if not filtered_candidates:
        log.info("✅ Không có NFT nào cần xử lý.")
        return []

    # 2. Giai đoạn Xác minh trạng thái On-chain (Multicall)
    log.info("📡 Đang chạy Multicall lấy trạng thái...")
    active_ids, inactive_ids, unknown_ids, status_map, position_map = get_nft_ids_by_all_status(
        w3, chain_name, chain_api, filtered_candidates, npm_contract, factory_contract
    )
    log.info(f"✅ Kết quả: Active={len(active_ids)}, Inactive={len(inactive_ids)}, Unknown={len(unknown_ids)}")
    log.info(f"✅ Active IDs: {sorted(list(active_ids))}")
    log.info(f"✅ Inactive IDs: {sorted(list(inactive_ids))}")
    log.info(f"✅ Unknown IDs: {sorted(list(unknown_ids))}")

    # Danh sách ID Inactive để notify (Nếu trước đó DB lưu là Active)
    db_active_ids = [tid for tid, stat in db_active_inactive_map.items() if stat == "Active"]
    inactived_ids_to_notify = [tid for tid in db_active_ids if tid in inactive_ids]

    results = []
    
    # 3. Giai đoạn Phân loại và Xử lý (Vòng lặp tập trung)
    for nft_id in filtered_candidates:
        nft_id_str = str(nft_id)
        status = status_map.get(nft_id_str)

        # TRƯỜNG HỢP 1: NFT đã bị đóng (Xác nhận Burned từ Smart Contract)
        if status == "Burned":
            log.info(f"🔥 NFT ID: {nft_id_str} -> BURNED. Đang cập nhật DB & Cache...")
            update_nft_status_to_burned(WALLET_ADDRESS, chain_name, nft_id_str, 'pancakeswap')
            insert_nft_closed_cache(WALLET_ADDRESS, chain_name, nft_id_str, 'Burned', 'pancakeswap')
            continue

        # TRƯỜNG HỢP 2: Lỗi RPC (Unknown) - Bỏ qua để bảo vệ dữ liệu, không đánh dấu Burned
        if status == "Unknown":
            log.warning(f"⚠️ NFT ID: {nft_id_str} -> UNKNOWN. Skipping...")
            continue

        # TRƯỜNG HỢP 3: NFT không tồn tại trong kết quả Multicall nhưng có trong DB
        if not status:
            if nft_id_str in db_known_ids:
                 log.info(f"🧹 NFT ID: {nft_id_str} -> Không tìm thấy trên RPC. Dọn dẹp (Mark as Burned)...")
                 update_nft_status_to_burned(WALLET_ADDRESS, chain_name, nft_id_str, 'pancakeswap')
                 insert_nft_closed_cache(WALLET_ADDRESS, chain_name, nft_id_str, 'Burned', 'pancakeswap')
            continue

        # TRƯỜNG HỢP 4: NFT Hợp lệ (Active/Inactive) -> Tiến hành tính toán
        log.info(f"⚙️ Đang xử lý dữ liệu chi tiết NFT ID: {nft_id_str} ({status})")
        nft_data = process_nft_mint_data_evm(
            chain_name, WALLET_ADDRESS, nft_id_str, status_map, position_map,
            factory_contract, w3, chain_api, multiplier_chain, cake_per_second,
            npm_contract, masterchef_contract, inactived_ids_to_notify,
            npm_abi, masterchef_abi, mode="cron"
        )
        if nft_data:
            results.append(nft_data)

    log.info(f"🏁 Hoàn tất chu kỳ. Tổng số NFT cập nhật thành công: {len(results)}")
    return results