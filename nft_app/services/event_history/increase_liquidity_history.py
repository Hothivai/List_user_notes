import requests
import datetime
import time
from logging_setup import evm_logger as log

def get_increase_liquidity_history(API_URLS, API_KEYS, chain, npm_contract_address, token_id, 
                                   from_block=None, to_block=None, mode="auto", retries=3, delay=15,
                                   retry_empty_results=True):
    """
    Lấy lịch sử IncreaseLiquidity của NFT ID.
    
    mode:
        - "cron": quét block lớn (0 -> latest)
        - "realtime": chỉ quét 1000 block quanh block mint
        - "auto": tự chọn dựa trên from_block/to_block có sẵn
    """
    if chain not in API_URLS or chain not in API_KEYS:
        log.error(f"❌ No API URL or API Key for {chain}")
        return [], None, None, 0, 0

    topic0 = "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
    topic1 = "0x" + hex(token_id)[2:].zfill(64)
    url = API_URLS[chain]
    api_key = API_KEYS[chain]

    # --- Xác định block range ---
    if not from_block or not to_block:        
        headers = {"User-Agent": "Mozilla/5.0"}
        
        # Lấy latest block
        params_block = {
            "module": "proxy",
            "action": "eth_blockNumber",
            "apikey": api_key
        }
        response_block = requests.get(url, params=params_block, headers=headers)
        response_block.raise_for_status()
        latest_block = int(response_block.json()["result"], 16)
        log.info(f"ℹ️ Latest block on {chain}: {latest_block}")

        if mode == "cron":
            from_block = "0"
            to_block = str(latest_block)
        elif mode == "realtime":
            from_block = str(max(latest_block - 2000, 0))
            to_block = str(latest_block)
        else:  # auto
            from_block = str(max(latest_block - 50000, 0))
            to_block = str(latest_block)

    params = {
        "module": "logs",
        "action": "getLogs",
        "fromBlock": from_block,
        "toBlock": to_block,
        "address": npm_contract_address,
        "topic0": topic0,
        "topic1": topic1,
        "apikey": api_key
    }

    for attempt in range(retries):
        response = requests.get(url, params=params, headers=headers)
        data = response.json()

        if not isinstance(data.get("result"), list):
            log.warning(f"⚠️ API error for {chain}: {data.get('result')}")
            time.sleep(delay)
            continue

        if response.status_code == 200 and len(data["result"]) > 0:
            log.info(f"✅ Found {len(data['result'])} IncreaseLiquidity events for tokenId {token_id}")
            mint_transactions = []
            total_token0 = 0
            total_token1 = 0
            latest_time = None

            for log_entry in data["result"]:
                tx_hash = log_entry["transactionHash"]
                time_stamp = int(log_entry.get("timeStamp", "0x0"), 16)
                dt_object = datetime.datetime.fromtimestamp(time_stamp)
                formatted_time = dt_object.strftime("%m-%d-%Y %H:%M:%S")
                latest_time = max(latest_time, dt_object) if latest_time else dt_object

                data_hex = log_entry["data"]
                if len(data_hex) != 194:
                    log.warning(f"⚠️ Data length mismatch in tx {tx_hash}, skipping.")
                    continue

                try:
                    liquidity = int(data_hex[2:66], 16)
                    amount0 = int(data_hex[66:130], 16)
                    amount1 = int(data_hex[130:], 16)

                    total_token0 += amount0
                    total_token1 += amount1

                    mint_transactions.append({
                        "timestamp": formatted_time,
                        "tx_hash": tx_hash,
                        "amount_token0": amount0,
                        "amount_token1": amount1,
                    })

                except Exception as e:
                    log.error(f"❌ Error parsing values: {e}")
                    continue
            
            first_time_add = datetime.datetime.fromtimestamp(
                int(data["result"][0]["timeStamp"], 16)
            ).strftime('%m-%d-%Y %H:%M:%S')
            
            latest_time_add = latest_time.strftime('%m-%d-%Y %H:%M:%S')
            total_amount_token0_add = round(total_token0, 6)
            total_amount_token1_add = round(total_token1, 6)

            return mint_transactions, latest_time_add, first_time_add, total_amount_token0_add, total_amount_token1_add

        if not retry_empty_results:
            log.warning(f"❌ No IncreaseLiquidity events found for tokenId {token_id} ({mode} mode, no retry).")
            return [], None, None, 0, 0

        log.warning(f"⏳ Retry {attempt+1}/{retries} for tokenId {token_id} ({mode} mode, no results yet)")
        time.sleep(delay)

    log.error(f"❌ No IncreaseLiquidity events found for tokenId {token_id} after {retries} retries.")
    return [], None, None, 0, 0

# # Example usage:
# npm_contract = "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"
# your_token_id = 4876027

# API_URLS = {
#     'ETH': f'https://api.etherscan.io/v2/api?chainid=1',
#     'BAS': f'https://api.etherscan.io/v2/api?chainid=8453',
#     'POL': f'https://api.etherscan.io/v2/api?chainid=137',
#     'BNB': f'https://api.etherscan.io/v2/api?chainid=56',
#     'ARB': f'https://api.etherscan.io/v2/api?chainid=42161',
#     'LIN': f'https://api.etherscan.io/v2/api?chainid=59144',
# }

# API_KEYS = {
#     'ETH': '9BZMQYAZVSVKVURIA3SPKDZEYS2DQ4NARU',
#     'BAS': 'FNNNBJ73PYIGJAIKBX31386PSEPYK6AU5Y',
#     'POL': 'FNNNBJ73PYIGJAIKBX31386PSEPYK6AU5Y',
#     'BNB': 'YXRCHCJE1BQVPDT2NA5BD713Q7WJUAJEWF',
#     'ARB': 'FNNNBJ73PYIGJAIKBX31386PSEPYK6AU5Y',
#     'LIN': '9BZMQYAZVSVKVURIA3SPKDZEYS2DQ4NARU',
# }

# mint_transactions, latest_time_add, first_time_add, total_amount_token0_add, total_amount_token1_add = get_increase_liquidity_history(API_URLS, API_KEYS, 'BNB', npm_contract, your_token_id, mode="cron")

# for tx in mint_transactions:
#     print(f"🧾 TX Hash: {tx['tx_hash']}")
#     print(f"⏰ Timestamp: {tx['timestamp']}")
#     print(f"➕ Token0 Added: {tx['amount_token0']}")
#     print(f"➕ Token1 Added: {tx['amount_token1']}\n")

# print(f"\n📌 Last Add Liquidity Time: {latest_time_add}")
# print(f"🔢 Total Token0 Added: {total_amount_token0_add}")
# print(f"🔢 Total Token1 Added: {total_amount_token1_add}\n")
