import requests
import datetime
from logging_setup import evm_logger as log

def get_last_collect_time(API_URLS, API_KEYS, chain, npm_address, token_id):
    if chain not in API_URLS or chain not in API_KEYS:
        log.error(f"❌ No API URL or API Key for {chain}")
        return None
    
    topic0 = "0x40d0efd1a53d60ecbf40971b9daf7dc90178c3aadc7aab1765632738fa8b8f01"  # Collect event
    topic1 = "0x" + hex(token_id)[2:].zfill(64)

    url = API_URLS[chain]
    
    params = {
        "module": "logs",
        "action": "getLogs",
        "fromBlock": "0",
        "toBlock": "latest",
        "address": npm_address,
        "topic0": topic0,
        "topic1": topic1,
        "apikey": API_KEYS[chain]
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    try:
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        log.error(f"❌ API request failed: {e}")
        return None

    if not data.get("result"):
        log.error(f"❌ Not found any collect event for tokenId {token_id}")
        return None

    try:
        latest_log = max(data["result"], key=lambda log: int(log["blockNumber"], 16))
        timestamp_hex = latest_log.get("timeStamp")
        if not timestamp_hex:
            log.warning(f"⚠️ No timestamp found in latest log for tokenId {token_id}")
            return None

        timestamp = int(timestamp_hex, 16)
        dt_object = datetime.datetime.fromtimestamp(timestamp)
        return dt_object.strftime("%m-%d-%Y %H:%M:%S")
    
    except Exception as e:
        log.error(f"❌ Not found any collect event for this tokenId: {e}")
        return None

# # Example usage
# npm_address = "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"  # NPM contract on BSC
# token_id = 1312528
# bsc_api_key = "1Q1I7XJZDTVQY7BJ91KNE1PQT6C2DDFWHG"

# last_collect_time = get_last_collect_time(npm_address, token_id, bsc_api_key)
# if last_collect_time:
#     print("🕒 Last fee collect time:", last_collect_time)
