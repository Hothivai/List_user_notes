import requests
import hmac
import hashlib
import time

FUTURES_URL = "https://fapi.binance.com"
FUTURES_URL_COINM = "https://dapi.binance.com"
BASE_URL = "https://api.binance.com"

def sign_request(api_secret, params):
    """Tạo chữ ký HMAC SHA256 cho request Binance."""
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(
        api_secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"{query_string}&signature={signature}"


# =========================================================
# 🔹 FUTURES TRADING APIs
# =========================================================
def get_futures_positions(api_key, api_secret, symbol=None):
    """
    Lấy danh sách vị thế Futures (LONG / SHORT) hiện tại cho cả:
      - USDⓈ-M Futures (/fapi/v2/positionRisk)
      - COIN-M Futures (/dapi/v1/positionRisk)
    Trả về list các position có positionAmt ≠ 0.
    """
    all_positions = []

    endpoints = [
        (FUTURES_URL, "/fapi/v2/positionRisk", "USDT-M"),
        (FUTURES_URL_COINM, "/dapi/v1/positionRisk", "COIN-M"),
    ]

    for base_url, endpoint, ftype in endpoints:
        params = {"timestamp": int(time.time() * 1000)}
        if symbol:
            params["symbol"] = symbol.upper()

        query = sign_request(api_secret, params)
        url = f"{base_url}{endpoint}?{query}"

        try:
            r = requests.get(url, headers={"X-MBX-APIKEY": api_key}, timeout=10)
            if r.status_code != 200:
                print(f"❌ [{ftype}] Error {r.status_code}: {r.text}")
                continue

            data = r.json()
            for p in data:
                amt = float(p.get("positionAmt", 0))
                if abs(amt) > 0:
                    all_positions.append({
                        "symbol": p["symbol"],
                        "side": "LONG" if amt > 0 else "SHORT",
                        "amount": amt,
                        "entry": float(p.get("entryPrice", 0)),
                        "mark": float(p.get("markPrice", 0)),
                        "leverage": float(p.get("leverage", 0)),
                        "unrealizedPnl": float(p.get("unRealizedProfit", 0)),
                        "marginAsset": p.get("marginAsset", None),
                        "contractType": p.get("contractType", None),
                        "futures_type": ftype,  # USDT-M or COIN-M
                    })

        except Exception as e:
            print(f"⚠️ [{ftype}] Exception: {e}")

    return all_positions

def get_futures_orders(api_key, api_secret, symbol=None):
    """
    Lấy danh sách lệnh Futures đang mở (openOrders).
    """
    endpoint = "/fapi/v1/openOrders"
    params = {"timestamp": int(time.time() * 1000)}
    if symbol:
        params["symbol"] = symbol.upper()

    query = sign_request(api_secret, params)
    url = f"{FUTURES_URL}{endpoint}?{query}"

    r = requests.get(url, headers={"X-MBX-APIKEY": api_key})
    if r.status_code != 200:
        print(f"❌ [Binance Futures Orders] Error {r.status_code}: {r.text}")
        return []

    return r.json()


# =========================================================
# 🔹 SPOT TRADING APIs
# =========================================================
def get_spot_account(api_key, api_secret):
    """
    Lấy thông tin tài khoản Spot (số dư từng coin).
    """
    endpoint = "/api/v3/account"
    params = {"timestamp": int(time.time() * 1000)}

    query = sign_request(api_secret, params)
    url = f"{BASE_URL}{endpoint}?{query}"

    r = requests.get(url, headers={"X-MBX-APIKEY": api_key})
    if r.status_code != 200:
        print(f"❌ [Binance Spot] Error {r.status_code}: {r.text}")
        return {}

    return r.json()
