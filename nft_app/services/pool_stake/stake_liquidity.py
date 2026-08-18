import sys
import os

# Đường dẫn đến thư mục flask_app (chứa thư mục services)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../'))

# Thêm vào sys.path
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import requests
from web3 import Web3
from w3multicall.multicall import W3Multicall
import math
from services.update_query import fetch_all_pool_info, get_total_alloc_point_each_chain
from config import (
    MASTERCHEF_ADDRESSES, NPM_ADDRESSES, API_URLS, API_KEYS, RPC_URLS, CHAIN_ID_MAP,
    CHAIN_SCAN_URLS, AERODROME_NPM_ADDRESSES
)
from services.pancake_api import get_price_tokens, get_cake_price_usd
from web3.exceptions import ContractLogicError
from decimal import Decimal
import time
from collections import defaultdict
import json

# Đổi sang topic của event "Deposit"
DEPOSIT_TOPIC0 = "0xb19157bff94fdd40c58c7d4a5d52e8eb8c2d570ca17b322b49a2bbbeedc82fbf"
WITHDRAW_TOPIC0 = "0xf341246adaac6f497bc2a656f546ab9e182111d630394f0c57c710a59a2cb567"

POSITION_METHOD_SIG = 'positions(uint256)(uint96,address,address,address,uint24,int24,int24,uint128,uint256,uint256,uint128,uint128)'
GET_POOL_INFO_METHOD_SIG = 'getPool(address)(address,address,uint24)'

DISCORD_WEBHOOK_URL = 'https://discordapp.com/api/webhooks/1377961748925124681/4L4i0oxq6PD1jLlBUV2IxH-G2vobb-ESm2VhKWL30dQztF4sRVg8IkgOoWe4W2EB0IFS'

abi_slot0 = [
    {
        "name": "slot0",
        "outputs": [
            {"type": "uint160", "name": "sqrtPriceX96"},
            {"type": "int24", "name": "tick"},
            {"type": "uint16", "name": "observationIndex"},
            {"type": "uint16", "name": "observationCardinality"},
            {"type": "uint16", "name": "observationCardinalityNext"},
            {"type": "uint32", "name": "feeProtocol"},
            {"type": "bool", "name": "unlocked"},
        ],
        "inputs": [],
        "stateMutability": "view",
        "type": "function",
    }
]

abi_slot0_aerodrome = [
    {
        "name": "slot0",
        "outputs": [
            {"type": "uint160", "name": "sqrtPriceX96"},
            {"type": "int24", "name": "tick"},
            {"type": "uint16", "name": "observationIndex"},
            {"type": "uint16", "name": "observationCardinality"},
            {"type": "uint16", "name": "observationCardinalityNext"},
            {"type": "bool", "name": "unlocked"},
        ],
        "inputs": [],
        "stateMutability": "view",
        "type": "function",
    }
]

def load_multicall_history(filepath='multicall_times.json'):
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            return json.load(f)
    return {}

def save_multicall_history(data, filepath='multicall_times.json'):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def get_positions_multicall(w3, token_ids, chain, batch_size=50):
    all_positions = {}
    print(f"- Token IDs to fetch on Pancake: {token_ids}")
    for token_batch in chunks(token_ids, batch_size):
        w3_multicall = W3Multicall(w3)
        for token_id in token_batch:
            w3_multicall.add(W3Multicall.Call(NPM_ADDRESSES[chain], POSITION_METHOD_SIG, token_id))
        try:
            results = w3_multicall.call()
            for i, token_id in enumerate(token_batch):
                data = results[i]
                all_positions[token_id] = {
                    "nonce": data[0],
                    "operator": data[1],
                    "token0": data[2],
                    "token1": data[3],
                    "fee": data[4],
                    "tickLower": data[5],
                    "tickUpper": data[6],
                    "liquidity": data[7],
                    "feeGrowthInside0LastX128": data[8],
                    "feeGrowthInside1LastX128": data[9],
                    "tokensOwed0": data[10],
                    "tokensOwed1": data[11],
                }
        except ContractLogicError as e:
            print(f"[Multicall Error] Failed batch {token_batch}: {e}")
            # fallback: gọi từng token_id để tìm cái nào lỗi
            for token_id in token_batch:
                try:
                    single_call = W3Multicall(w3)
                    single_call.add(W3Multicall.Call(NPM_ADDRESSES[chain], POSITION_METHOD_SIG, token_id))
                    result = single_call.call()[0]
                    all_positions[token_id] = {
                        "nonce": result[0],
                        "operator": result[1],
                        "token0": result[2],
                        "token1": result[3],
                        "fee": result[4],
                        "tickLower": result[5],
                        "tickUpper": result[6],
                        "liquidity": result[7],
                        "feeGrowthInside0LastX128": result[8],
                        "feeGrowthInside1LastX128": result[9],
                        "tokensOwed0": result[10],
                        "tokensOwed1": result[11],
                    }
                except Exception as e2:
                    print(f"❌ token_id {token_id} failed: {e2}")

    return all_positions

def get_positions_multicall_aerodrome(w3, token_ids, chain, npm_address, batch_size=50):
    all_positions = {}
    print(f"- Token IDs to fetch on Aerodrome: {token_ids}")
    
    if hasattr(npm_address, 'address'):
        npm_address = npm_address.address

    if isinstance(npm_address, list):
        npm_address = npm_address[0]

    if isinstance(npm_address, str):
        npm_address = Web3.to_checksum_address(npm_address)
    
    for token_batch in chunks(token_ids, batch_size):
        w3_multicall = W3Multicall(w3)
        for token_id in token_batch:
            w3_multicall.add(W3Multicall.Call(npm_address, POSITION_METHOD_SIG, token_id))
        try:
            results = w3_multicall.call()
            for i, token_id in enumerate(token_batch):
                data = results[i]
                all_positions[token_id] = {
                    "nonce": data[0],
                    "operator": data[1],
                    "token0": data[2],
                    "token1": data[3],
                    "tickSpacing": data[4],
                    "tickLower": data[5],
                    "tickUpper": data[6],
                    "liquidity": data[7],
                    "feeGrowthInside0LastX128": data[8],
                    "feeGrowthInside1LastX128": data[9],
                    "tokensOwed0": data[10],
                    "tokensOwed1": data[11],
                }
        except ContractLogicError as e:
            print(f"[Multicall Error] Failed batch {token_batch}: {e}")
            # fallback: gọi từng token_id để tìm cái nào lỗi
            for token_id in token_batch:
                try:
                    single_call = W3Multicall(w3)
                    single_call.add(W3Multicall.Call(npm_address, POSITION_METHOD_SIG, token_id))
                    result = single_call.call()[0]
                    all_positions[token_id] = {
                        "nonce": result[0],
                        "operator": result[1],
                        "token0": result[2],
                        "token1": result[3],
                        "tickSpacing": result[4],
                        "tickLower": result[5],
                        "tickUpper": result[6],
                        "liquidity": result[7],
                        "feeGrowthInside0LastX128": result[8],
                        "feeGrowthInside1LastX128": result[9],
                        "tokensOwed0": result[10],
                        "tokensOwed1": result[11],
                    }
                except Exception as e2:
                    print(f"❌ token_id {token_id} failed: {e2}")

    return all_positions

def get_logs(chain, topic0, topic2=None):
    url = API_URLS[chain]
    
    params = {
        "module": "logs",
        "action": "getLogs",
        "fromBlock": "0",
        "toBlock": "latest",
        "address": MASTERCHEF_ADDRESSES[chain],
        "topic0": topic0
    }
    if topic2:
        params["topic2"] = topic2
    params["apikey"] = API_KEYS[chain]

    response = requests.get(url, params=params)
    data = response.json()
    if data["status"] == "1":
        return data["result"]
    else:
        print(f"⚠️ Không tìm thấy logs hoặc lỗi: {data.get('message')}")
        return []
    
def get_staked_nft_ids_by_pid(chain, pid: int) -> list:
    deposited = set()
    withdrawn = set()

    pid_topic = hex(pid)[2:].rjust(64, '0')  # pad 64 ký tự
    pid_topic = '0x' + pid_topic

    print(f"🔍 Quét PID: {pid} - Topic: {pid_topic}")

    deposit_logs = get_logs(chain, DEPOSIT_TOPIC0, topic2=pid_topic)
    for log in deposit_logs:
        token_id = int(log["topics"][3], 16)
        deposited.add(token_id)

    withdraw_logs = get_logs(chain, WITHDRAW_TOPIC0, topic2=pid_topic)
    for log in withdraw_logs:
        token_id = int(log["topics"][3], 16)
        withdrawn.add(token_id)

    staked_ids = sorted(deposited - withdrawn)
    return staked_ids

def get_current_tick(w3, pool_address, abi_slot0, rpc_list=None):
    def _call_slot0(provider):
        pool_contract = provider.eth.contract(address=pool_address, abi=abi_slot0)
        return pool_contract.functions.slot0().call()

    # thử RPC chính trước
    try:
        slot0 = _call_slot0(w3)
        return slot0[1], slot0[0]
    except Exception as e:
        print(f"⚠️ RPC chính lỗi khi gọi slot0: {e}")

    # fallback qua danh sách RPC
    if rpc_list:
        for rpc in rpc_list:
            try:
                w3_backup = Web3(Web3.HTTPProvider(rpc))
                slot0 = _call_slot0(w3_backup)
                print(f"✅ RPC backup {rpc} OK")
                return slot0[1], slot0[0]
            except Exception as e:
                print(f"⚠️ RPC backup {rpc} fail: {e}")
                continue

    # nếu tất cả fail
    raise Exception(f"❌ Tất cả RPC đều fail khi gọi slot0 cho pool {pool_address}")

def get_amount_cake_per_second(chain, w3):
    masterchef_contract = w3.eth.contract(address=MASTERCHEF_ADDRESSES[chain], abi=[
        {
            "inputs": [],
            "name": "latestPeriodCakePerSecond",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view",
            "type": "function"
            }
    ])
    
    amount_cake_per_second = (masterchef_contract.functions.latestPeriodCakePerSecond().call() / (10**30))
    
    return amount_cake_per_second

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

def send_discord_webhook_message(message: str, webhook_url: str = DISCORD_WEBHOOK_URL):
    data = {"content": message}
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.post(webhook_url, json=data, headers=headers, timeout=5)
        print(f"✅ Discord webhook sent: {response.status_code}")
    except Exception as e:
        print(f"❌ Failed to send Discord webhook: {e}")

def get_total_stake_liquidity_pool(chain):
    w3 = Web3(Web3.HTTPProvider(RPC_URLS[chain]))
    
    pool_data = fetch_all_pool_info(chain)
    amount_cake_per_second = get_amount_cake_per_second(chain, w3)
    total_alloc_point = get_total_alloc_point_each_chain(chain)
    cake_price = get_cake_price_usd()
    
    multicall_times_by_chain = defaultdict(list, load_multicall_history())
    for pool_info in pool_data[:1]:
        token_ids = get_staked_nft_ids_by_pid(chain, pool_info['pid'])
        alloc_point = pool_info.get('alloc_point', 0)
        
        print(f"Length Token IDs: {len(token_ids)}")
        print(f"Token IDs: {token_ids}")
        
        start_time = time.perf_counter()
        positions = get_positions_multicall(w3, token_ids, chain)
        end_time = time.perf_counter()
        
        duration = end_time - start_time
        multicall_times_by_chain[chain].append(duration)
        
        current_tick_pool, sqrt_price_x96 = get_current_tick(w3, pool_info['pool_address'], abi_slot0)  # Địa chỉ pool cụ thể bạn cần kiểm tra
        print(f"Current Tick Pool: {current_tick_pool}")
        
        token0_price = get_price_tokens(CHAIN_ID_MAP[chain], (pool_info['token0_address']))
        token1_price = get_price_tokens(CHAIN_ID_MAP[chain], (pool_info['token1_address']))
        
        total_liquidity = 0
        for tid, info in positions.items():
            nft_id_status = get_position_status(
                liquidity=info['liquidity'],
                tick_lower=info['tickLower'],
                tick_upper=info['tickUpper'],
                current_tick=current_tick_pool,
                tokens_owed0=info['tokensOwed0'],
                tokens_owed1=info['tokensOwed1']
            )
            
            if nft_id_status == "Active":
                amount0, amount1 = get_current_amounts(
                    liquidity=info['liquidity'],
                    sqrt_price_x96=sqrt_price_x96,
                    tick_lower=info['tickLower'],
                    tick_upper=info['tickUpper']
                )
                
                amount0_decimal = amount0 / (10 ** (pool_info['token0_decimals']))  
                amount1_decimal = amount1 / (10 ** (pool_info['token1_decimals']))  
                
                price_amount0 = amount0_decimal * token0_price
                price_amount1 = amount1_decimal * token1_price
                
                liquidity_value = price_amount0 + price_amount1
                print(f"Token ID: {tid} - Status: {nft_id_status} - Liquidity: {info['liquidity']} - Amount0: {amount0_decimal} - Amount1: {amount1_decimal} - Price Amount0: {price_amount0} - Price Amount1: {price_amount1} - Position Liquidity: {liquidity_value}")

                total_liquidity += liquidity_value
        
        save_multicall_history(multicall_times_by_chain)
        
        second_per_year = Decimal(365 * 24 * 60 * 60)
        amount_cake_per_second = Decimal(amount_cake_per_second)
        alloc_point = Decimal(alloc_point)
        total_alloc_point = Decimal(total_alloc_point)
        cake_price = Decimal(cake_price)
        total_liquidity = Decimal(total_liquidity)

        pool_cake_per_second = amount_cake_per_second * alloc_point / total_alloc_point
        pool_apr = ((pool_cake_per_second * second_per_year) * cake_price) / total_liquidity * 100
        
        pool_url = f"{CHAIN_SCAN_URLS[chain]}{pool_info['pool_address']}"
        pair_symbol = f"{pool_info['token0_symbol']}/{pool_info['token1_symbol']}"
        message = (
            f"🎯 Total stake liquidity in-range of pool on chain {chain}: {pair_symbol}: ${total_liquidity:,.2f}\n"
            f"🎯 Pool APR: {pool_apr:.2f}%\n"
            f"{pool_url} \n"
            f"Multicall execution time: {end_time - start_time:.2f} seconds\n"
        )
        send_discord_webhook_message(message)
        
        print(message)
        durations = multicall_times_by_chain[chain]
        avg = sum(durations) / len(durations)
        
        print(f"🔁 Durations for chain {chain}: {durations}")
        print(f"🔁 Average multicall time for chain {chain}: {avg:.2f} seconds (over {len(durations)} calls)")
        print(f"⏱️ Multicall execution time for {len(token_ids)} token IDs of pool {pool_info['pid']}: {duration:.2f} seconds")
        print("-----------------------------------------------------")


if __name__ == "__main__":
    chains = ['ETH', 'BNB', 'ARB', 'BAS']
    for chain in chains:
        print(f"🔍 Đang quét chain: {chain}")
        get_total_stake_liquidity_pool(chain)
    # get_total_stake_liquidity_pool('ARB')