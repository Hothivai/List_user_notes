from web3 import Web3
from eth_account import Account
import math
import math
from decimal import Decimal, getcontext
from services.liquidity_actions.helper import *
from dotenv import load_dotenv
import os
import time
from config import *
from services.liquidity_actions.stake_liquidity import *
from services.solana.decode_account import *
import asyncio
import websockets
from eth_abi import decode as abi_decode

getcontext().prec = 50  # tăng độ chính xác tính toán

load_dotenv()

# PRIVATE_KEY = os.getenv('PRIVATE_KEY')
# ACCOUNT = Account.from_key(PRIVATE_KEY)

def get_pool_farm_info(chain, pool_address):
    w3 = get_web3_connection(chain)
    if not w3:
        print(f"❌ Không thể kết nối Web3 tới {chain}.")
        return
    
    masterchef_address = MASTERCHEF_ADDRESSES.get(chain)
    masterchef_abi = get_abi(chain, masterchef_address)
    masterchef_contract = get_contract(w3, masterchef_address, masterchef_abi)
    
    pool_info_db = get_pool_info_from_db(chain, pool_address)
    if not pool_info_db:
        return False
    
    pool_pid = pool_info_db["pid"]
    
    pool_info = masterchef_contract.functions.poolInfo(pool_pid).call()
    alloc_point = pool_info[0]
    
    return {
        "pool_pid": pool_pid,
        "alloc_point": alloc_point
    }

def get_data_mint(chain, pool_address):
    
    w3 = get_web3_connection(chain)
    if not w3:
        print(f"❌ Không thể kết nối Web3 tới {chain}.")
        return 
    
    pool_address_cs = Web3.to_checksum_address(pool_address)
    pool_abi = get_abi(chain, pool_address_cs)
    pool_contract = get_contract(w3, pool_address_cs, pool_abi)
    
    npm_address = NPM_ADDRESSES.get(chain, "unknown")
    masterchef_address = MASTERCHEF_ADDRESSES.get(chain, "unknown")  

    # Lấy thông tin pool
    pool_info = get_pool_info_from_db(chain, pool_address_cs)
    fee_tier_raw = pool_info["fee"]
    token0_address = pool_info["token0_address"]
    token1_address = pool_info["token1_address"]
    
    token0_symbol = pool_info["token0_symbol"]
    token1_symbol = pool_info["token1_symbol"]
    token0_decimals = pool_info["token0_decimals"]
    token1_decimals = pool_info["token1_decimals"]
    pool_pid = pool_info["pid"]
    
    pool_farm_info = get_pool_farm_info(chain, pool_address)
    if pool_farm_info:
        alloc_point = pool_farm_info["alloc_point"]
    else:
        alloc_point = 0
        
    print(f"Token0: {token0_symbol} ({token0_decimals} decimals) | Token1: {token1_symbol} ({token1_decimals} decimals)")
    
    slot0 = pool_contract.functions.slot0().call()
    sqrtPriceX96 = slot0[0]
    tick_spacing = pool_contract.functions.tickSpacing().call()

    scale_factor = 10 ** token0_decimals / 10 ** token1_decimals
    
    fee_tier = fee_tier_raw
    
    # Giá hiện tại token1 per token0
    price_current = (sqrtPriceX96 / (2**96)) ** 2
    current_price = price_current * scale_factor
    
    return {
        "token0_address": token0_address,
        "token1_address": token1_address,
        "token0_symbol": token0_symbol,
        "token1_symbol": token1_symbol,
        "token0_decimals": token0_decimals,
        "token1_decimals": token1_decimals,
        "fee_tier": fee_tier,
        "current_price": current_price,
        "tick_spacing": tick_spacing,
        "scale_factor": scale_factor,
        "sqrtPriceX96": sqrtPriceX96,
        "pool_pid": pool_pid,
        "alloc_point": alloc_point,
        "npm_address": npm_address,
        "masterchef_address": masterchef_address
    }

def get_data_mint_sol(chain, client, pool_account):    
    pool_info_decode = decode_pool_state(client, pool_account)
    
    tick_current = pool_info_decode["tick_current"]
    tick_spacing = pool_info_decode["tick_spacing"]
    sqrtPriceX64 = pool_info_decode["sqrt_price_x64"]
    
    # Lấy thông tin pool
    pool_info = get_pool_sol_info_from_db(chain, pool_account)
    fee_tier_raw = pool_info["fee"]
    token0_address = pool_info["token0_mint"]
    token1_address = pool_info["token1_mint"]
    
    token0_symbol = pool_info["token0_symbol"]
    token1_symbol = pool_info["token1_symbol"]
    token0_decimals = pool_info["token0_decimals"]
    token1_decimals = pool_info["token1_decimals"]
    
    scale_factor = 10 ** token0_decimals / 10 ** token1_decimals
    
    fee_tier = fee_tier_raw
    
    # Giá hiện tại token1 per token0
    price_current = (sqrtPriceX64 / (2**64)) ** 2
    current_price = price_current * scale_factor
    
    return {
        "token0_address": token0_address,
        "token1_address": token1_address,
        "token0_symbol": token0_symbol,
        "token1_symbol": token1_symbol,
        "token0_decimals": token0_decimals,
        "token1_decimals": token1_decimals,
        "fee_tier": fee_tier,
        "current_price": current_price,
        "tick_spacing": tick_spacing,
        "scale_factor": scale_factor,
        "sqrtPriceX96": sqrtPriceX64,
    }
    
def calculate_mint_amount(sqrtPriceX96, min_pct, max_pct, token0_amount, scale_factor, tick_spacing, token0_decimals, token1_decimals):
    # Token0 amount
    capital_token0 = token0_amount * (10 ** token0_decimals)
    
    # Giá hiện tại token1 per token0
    price_current = (sqrtPriceX96 / (2**96)) ** 2
    
    # Tính giá lower/upper
    lower_price = price_current * (1 + (min_pct / 100))
    upper_price = price_current * (1 + (max_pct / 100))
    
    # Convert sang tick
    tick_lower = round_tick(int(math.log(lower_price, 1.0001)), tick_spacing)
    tick_upper = round_tick(int(math.log(upper_price, 1.0001)), tick_spacing)

    min_price_lower_raw = tick_to_price(tick_lower)
    max_price_upper_raw = tick_to_price(tick_upper)
    
    min_price_lower = min_price_lower_raw * scale_factor
    max_price_upper = max_price_upper_raw * scale_factor

    # Công thức Uniswap V3 tính amount1 từ amount0
    # sqrt(P) = sqrtPriceX96 / 2^96
    sqrt_lower = math.sqrt(min_price_lower_raw)
    sqrt_upper = math.sqrt(max_price_upper_raw)
    sqrt_current = math.sqrt(price_current)

    # Liquidity L = amount0 * (sqrt(upper)*sqrt(current)) / (sqrt(upper)-sqrt(current))
    liquidity = capital_token0 * (sqrt_upper * sqrt_current) / (sqrt_upper - sqrt_current)

    # amount1 = L * (sqrt(current) - sqrt(lower))
    amount0_desired = capital_token0
    amount1_desired = liquidity * (sqrt_current - sqrt_lower)
    
    return {
        "tick_lower": tick_lower,
        "tick_upper": tick_upper,
        "min_price": min_price_lower,
        "max_price": max_price_upper,
        "amount0Desired": int(amount0_desired) / (10 ** token0_decimals),
        "amount1Desired": int(amount1_desired) / (10 ** token1_decimals)
    }
    
# Stream sqrtPriceX96 khi có Swap
async def stream_slot0(chain_name, pool_address):
    pool_address_cs = Web3.to_checksum_address(pool_address)

    # Pancake V3 Swap event signature (topic0)
    swap_topic = "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83"

    ws_url = WS_RPC_URLS[chain_name]
    async with websockets.connect(ws_url) as ws:
        # Gửi request subscribe logs
        sub_req = {
            "id": 1,
            "method": "eth_subscribe",
            "params": [
                "logs",
                {"address": pool_address_cs, "topics": [swap_topic]}
            ]
        }
        await ws.send(json.dumps(sub_req))
        sub_reply = await ws.recv()
        print(f"🔗 Subscribed to Swap on {chain_name}: {sub_reply}")

        # Nhận log realtime
        while True:
            try:
                message = await ws.recv()
                data = json.loads(message)

                if "params" in data and "result" in data["params"]:
                    log = data["params"]["result"]

                    # log["data"] chứa (amount0, amount1, sqrtPriceX96, liquidity, tick, protocolFeesToken0, protocolFeesToken1)
                    decoded = abi_decode(
                        ["int256","int256","uint160","uint128","int24","uint128","uint128"],
                        bytes.fromhex(log["data"][2:])
                    )
                    amount0, amount1, sqrtPriceX96, liquidity, tick, fee0, fee1 = decoded

                    print(f"📊 block={int(log['blockNumber'],16)} | sqrtPriceX96={sqrtPriceX96} | tick={tick}")

            except Exception as e:
                print("❌ Error:", e)
                break

def get_mint_params(chain, pool_address, min_ptc=-10, max_ptc=10, capital_token0=0):
    """
    pct_range: ví dụ 0.2 cho ±20%
    capital_token0: số token0 bạn muốn cung cấp
    """
    w3 = get_web3_connection(chain)
    if not w3:
        print(f"❌ Không thể kết nối Web3 tới {chain}.")
        return 
    
    pool_address_cs = Web3.to_checksum_address(pool_address)
    pool_abi = get_abi(chain, pool_address_cs)
    pool_contract = get_contract(w3, pool_address_cs, pool_abi)

    # Lấy thông tin pool
    pool_info = get_pool_info_from_db(chain, pool_address_cs)
    fee_tier_raw = pool_info["fee"]
    token0_address = pool_info["token0_address"]
    token1_address = pool_info["token1_address"]
    
    token0_symbol = pool_info["token0_symbol"]
    token1_symbol = pool_info["token1_symbol"]
    token0_decimals = pool_info["token0_decimals"]
    token1_decimals = pool_info["token1_decimals"]
    print(f"Token0: {token0_symbol} ({token0_decimals} decimals) | Token1: {token1_symbol} ({token1_decimals} decimals)")
    
    pool_pid = pool_info["pid"]
    
    pool_farm_info = get_pool_farm_info(chain, pool_address)
    if pool_farm_info:
        alloc_point = pool_farm_info["alloc_point"]
    else:
        alloc_point = 0
    
    slot0 = pool_contract.functions.slot0().call()
    sqrtPriceX96 = slot0[0]
    tick_current = slot0[1]
    tick_spacing = pool_contract.functions.tickSpacing().call()
    capital_token0 = capital_token0 * (10 ** token0_decimals)

    scale_factor = 10 ** token0_decimals / 10 ** token1_decimals
    
    fee_tier = fee_tier_raw
    
    # Giá hiện tại token1 per token0
    price_current = (sqrtPriceX96 / (2**96)) ** 2

    # Tính giá lower/upper
    lower_price = price_current * (1 + (min_ptc / 100))
    upper_price = price_current * (1 + (max_ptc / 100))

    # Convert sang tick
    tick_lower = round_tick(int(math.log(lower_price, 1.0001)), tick_spacing)
    tick_upper = round_tick(int(math.log(upper_price, 1.0001)), tick_spacing)

    current_price = price_current * scale_factor
    min_price_lower_raw = tick_to_price(tick_lower)
    max_price_upper_raw = tick_to_price(tick_upper)
    
    min_price_lower = min_price_lower_raw * scale_factor
    max_price_upper = max_price_upper_raw * scale_factor

    # Công thức Uniswap V3 tính amount1 từ amount0
    # sqrt(P) = sqrtPriceX96 / 2^96
    sqrt_lower = math.sqrt(min_price_lower_raw)
    sqrt_upper = math.sqrt(max_price_upper_raw)
    sqrt_current = math.sqrt(price_current)

    # Liquidity L = amount0 * (sqrt(upper)*sqrt(current)) / (sqrt(upper)-sqrt(current))
    liquidity = capital_token0 * (sqrt_upper * sqrt_current) / (sqrt_upper - sqrt_current)

    # amount1 = L * (sqrt(current) - sqrt(lower))
    amount0_desired = capital_token0
    amount1_desired = liquidity * (sqrt_current - sqrt_lower)

    return {
        "token0_address": token0_address,
        "token1_address": token1_address,
        "token0_symbol": token0_symbol,
        "token1_symbol": token1_symbol,
        "token0_decimals": token0_decimals,
        "token1_decimals": token1_decimals,
        "fee_tier": fee_tier,
        "current_price": current_price,
        "min_price": min_price_lower,
        "max_price": max_price_upper,
        "tickLower": tick_lower,
        "tickUpper": tick_upper,
        "amount0Desired": int(amount0_desired) / (10 ** token0_decimals),
        "amount1Desired": int(amount1_desired) / (10 ** token1_decimals),
        "pool_pid": pool_pid,
        "alloc_point": alloc_point
    }

def extract_token_id_from_receipt(w3, npm_contract, receipt):
    npm_addr = npm_contract.address.lower()
    transfer_sig = w3.keccak(text="Transfer(address,address,uint256)").hex()
    inc_liq_sig = w3.keccak(text="IncreaseLiquidity(uint256,uint128,uint256,uint256)").hex()
    zero_topic = "0x" + "0"*64

    # 1) Ưu tiên đọc Transfer mint (from = 0x0)
    for log in receipt["logs"]:
        if log["address"].lower() == npm_addr and log["topics"][0].hex() == transfer_sig and len(log["topics"]) == 4:
            if log["topics"][1].hex() == zero_topic:  # mint từ 0x0
                return int(log["topics"][3].hex(), 16)

    # 2) Fallback: IncreaseLiquidity mang tokenId ở topics[1]
    for log in receipt["logs"]:
        if log["address"].lower() == npm_addr and log["topics"][0].hex() == inc_liq_sig and len(log["topics"]) >= 2:
            return int(log["topics"][1].hex(), 16)

    raise ValueError("Không tìm thấy tokenId trong receipt")

def mint_position(chain_name, pool_address, min_pct, max_pct, capital_token0, account, private_key):
    
    w3 = get_web3_connection(chain_name)
    if not w3:
        print(f"❌ Không thể kết nối Web3 tới {chain_name}.")
        return
    
    npm_address = NPM_ADDRESSES.get(chain_name, "unknown")
    if npm_address != "unknown":
        npm_address_cs = Web3.to_checksum_address(npm_address)
    
    npm_abi = get_abi(chain_name, npm_address)
    npm_contract = get_contract(w3, npm_address, npm_abi)

    # Lấy tickLower, tickUpper, amounts
    params = get_mint_params(
        chain_name, pool_address, min_pct, max_pct, capital_token0
    )
    
    token0 = Web3.to_checksum_address(params["token0_address"])
    token1 = Web3.to_checksum_address(params["token1_address"])
    # target_token0_address = get_target_token_address(w3, token0)
    # target_token1_address = get_target_token_address(w3, token1)
    fee = params["fee_tier"]
    amount0 = params["amount0Desired"]
    amount1 = params["amount1Desired"]
    token0_decimals = params["token0_decimals"]
    token1_decimals = params["token1_decimals"]
    
    pool_farm_info = get_pool_farm_info(chain_name, pool_address)
    pool_pid = pool_farm_info["pool_pid"]
    alloc_point = pool_farm_info["alloc_point"]

    ensure_wrapped_token_balance(chain_name, account, token0, token0_decimals, amount0, private_key)
    # ensure_wrapped_token_balance("BNB", account, token1, amount1, private_key)
    
    # Approve token0 if needed
    approve_token_if_needed(
        w3, chain_name, token0, token0_decimals, npm_address_cs, amount0, account, private_key
    )
    
    # Approve token1 if needed
    approve_token_if_needed(
        w3, chain_name, token1, token1_decimals, npm_address_cs, amount1, account, private_key
    )

    # Gọi mint
    mint_tx = npm_contract.functions.mint({
        "token0": token0,
        "token1": token1,
        "fee": fee,
        "tickLower": params["tickLower"],
        "tickUpper": params["tickUpper"],
        "amount0Desired": int(amount0 * (10**token0_decimals)),
        "amount1Desired": int(amount1 * (10**token1_decimals)),
        "amount0Min": 0,  # hoặc set slippage tolerance
        "amount1Min": 0,
        "recipient": account,
        "deadline": int(time.time()) + 600
    })
    
    build_mint_tx = build_transaction_safely(chain_name, mint_tx, account)

    signed_mint_tx = w3.eth.account.sign_transaction(build_mint_tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_mint_tx.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    print("Mint transaction sent:", tx_hash.hex())
    print(f"Pool ID: {pool_pid}")
    
    token_id = extract_token_id_from_receipt(w3, npm_contract, receipt)
    print(f"Token ID: {token_id}")

    if token_id:
        if alloc_point > 0:
            stake_liquidity_position(chain_name, token_id, pool_pid, account, private_key)
        else:
            print(f"Pool {pool_address} is not active farming. Skipping stake.")
    else:
        print("Token ID not found in Transfer event")
        
    return tx_hash.hex()

if __name__ == "__main__":
    
    min_pct = -10  # ±20%
    max_pct = 10
    capital_token0 = 0.0003  # ví dụ 1 WBNB
    pool_address = "0x370fbd4cC0C5C99FfC8586aAff24a5134601386B"

    params = get_mint_params("BNB", pool_address, min_pct, max_pct, capital_token0)
    # print(params)
    
    # mint_position("BNB", pool_address, min_pct, max_pct, capital_token0, ACCOUNT.address, PRIVATE_KEY)
    