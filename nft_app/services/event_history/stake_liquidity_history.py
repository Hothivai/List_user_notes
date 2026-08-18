import requests
import datetime
import json
from logging_setup import evm_logger as log

def get_stake_time(API_URLS, API_KEYS, chain, masterchef_address, token_id):
    if chain not in API_URLS or chain not in API_KEYS:
        log.error(f"❌ No API URL or API Key for {chain}")
        return None
    
    topic0 = "0xb19157bff94fdd40c58c7d4a5d52e8eb8c2d570ca17b322b49a2bbbeedc82fbf"
    topic3 = "0x" + hex(token_id)[2:].zfill(64)

    url = API_URLS[chain]
    
    params = {
        "module": "logs",
        "action": "getLogs",
        "fromBlock": "0",
        "toBlock": "latest",
        "address": masterchef_address,
        "topic0": topic0,
        "topic3": topic3,
        "apikey": API_KEYS[chain]
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    
    response = requests.get(url, params=params, headers=headers)
    data = response.json()

    # ✅ Kiểm tra kết quả hợp lệ
    result = data.get("result", [])
    if not isinstance(result, list) or len(result) == 0:
        log.error("❌ Not found stake event (Deposit)")
        return None

    # ✅ Đảm bảo từng phần tử là dict
    parsed_result = []
    for item in result:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError:
                continue
        if isinstance(item, dict) and "blockNumber" in item:
            parsed_result.append(item)

    if not parsed_result:
        log.error("❌ Result format invalid (no valid logs)")
        return None

    log.info(f"✅ Found {len(parsed_result['result'])} Stake liquidity events for tokenId {token_id}")
    
    # ✅ Lấy log mới nhất
    latest_log = max(parsed_result, key=lambda log: int(log["blockNumber"], 16))
    timestamp = int(latest_log["timeStamp"], 16)
    dt_object = datetime.datetime.fromtimestamp(timestamp)
    latest_time_stake = dt_object.strftime("%m-%d-%Y %H:%M:%S")

    return latest_time_stake

# masterchef_address = "0x556B9306565093C855AEA9AE92A594704c2Cd59e"
# token_id = 1663885
# bsc_api_key = "1Q1I7XJZDTVQY7BJ91KNE1PQT6C2DDFWHG"

# stake_time = get_stake_time(masterchef_address, token_id, bsc_api_key)
# if stake_time:
#     print("📌 Stake time:", stake_time)
    
#     latest_time_stake = datetime.datetime.strptime(stake_time, "%m-%d-%Y %H:%M:%S")
#     latest_time_stake_timestamp = latest_time_stake.timestamp()
    
#     now = datetime.datetime.now()
#     now_timestamp = now.timestamp()
#     time_elapsed_days = (now_timestamp - latest_time_stake_timestamp) / (3600 * 24)
    
#     print(f"⏳ Time Elapsed: {time_elapsed_days:.2f} days")