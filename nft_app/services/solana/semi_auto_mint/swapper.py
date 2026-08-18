import sys
import os

# Đường dẫn đến thư mục flask_app (chứa thư mục services)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../'))

# Thêm vào sys.path
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import requests
import json
import base64
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.types import TxOpts, TokenAccountOpts
from solana.rpc.commitment import Confirmed
from services.solana.semi_auto_mint.reward_estimator import RewardEstimator
from services.solana.semi_auto_mint.scan_pool import analyze_pool_ticks

# Constants cho Jupiter API (Updated to V1 standard endpoint)
JUPITER_QUOTE_API = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API = "https://api.jup.ag/swap/v1/swap"

# Token Mints phổ biến
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# An toàn phí Gas (SOL)
GAS_BUFFER = 0.02 

class JupiterSwapper:
    def __init__(self, rpc_client: Client, payer_keypair: Keypair = None, api_key: str = None):
        """
        :param rpc_client: Solana RPC Client
        :param payer_keypair: (Optional) Chỉ cần nếu Backend muốn ký thay user.
        :param api_key: (Optional) Jupiter API Key
        """
        self.client = rpc_client
        self.payer = payer_keypair
        self.api_key = api_key
        
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def get_token_balance_human(self, user_pubkey: Pubkey, mint_address: str, decimals: int = None) -> float:
        """
        Lấy số dư thực tế của user cho 1 token cụ thể (Native SOL hoặc SPL Token).
        Trả về số dư dạng Human (VD: 1.5 SOL).
        """
        try:
            # CASE 1: NATIVE SOL
            if mint_address == SOL_MINT:
                # Lấy balance Lamports
                resp = self.client.get_balance(user_pubkey)
                lamports = resp.value
                return lamports / (10 ** 9)

            # CASE 2: SPL TOKEN
            mint_pubkey = Pubkey.from_string(mint_address)
            
            # Tìm tất cả token account của user sở hữu mint này
            # Sử dụng encoding jsonParsed để lấy luôn uiAmount
            opts = TokenAccountOpts(mint=mint_pubkey)
            resp = self.client.get_token_accounts_by_owner_json_parsed(user_pubkey, opts)
            
            accounts = resp.value
            if not accounts:
                return 0.0
            
            # Cộng dồn số dư nếu user có nhiều token account cho cùng 1 mint (dù hiếm)
            total_balance = 0.0
            for acc in accounts:
                data = acc.account.data.parsed
                info = data.get('info', {})
                token_amount = info.get('tokenAmount', {})
                ui_amount = token_amount.get('uiAmount', 0.0)
                if ui_amount:
                    total_balance += ui_amount
            
            return total_balance

        except Exception as e:
            print(f"Error fetching balance for {mint_address}: {e}")
            return 0.0

    def get_quote(self, input_mint: str, output_mint: str, amount_in_lamports: int, slippage_bps: int = 50):
        """Lấy báo giá swap từ Jupiter V1 API."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount_in_lamports)),
            "slippageBps": slippage_bps,
            "swapMode": "ExactIn",
            "restrictIntermediateTokens": "true",
        }
        
        headers = {}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        
        try:
            response = self.session.get(JUPITER_QUOTE_API, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error fetching quote: {e}")
            return None

    def get_swap_tx(self, quote_response, user_pubkey_str: str):
        """Lấy serialized transaction từ Jupiter (Unsigned)."""
        payload = {
            "quoteResponse": quote_response,
            "userPublicKey": user_pubkey_str,
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": 10000
        }
        
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        
        try:
            response = self.session.post(JUPITER_SWAP_API, json=payload, headers=headers, timeout=10)
            response.raise_for_status()
            return response.json().get("swapTransaction")
        except requests.exceptions.RequestException as e:
            print(f"Error building swap tx: {e}")
            return None

    def calculate_and_prepare_swaps(self, user_pubkey_str: str, required_assets: dict, current_balances: dict = None, mints_map: dict = None, slippage_bps: int = 50):
        user_pubkey = Pubkey.from_string(user_pubkey_str)
        
        # 1. Fetch Balance
        if current_balances is None:
            if mints_map is None:
                raise ValueError("Mints map required to fetch balances")
            
            print(f"Fetching real-time balances for {user_pubkey_str}...")
            token0_mint = mints_map['token0']
            token1_mint = mints_map['token1']
            
            bal0 = self.get_token_balance_human(user_pubkey, token0_mint)
            bal1 = self.get_token_balance_human(user_pubkey, token1_mint)
            
            current_balances = {
                'token0_balance': bal0,
                'token1_balance': bal1
            }
            print(f"Real-time Balances: {current_balances}")
        
        # 2. Config
        TOKEN0_MINT = mints_map.get('token0', SOL_MINT) if mints_map else SOL_MINT
        TOKEN0_DECIMALS = mints_map.get('decimals0', 9) if mints_map else 9
        TOKEN0_SYMBOL = mints_map.get('symbol0', 'SOL') if mints_map else 'SOL'
        
        TOKEN1_MINT = mints_map.get('token1', USDC_MINT) if mints_map else USDC_MINT
        TOKEN1_DECIMALS = mints_map.get('decimals1', 6) if mints_map else 6
        TOKEN1_SYMBOL = mints_map.get('symbol1', 'USDC') if mints_map else 'USDC'

        req_0 = required_assets.get('token0_amount', 0)
        req_1 = required_assets.get('token1_amount', 0)
        
        bal_0 = current_balances.get('token0_balance', 0)
        bal_1 = current_balances.get('token1_balance', 0)

        missing_0 = max(0, req_0 - bal_0)
        missing_1 = max(0, req_1 - bal_1)
        
        # Check thiếu cả 2
        if missing_0 > 0 and missing_1 > 0:
            return [{
                "type": "ERROR",
                "code": "INSUFFICIENT_FUNDS_BOTH",
                "description": f"Insufficient balance for both tokens. Need {missing_0:.4f} {TOKEN0_SYMBOL} and {missing_1:.4f} {TOKEN1_SYMBOL}.",
                "missing": {"token0": missing_0, "token1": missing_1}
            }], 0.0
        
        swaps_payload = []
        price_impact_percent = 0.0

        # CASE 1: Thiếu Token 1 (Ví dụ: Thiếu SOL hoặc USDC)
        # Cần dùng dư thừa của Token 0 để mua
        if missing_1 > 0:
            excess_0 = bal_0 - req_0
            
            # Nếu Token 0 là SOL, phải trừ Buffer Gas
            if str(TOKEN0_MINT) == SOL_MINT:
                excess_0 -= GAS_BUFFER

            if excess_0 <= 0:
                 return [{
                    "type": "ERROR",
                    "code": "INSUFFICIENT_FUNDS_TOKEN0",
                    "description": f"Insufficient {TOKEN0_SYMBOL} balance. {TOKEN0_SYMBOL} is not enough (after subtracting gas) to swap.",
                }], price_impact_percent

            print(f"[Logic] Missing {missing_1} {TOKEN1_SYMBOL}. Using excess {TOKEN0_SYMBOL} ({excess_0:.4f}) to buy.")
            
            # Lấy giá tham chiếu
            test_quote = self.get_quote(TOKEN0_MINT, TOKEN1_MINT, 1 * (10**TOKEN0_DECIMALS), slippage_bps) 
            
            if test_quote and 'outAmount' in test_quote:
                out_human = int(test_quote['outAmount']) / (10**TOKEN1_DECIMALS)
                price_0_vs_1 = out_human 
                
                amount_in_needed = (missing_1 / price_0_vs_1) * 1.01 # Buffer 1% slippage
                
                if amount_in_needed > excess_0:
                     return [{
                        "type": "ERROR",
                        "code": "INSUFFICIENT_SWAP_BALANCE",
                        "description": f"Need to sell {amount_in_needed:.4f} {TOKEN0_SYMBOL} to buy enough {TOKEN1_SYMBOL}, but only have {excess_0:.4f}.",
                        "missing": {"token0_needed": amount_in_needed, "token0_available": excess_0}
                    }], price_impact_percent

                amount_in_lamports = int(amount_in_needed * (10**TOKEN0_DECIMALS))
                real_quote = self.get_quote(TOKEN0_MINT, TOKEN1_MINT, amount_in_lamports, slippage_bps)
                print(f"Quote: {real_quote}")
                
                if real_quote:
                    route_labels = [step['swapInfo']['label'] for step in real_quote.get('routePlan', [])]
                    route_display = " -> ".join(route_labels)
                    print(route_display, route_labels)
                    
                    tx_base64 = self.get_swap_tx(real_quote, user_pubkey_str)
                    price_impact_percent = float(real_quote.get('priceImpactPct', 0.0))
                    
                    if tx_base64:
                        swaps_payload.append({
                            "type": "SWAP_0_TO_1",
                            "description": f"Swap {amount_in_needed:.4f} {TOKEN0_SYMBOL} -> {missing_1:.2f} {TOKEN1_SYMBOL} (Auto-balance)",
                            "tx_base64": tx_base64,
                            "route": route_display,
                            "route_steps": route_labels
                        })

        # CASE 2: Thiếu Token 0 (TRƯỜNG HỢP CỦA BẠN: Meme coin = 0)
        # Cần dùng dư thừa của Token 1 (Main Token: SOL/USDC) để mua
        elif missing_0 > 0:
            excess_1 = bal_1 - req_1
            
            # Nếu Token 1 là SOL, phải trừ Buffer Gas
            if str(TOKEN1_MINT) == SOL_MINT:
                excess_1 -= GAS_BUFFER

            if excess_1 <= 0:
                 return [{
                    "type": "ERROR",
                    "code": "INSUFFICIENT_FUNDS_TOKEN1",
                    "description": f"Insufficient {TOKEN1_SYMBOL} balance. {TOKEN1_SYMBOL} is not enough (after subtracting gas) to swap.",
                }], price_impact_percent

            print(f"[Logic] Missing {missing_0} {TOKEN0_SYMBOL}. Using excess {TOKEN1_SYMBOL} ({excess_1:.4f}) to buy.")

            test_quote = self.get_quote(TOKEN1_MINT, TOKEN0_MINT, 1 * (10**TOKEN1_DECIMALS), slippage_bps)
            
            if test_quote and 'outAmount' in test_quote:
                out_human = int(test_quote['outAmount']) / (10**TOKEN0_DECIMALS)
                price_1_vs_0 = out_human
                
                amount_in_needed = (missing_0 / price_1_vs_0) * 1.01 # Buffer 1% slippage
                
                if amount_in_needed > excess_1:
                     return [{
                        "type": "ERROR",
                        "code": "INSUFFICIENT_SWAP_BALANCE",
                        "description": f"Need to sell {amount_in_needed:.4f} {TOKEN1_SYMBOL} to buy enough {TOKEN0_SYMBOL}, but only have {excess_1:.4f}.",
                        "missing": {"token1_needed": amount_in_needed, "token1_available": excess_1}
                    }], price_impact_percent

                amount_in_lamports = int(amount_in_needed * (10**TOKEN1_DECIMALS))
                real_quote = self.get_quote(TOKEN1_MINT, TOKEN0_MINT, amount_in_lamports, slippage_bps)
                print(f"Quote: {real_quote}")
                
                if real_quote:
                    route_labels = [step['swapInfo']['label'] for step in real_quote.get('routePlan', [])]
                    route_display = " -> ".join(route_labels)
                    print(route_display, route_labels)
                    
                    tx_base64 = self.get_swap_tx(real_quote, user_pubkey_str)
                    price_impact_percent = float(real_quote.get('priceImpactPct', 0.0))
                    if tx_base64:
                        swaps_payload.append({
                            "type": "SWAP_1_TO_0",
                            "description": f"Swap {amount_in_needed:.4f} {TOKEN1_SYMBOL} -> {missing_0:.4f} {TOKEN0_SYMBOL} (Auto-balance)",
                            "tx_base64": tx_base64,
                            "route": route_display,
                            "route_steps": route_labels
                        })
                    
        return swaps_payload, price_impact_percent

# # --- INTEGRATION TEST BLOCK ---
# if __name__ == "__main__":
#     # Thay bằng RPC xịn của bạn
#     RPC_URL = "https://shy-spring-card.solana-mainnet.quiknode.pro/6a97979ed162924bd71e878f5517215efab54766" 
#     API_KEY = "87eef807-0114-49ba-a50c-7ec86337a08d"
#     USER_WALLET_ADDRESS = "HJncdQqZwAjD5sCTP2dxqqxzSF1XQrFdXwPYJgAj1dma"
#     program_id = Pubkey.from_string("HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq")
#     pool_id = Pubkey.from_string("4FSrFjSMePHfRZiNaT9WxrRV8wqLcNnevjruG4zJWbpQ")
#     user_multiplier = 2.0

#     rpc_client = Client(RPC_URL)
#     swapper = JupiterSwapper(rpc_client, api_key=API_KEY)
    
#     pool_ranges = analyze_pool_ticks(rpc_client, program_id, pool_id)
    
#     mock_pool_info = pool_ranges.get("pool_info", {})
#     token_0 = mock_pool_info.get("token_mint_0")
#     token_1 = mock_pool_info.get("token_mint_1")
#     decimals_0 = mock_pool_info.get("mint_decimals_0")
#     decimals_1 = mock_pool_info.get("mint_decimals_1")
#     symbol_0 = pool_ranges.get("token0_symbol")
#     symbol_1 = pool_ranges.get("token1_symbol")
    
#     mock_sample_position_list = pool_ranges.get("personal_price_ranges", [])  
    
#     estimator = RewardEstimator(pool_info=mock_pool_info)  
    
#     print(f"DEBUG: SqrtPriceCurrent ≈ {estimator.sqrt_price_current}")
#     best_position = estimator.find_best_position_to_copy(mock_sample_position_list, strategy='max_liquidity')
    
#     if best_position:
#         print(f"DEBUG: Selected Best Position Range: [{best_position['tick_low']} - {best_position['tick_up']}]")
        
#         # Bước 2: Tính toán dựa trên Best Position
#         result = estimator.estimate_by_multiplier(best_position, user_multiplier)

#         print("--- KẾT QUẢ MODULE 2 (Fixed & Optimized) ---")
#         print(f"Hệ số nhân: x{result['input_multiplier']}")
#         print(f"Reward Share: {result['reward_share_percent']}%")
#         print(f"Token 0 {symbol_0}: {result['required_assets']['token0_amount']:.6f}")
#         print(f"Token 1 {symbol_1}: {result['required_assets']['token1_amount']:.6f}")
#         print(f"Range Active: {result['range_info']['is_active']}")
#     else:
#         print("Không tìm thấy position active nào phù hợp!")

#     # Yêu cầu: 5 SOL + 500 USDC
#     required = {'token0_amount': result['required_assets']['token0_amount'], 'token1_amount': result['required_assets']['token1_amount']}
#     mints = {'token0': token_0, 'token1': token_1, 'decimals0': decimals_0, 'decimals1': decimals_1}
    
#     # Số dư ví: Chỉ có 1 SOL, 0 USDC -> Chắc chắn thiếu
#     # fake_poor_balance = {'token0_balance': 1.0, 'token1_balance': 0.0}

#     try:
#         results = swapper.calculate_and_prepare_swaps(
#             USER_WALLET_ADDRESS, 
#             required, 
#             mints_map=mints
#         )

#         print("\n=== KẾT QUẢ ===")
#         for item in results:
#             if item.get("type") == "ERROR":
#                 print(f"❌ LỖI PHÁT HIỆN: {item['description']}")
#             else:
#                 print(f"✅ SWAP: {item['description']}")
            
#     except Exception as e:
#         print(f"Lỗi Exception: {e}")