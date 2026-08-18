import requests
import datetime
from logging_setup import evm_logger as log

def get_decrease_liquidity_history(API_URLS, API_KEYS, chain, npm_contract_address, token_id):
    if chain not in API_URLS or chain not in API_KEYS:
        log.error(f"❌ No API URL or API Key for {chain}")
        return [], None, None, 0, 0

    # ✅ Correct topic for DecreaseLiquidity
    topic0 = "0x26f6a048ee9138f2c0ce266f322cb99228e8d619ae2bff30c67f8dcf9d2377b4"
    topic1 = "0x" + hex(token_id)[2:].zfill(64)
    url = API_URLS[chain]

    params = {
        "module": "logs",
        "action": "getLogs",
        "fromBlock": "0",
        "toBlock": "latest",
        "address": npm_contract_address,
        "topic0": topic0,
        "topic1": topic1,
        "apikey": API_KEYS[chain]  # ✅ Use correct chain
    }

    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        log.error(f"❌ API request failed: {e}")
        return [], None, None, 0, 0

    if not isinstance(data.get("result"), list):
        log.error(f"❌ API error for {chain}: {data.get('result')}")
        return [], None, None, 0, 0
    
    if "result" not in data or not data["result"]:
        log.error(f"❌ No DecreaseLiquidity events found for tokenId {token_id}")
        return [], None, None, 0, 0

    log.info(f"✅ Found {len(data['result'])} DecreaseLiquidity events for tokenId {token_id}")

    decrease_transactions = []
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
        if len(data_hex) < 194:
            log.warning(f"⚠️ Data length mismatch in tx {tx_hash}, skipping.")
            continue

        try:
            liquidity = int(data_hex[2:66], 16)
            amount0 = int(data_hex[66:130], 16)
            amount1 = int(data_hex[130:], 16)

            total_token0 += amount0
            total_token1 += amount1

            decrease_transactions.append({
                "timestamp": formatted_time,
                "tx_hash": tx_hash,
                "liquidity": liquidity,
                "amount_token0": amount0,
                "amount_token1": amount1
            })

        except Exception as e:
            log.error(f"❌ Error parsing data in tx {tx_hash}: {e}")
            continue

    first_time = datetime.datetime.fromtimestamp(
        int(data["result"][0]["timeStamp"], 16)
    ).strftime('%m-%d-%Y %H:%M:%S')

    latest_time_str = latest_time.strftime('%m-%d-%Y %H:%M:%S')
    total_token0 = round(total_token0, 6)
    total_token1 = round(total_token1, 6)

    return decrease_transactions, latest_time_str, first_time, total_token0, total_token1


# npm_contract = "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"
# your_token_id = 417437	

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
#     'BAS': 'WFV28BSE1T9C9Q4XFBKJESD4MXDZ322XXH',
#     'POL': 'PM8HRYCQZ4QUWC5RVJNWQVBNTBBEIY4A8F',
#     'BNB': '1Q1I7XJZDTVQY7BJ91KNE1PQT6C2DDFWHG',
#     'ARB': 'AAZFEQ2R2AYXFHV9YVDX4UJ4NKHA43WJP5',
#     'LIN': '15V5YKYIH6RCKW6YNT5NG12FZ7FS8CDVS3',
# }

# decrease_tx, latest, first, total0, total1 = get_decrease_liquidity_history(
#     API_URLS, API_KEYS, "BAS", npm_contract, your_token_id
# )

# print(f"🔥 Tổng token0 đã rút: {total0}, Tổng token1 đã rút: {total1}, TimeStamp: {latest}")