import math
import requests
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from web3 import Web3
from requests.exceptions import Timeout, RequestException

# ABI tiêu chuẩn cho ERC20 để lấy số dư
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
]

# Hỗ trợ Address Constant
NATIVE_TOKEN_ADDRESS = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
EXECUTE_ROUTE_OUTPUT_TOLERANCE_BPS = 30
PROVIDER_PRIORITY = {
    "KyberSwap": 0,
    "OKX": 1,
    "0x": 2,
}
WRAPPED_NATIVE = [
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", # WBNB
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", # WETH
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1", # WETH (ARB)
    "0x4200000000000000000000000000000000000006"  # WETH (Base/OP)
]

class V3Swapper:
    """
    Module Swapper cho EVM: Sử dụng KyberSwap Aggregator (tương tự Jupiter trên Solana).
    Chức năng: Kiểm tra số dư, tính toán Zap V3 và lấy dữ liệu giao dịch swap.
    """
    def __init__(self, chain_name, rpc_url):
        self.chain_name = chain_name.upper()
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))

        # Mapping Chain Name sang ID của KyberSwap
        self.chain_configs = {
            "BSC": "bsc", "BNB": "bsc",
            "ARB": "arbitrum", "BAS": "base",
            "ETH": "ethereum", "POLYGON": "polygon"
        }

        self.chain_ids = {
            "ETH": 1,
            "BSC": 56, 
            "BNB": 56,
            "POL": 137,
            "ARB": 42161,
            "BAS": 8453
        }

        self.chain_id = self.chain_configs.get(self.chain_name, "")
        self.api_base_url = f"https://aggregator-api.kyberswap.com/{self.chain_id}/api/v1"

        self.chain_id_0x = self.chain_ids.get(self.chain_name, 56)
        self.api_base_url_0x = f"https://api.0x.org/swap/allowance-holder/quote"

        self.api_key = "dfc27316-a8fe-4a4b-aa74-b8f2e9c49559"

        self.client_id = "NftApp"

        # OKX DEX Configuration
        self.okx_api_base = "https://www.okx.com/api/v6/dex/aggregator"
        self.okx_api_key = "1ef5d201-1cec-46db-9658-c58a67008797"
        self.okx_secret_key = "E05BA24E99B675FC9E9B9F7EE32CD232"
        self.okx_passphrase = "@Shin12398"

    def get_token_balance(self, user_address, token_address):
        """Lấy số dư thực tế (Human readable) của User"""
        user_checksum = Web3.to_checksum_address(user_address)
        
        # Native Token (BNB, ETH, etc.)
        if token_address.lower() == "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee":
            balance_wei = self.w3.eth.get_balance(user_checksum)
            return balance_wei / 1e18, 18

        # ERC20 Token
        token_checksum = Web3.to_checksum_address(token_address)
        contract = self.w3.eth.contract(address=token_checksum, abi=ERC20_ABI)
        balance_raw = contract.functions.balanceOf(user_checksum).call()
        decimals = contract.functions.decimals().call()
        return balance_raw / (10**decimals), decimals

    def calculate_optimal_swap_v3(self, total_capital_0, price_0_in_1, t_current, t_low, t_up):
        """
        Toán học V3: Tính lượng Token 0 cần swap sang Token 1 để nạp 'vừa khít'.
        """
        sqrt_p = math.sqrt(1.0001 ** t_current)
        sqrt_a = math.sqrt(1.0001 ** t_low)
        sqrt_b = math.sqrt(1.0001 ** t_up)
        if sqrt_a > sqrt_b: sqrt_a, sqrt_b = sqrt_b, sqrt_a

        if sqrt_p <= sqrt_a: return 0
        elif sqrt_p >= sqrt_b: return total_capital_0
        else:
            ratio_target = ((sqrt_p - sqrt_a) * (sqrt_p * sqrt_b)) / (sqrt_b - sqrt_p)
            amount_to_keep_0 = total_capital_0 / (1 + (ratio_target / price_0_in_1))
            return max(0, total_capital_0 - amount_to_keep_0)

    def get_kyber_route(self, token_in, token_out, amount_wei):
        """Bước 1: Lấy route tốt nhất từ KyberSwap"""
        url = f"{self.api_base_url}/routes"
        headers = {
            "X-Client-Id": self.client_id,
            "Accept":"*/*"
        }
        params = {
            "tokenIn": token_in,
            "tokenOut": token_out,
            "amountIn": str(int(amount_wei)),
            "saveGas": "true",
            "maxSplits": 1,
            "maxHops": 2
        }
        try:
            res = requests.get(url, params=params, headers=headers, timeout=8)
            data = res.json()
            if data.get("code") == 0: 
                return data["data"]
            else:
                return None
        except Exception as e:
            print(f"Kyber Route Error: {e}")
            return None

    def build_kyber_swap_data(self, route_summary, sender_address, slippage_bps=10):
        """Bước 2: Lấy dữ liệu Transaction (Encoded Data) để gửi lên chuỗi"""
        url = f"{self.api_base_url}/route/build"
        headers = {
            "X-Client-Id": self.client_id,
            "Accept":"*/*"
        }
        payload = {
            "routeSummary": route_summary,
            "sender": Web3.to_checksum_address(sender_address),
            "recipient": Web3.to_checksum_address(sender_address),
            "slippageTolerance": slippage_bps, # Ví dụ 50 = 0.5%
            "deadline": int(time.time()) + 1200,
            "source": "sniper_tool"
        }
        try:
            res = requests.post(url, json=payload, headers=headers, timeout=8)
            data = res.json()
            if data.get("code") == 0:
                return data["data"]
            else:
                return None
        except Exception as e:
            print(f"Kyber Build Error: {e}")
            return None

    def prepare_zap_and_swap(self, user_address, total_cap_0, token0_info, token1_info, pool_state):
        """
        Tương tự calculate_and_prepare_swaps của Solana.
        Tính toán Zap -> Lấy Route -> Build Transaction.
        """
        # 1. Kiểm tra số dư thực tế
        bal0, dec0 = self.get_token_balance(user_address, token0_info['address'])
        print(f"Token 0 (USDT): {bal0} {token0_info['symbol']}")
        
        if bal0 < total_cap_0:
            return {"error": "INSUFFICIENT_BALANCE", "msg": f"Bạn cần {total_cap_0} nhưng chỉ có {bal0}"}

        # 2. Tính toán lượng Swap tối ưu (Zap)
        # Giả sử giá Token 0 quy ra Token 1
        price_0_in_1 = token1_info['price_in_usd'] / token0_info['price_in_usd'] if token0_info['price_in_usd'] > 0 else 0
        
        swap_amount_human = self.calculate_optimal_swap_v3(
            total_cap_0, 
            price_0_in_1,
            pool_state['currentTick'],
            pool_state['range'][0],
            pool_state['range'][1]
        )

        if swap_amount_human <= 0:
            return {"type": "NO_SWAP_NEEDED", "msg": "Giá nằm ngoài dải hoặc chỉ cần Token 0"}

        # 3. Thực hiện lấy dữ liệu từ KyberSwap
        swap_amount_wei = int(swap_amount_human * (10**dec0))
        route = self.get_kyber_route(token0_info['address'], token1_info['address'], swap_amount_wei)
        
        if not route:
            return {"error": "NO_ROUTE_FOUND"}

        swap_data = self.build_kyber_swap_data(route["routeSummary"], user_address)
        
        return {
            "type": "SUCCESS",
            "swap_amount": swap_amount_human,
            "token_in": token0_info['symbol'],
            "token_out": token1_info['symbol'],
            "transaction_data": swap_data, # Chứa 'data', 'to', 'value', 'gas'
            "route_summary": route["routeSummary"]
        }

    def get_0x_swap_quote(self, token_in, token_out, amount_in_wei, taker_address, slippage_bps=10, skip_validation=True):
        """
        Lấy báo giá và Transaction Data trực tiếp từ 0x API Version 2.
        """
        # Header chuẩn của 0x API V2
        headers = {
            "0x-api-key": self.api_key,
            "0x-version": "v2"
        }

        # Trong V2, slippage dùng theo định dạng Bps (Basis points)
        # quan trọng: skipValidation=true giúp lấy được quote ngay cả khi chưa Approve hoặc số dư = 0
        params = {
            "chainId": self.chain_id_0x,
            "buyToken": token_out,
            "sellToken": token_in,
            "sellAmount": str(amount_in_wei),
            "taker": taker_address,
            "slippageBps": str(slippage_bps),
        }
        if skip_validation:
            params["skipValidation"] = "true"

        try:
            print(f"🔄 [0X SWAPPER V2] Đang tìm Route Matcha cho {amount_in_wei} wei...")
            response = requests.get(self.api_base_url_0x, params=params, headers=headers, timeout=8)
            
            if response.status_code != 200:
                print(f"⚠️ [0X SWAPPER V2] Lỗi API ({response.status_code}): {response.text}")
                return None
                
            data = response.json()
            if not data:
                return None

            tx_data = data.get("transaction", {})
            buy_amount = data.get("buyAmount", "0")
            if not tx_data:
                print(f"⚠️ [0X SWAPPER V2] Không tìm thấy dữ liệu transaction: {data}")
                return None
            
            print(f"✅ [0X SWAPPER V2] Tìm thấy Route! Sẽ nhận được: {buy_amount} wei")
            
            # Trích xuất route display từ mảng fills của 0x V2
            router_names = []
            route_data = data.get("route") or {}
            fills = route_data.get("fills") or []
            
            for dex_info in fills:
                name = dex_info.get("source", "Unknown")
                if name not in router_names:
                    router_names.append(name)
            route_str = " -> ".join(router_names) if router_names else "0x Matcha Route"

            # Trích xuất allowanceTarget an toàn
            is_native = token_in.lower() == "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
            allowance_target = None
            
            if not is_native:
                issues = data.get("issues") or {}
                allowance_info = issues.get("allowance") or {}
                allowance_target = allowance_info.get("spender") or data.get("allowanceTarget")
                       
            # Debug verify fields
            target_to = tx_data.get("to")
            target_value = tx_data.get("value", "0")
            print(f"📡 [0X DEBUG] To: {target_to}, Value: {target_value}, Spender: {allowance_target}")

            return {
                "provider": "0x",
                "to": target_to,
                "data": tx_data.get("data"),
                "value": target_value,
                "allowanceTarget": allowance_target,
                "buyAmount": buy_amount,
                "estimatedGas": tx_data.get("gas", "0"),
                "gasPrice": tx_data.get("gasPrice", "0"),
                "route_display": route_str
            }
            
        except Timeout:
            print(f"⏳ [0X SWAPPER V2] Timeout: 0x API phản hồi quá chậm (Trên 15s).")
            return None
        except RequestException as e:
            print(f"⚠️ [0X SWAPPER V2] Lỗi kết nối 0x API: {e}")
            return None

    def get_okx_swap_quote(self, token_in, token_out, amount_in_wei, user_address, slippage_bps=10):
        """
        Lấy báo giá và Transaction Data từ OKX DEX Aggregator V6.
        """
        import hmac
        import base64
        import datetime
        from urllib.parse import urlencode

        v6_path = "/api/v6/dex/aggregator/swap"
        url = f"https://www.okx.com{v6_path}"
        
        slippage_pct = float(slippage_bps) / 100

        params = {
            "chainIndex": self.chain_id_0x,
            "amount": str(amount_in_wei),
            "fromTokenAddress": token_in,
            "toTokenAddress": token_out,
            "slippagePercent": str(slippage_pct),
            "userWalletAddress": user_address
        }

        # Auth logic cho OKX
        now = datetime.datetime.now(datetime.timezone.utc)
        timestamp = now.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        
        query_string = urlencode(params)
        request_path_for_sign = f"{v6_path}?{query_string}"
        message = timestamp + "GET" + request_path_for_sign
        
        signature = ""
        if hasattr(self, 'okx_secret_key') and self.okx_secret_key:
            mac = hmac.new(bytes(self.okx_secret_key, encoding='utf8'), bytes(message, encoding='utf-8'), digestmod='sha256')
            d = mac.digest()
            signature = base64.b64encode(d).decode('utf-8')

        headers = {
            "OK-ACCESS-KEY": self.okx_api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": getattr(self, 'okx_passphrase', ""),
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        try:
            print(f"🔄 [OKX SWAPPER V6] Đang tìm Route trên OKX cho {amount_in_wei} wei...")
            res = requests.get(url, params=params, headers=headers, timeout=8)
            data = res.json()

            if data.get("code") == "0" and data.get("data"):
                swap_data = data["data"][0]
                tx = swap_data.get("tx", {})
                router_result = swap_data.get("routerResult", {})
                
                # Trích xuất route display từ dexRouterList của V6
                router_names = []
                for dex_info in router_result.get("dexRouterList", []):
                    protocol = dex_info.get("dexProtocol", {})
                    name = protocol.get("dexName", "Unknown")
                    if name not in router_names:
                        router_names.append(name)
                route_str = " -> ".join(router_names) if router_names else "OKX Route"

                buy_amount = router_result.get("toTokenAmount", "0")
                is_native = token_in.lower() == "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
                
                print(f"✅ [OKX SWAPPER V6] Tìm thấy Route! Sẽ nhận được: {buy_amount} wei")
                
                # Lấy allowanceTarget từ approveAddress nếu có (Native token không cần approve)
                allowance_target = None
                if not is_native:
                    allowance_target = swap_data.get("approveAddress")
                    
                    # Nếu không có approveAddress, gọi thêm endpoint approve-transaction
                    if not allowance_target:
                        try:
                            approve_url = "https://www.okx.com/api/v6/dex/aggregator/approve-transaction"
                            approve_params = {
                                "chainIndex": self.chain_id_0x,
                                "tokenContractAddress": token_in,
                                "approveAmount": str(amount_in_wei)
                            }
                            approve_path = "/api/v6/dex/aggregator/approve-transaction"
                            approve_msg = timestamp + "GET" + f"{approve_path}?{urlencode(approve_params)}"
                            
                            approve_sig = ""
                            if hasattr(self, 'okx_secret_key') and self.okx_secret_key:
                                mac = hmac.new(bytes(self.okx_secret_key, encoding='utf8'), bytes(approve_msg, encoding='utf-8'), digestmod='sha256')
                                approve_sig = base64.b64encode(mac.digest()).decode('utf-8')
                            
                            approve_headers = headers.copy()
                            approve_headers["OK-ACCESS-SIGN"] = approve_sig
                            
                            approve_res = requests.get(approve_url, params=approve_params, headers=approve_headers, timeout=5)
                            approve_data = approve_res.json()
                            if approve_data.get("code") == "0" and approve_data.get("data"):
                                allowance_target = approve_data["data"][0].get("dexContractAddress")
                        except Exception as e:
                            print(f"⚠️ [OKX SWAPPER V6] Spender fetch failed: {e}")

                # Fallback allowanceTarget
                if not allowance_target and not is_native:
                    allowance_target = swap_data.get("routerAddress") or tx.get("to")

                # Debug verify fields
                target_to = tx.get("to") or swap_data.get("routerAddress")
                target_value = tx.get("value") or ("0" if not is_native else str(amount_in_wei))
                
                print(f"📡 [OKX DEBUG] To: {target_to}, Value: {target_value}, Spender: {allowance_target}")

                return {
                    "provider": "OKX",
                    "to": target_to,
                    "data": tx.get("data"),
                    "value": target_value,
                    "allowanceTarget": allowance_target,
                    "buyAmount": buy_amount,
                    "estimatedGas": tx.get("gas", "0"),
                    "gasPrice": tx.get("gasPrice", "0"),
                    "route_display": route_str,
                    "price_impact": float(router_result.get("priceImpactPercent", 0))
                }
            else:
                print(f"⚠️ [OKX SWAPPER V6] Lỗi: {data.get('msg') or data}")
                return None
        except Exception as e:
            print(f"⚠️ [OKX SWAPPER V6] Lỗi kết nối: {e}")
            return None

    def _is_native_token(self, token_addr):
        return (token_addr or "").lower() == NATIVE_TOKEN_ADDRESS

    def _is_valid_quote_for_execute(self, quote, token_in):
        if not quote:
            return False
        try:
            if int(quote.get("buyAmount", "0")) <= 0:
                return False
        except Exception:
            return False
        if not quote.get("to") or not quote.get("data"):
            return False
        if not self._is_native_token(token_in) and not quote.get("allowanceTarget"):
            return False
        return True

    def _select_best_route(self, quotes, quote_context="execute"):
        if not quotes:
            return None
        if quote_context != "execute":
            return max(quotes, key=lambda x: int(x["buyAmount"]))

        valid_quotes = []
        for quote in quotes:
            if quote.get("provider") not in PROVIDER_PRIORITY:
                continue
            try:
                if int(quote.get("buyAmount", "0")) > 0:
                    valid_quotes.append(quote)
            except Exception:
                continue
        if not valid_quotes:
            return None

        best_out = max(int(q["buyAmount"]) for q in valid_quotes)
        min_safe_out = best_out * (10000 - EXECUTE_ROUTE_OUTPUT_TOLERANCE_BPS) // 10000
        safe_quotes = [q for q in valid_quotes if int(q["buyAmount"]) >= min_safe_out]
        return min(
            safe_quotes,
            key=lambda q: (PROVIDER_PRIORITY.get(q.get("provider"), 99), -int(q["buyAmount"]))
        )

    def get_best_swap_route(self, token_in, token_out, amount_in_wei, user_address, slippage_bps=50, quote_context="execute"):
        """
        So sánh báo giá từ các Aggregator và chọn Route tốt nhất (nhiều token nhận về nhất).
        """
        # Không tự động ánh xạ sang Native (0xeee...) vì ví người dùng đang giữ Wrapped token từ Pool.
        # Giữ nguyên address gốc để Aggregator build giao dịch ERC20 chuẩn xác.
        
        def fetch_kyber_quote():
            print(f"🔄 [KYBER SWAPPER] Đang tìm Route trên Kyber cho {amount_in_wei} wei...")
            kyber_route = self.get_kyber_route(token_in, token_out, amount_in_wei)
            if not kyber_route:
                return None
            route_summary = kyber_route.get("routeSummary") or {}
            kyber_tx = self.build_kyber_swap_data(route_summary, user_address, slippage_bps)
            if route_summary:
                print(f"✅ [KYBER SWAPPER] Tìm thấy Route! Sẽ nhận được: {kyber_route['routeSummary']['amountOut']} wei")
            if not kyber_tx:
                return None

            impact = 0.0
            try:
                summary = kyber_route.get('routeSummary', {})
                in_usd = float(summary.get('amountInUsd', 0))
                out_usd = float(summary.get('amountOutUsd', 0))
                if in_usd > 0:
                    impact = ((in_usd - out_usd) / in_usd) * 100
            except Exception:
                pass

            return {
                "provider": "KyberSwap",
                "buyAmount": kyber_route["routeSummary"].get("amountOut", "0"),
                "data": kyber_tx["data"],
                "to": kyber_tx["routerAddress"],
                "value": kyber_tx.get("value", "0"),
                "allowanceTarget": kyber_tx["routerAddress"],
                "price_impact": impact,
                "route_display": "Kyber -> " + " -> ".join([h["exchange"] for h in kyber_route["routeSummary"]["route"][0]])
            }

        quote_jobs = {
            "Kyber": fetch_kyber_quote,
            "0x": lambda: self.get_0x_swap_quote(
                token_in,
                token_out,
                amount_in_wei,
                user_address,
                slippage_bps,
                skip_validation=(quote_context != "execute")
            ),
            "OKX": lambda: self.get_okx_swap_quote(token_in, token_out, amount_in_wei, user_address, slippage_bps)
        }

        quotes = []
        with ThreadPoolExecutor(max_workers=len(quote_jobs)) as executor:
            futures = {executor.submit(fn): name for name, fn in quote_jobs.items()}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    quote = future.result()
                    if quote and (quote_context != "execute" or self._is_valid_quote_for_execute(quote, token_in)):
                        quotes.append(quote)
                except Exception as e:
                    print(f"❌ Lỗi lấy quote {name}: {e}")

        if not quotes:
            return None

        best_quote = self._select_best_route(quotes, quote_context=quote_context)
        if not best_quote:
            return None
        print(f"🏆 Best Route found via {best_quote['provider']}! (Amount Out: {best_quote['buyAmount']})")
        return best_quote

        # 1. Fetch Kyber Route
        try:
            print(f"🔄 [KYBER SWAPPER] Đang tìm Route trên Kyber cho {amount_in_wei} wei...")
            kyber_route = self.get_kyber_route(token_in, token_out, amount_in_wei)
            if kyber_route:
                # kyber_tx = self.build_kyber_swap_data(kyber_route, user_address, slippage_bps)
                route_summary = kyber_route.get("routeSummary") or {}
                kyber_tx = self.build_kyber_swap_data(route_summary, user_address, slippage_bps)
                if route_summary:
                    print(f"✅ [KYBER SWAPPER] Tìm thấy Route! Sẽ nhận được: {kyber_route['routeSummary']['amountOut']} wei")
                
                if kyber_tx:
                    impact = 0.0
                    try:
                        summary = kyber_route.get('routeSummary', {})
                        in_usd = float(summary.get('amountInUsd', 0))
                        out_usd = float(summary.get('amountOutUsd', 0))
                        if in_usd > 0: impact = ((in_usd - out_usd) / in_usd) * 100
                    except: pass

                    quotes.append({
                        "provider": "KyberSwap",
                        "buyAmount": kyber_route["routeSummary"].get("amountOut", "0"),
                        "data": kyber_tx["data"],
                        "to": kyber_tx["routerAddress"],
                        "value": kyber_tx.get("value", "0"),
                        "allowanceTarget": kyber_tx["routerAddress"],
                        "price_impact": impact,
                        "route_display": "Kyber -> " + " -> ".join([h["exchange"] for h in kyber_route["routeSummary"]["route"][0]])
                    })
        except Exception as e:
            print(f"❌ Lỗi lấy quote Kyber: {e}")
        
        # 2. Fetch 0x Route
        try:
            zero_x_quote = self.get_0x_swap_quote(token_in, token_out, amount_in_wei, user_address, slippage_bps)
            if zero_x_quote:
                quotes.append(zero_x_quote)
        except Exception as e:
            print(f"❌ Lỗi lấy quote 0x: {e}")

        # 3. Fetch OKX Route
        try:
            okx_quote = self.get_okx_swap_quote(token_in, token_out, amount_in_wei, user_address, slippage_bps)
            if okx_quote:
                quotes.append(okx_quote)
        except Exception as e:
            print(f"❌ Lỗi lấy quote OKX: {e}")

        if not quotes:
            return None

        # So sánh theo buyAmount (Token nhận được)
        best_quote = max(quotes, key=lambda x: int(x["buyAmount"]))
        
        print(f"🏆 Best Route found via {best_quote['provider']}! (Amount Out: {best_quote['buyAmount']})")
        return best_quote

if __name__ == "__main__":
    # TEST MOCK DATA
    RPC_URL = "https://bsc-dataseed.binance.org"
    USER = "0x88De2ab47352779494547CacCB31EE1a133Dd334" # Wallet Address
    
    # Thông tin Pool TRIA/USDT
    USDT_ADDR = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
    TRIA_ADDR = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"
    
    P_STATE = {"currentTick": 38118, "range": [33100, 43100]}
    T0_INFO = {"address": USDT_ADDR, "symbol": "USDT", "price_in_usd": 1.0}
    T1_INFO = {"address": TRIA_ADDR, "symbol": "TRIA", "price_in_usd": 0.0225}
    
    swapper = V3Swapper("BNB", RPC_URL)
    route = swapper.get_best_swap_route(USDT_ADDR, TRIA_ADDR, 3000000000000000, USER)
    print(f"Route Result: {route}")

    # result = swapper.prepare_zap_and_swap(USER, 62.0, T0_INFO, T1_INFO, P_STATE)

    # result_0x = swapper.get_0x_swap_quote(USDT_ADDR, TRIA_ADDR, 1000000, USER)
    # print(f"0x Result: {result_0x}")

    # print("\n=== KẾT QUẢ PHÂN TÍCH ZAP (EVM KYBERSWAP) ===")
    # if "error" in result:
    #     print(f"❌ Lỗi: {result['msg'] if 'msg' in result else result['error']}")
    # elif result["type"] == "SUCCESS":
    #     print(f"✅ Cần Swap: {result['swap_amount']:.4f} {result['token_in']} -> {result['token_out']}")
    #     print(f"Route Summary: {result['route_summary']}")
    #     print(f"📡 Router: {result['transaction_data']['routerAddress']}")
    #     print(f"📦 Encoded Data: {result['transaction_data']['data'][:50]}...")
