import sys
import os
import math
import requests

# Thiết lập đường dẫn hệ thống để import các module local
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from services.db_connect import get_connection
from services.evm.semi_auto_mint.scan_pool import V3Scanner
from services.pancake_api import get_price_tokens_coingecko, get_token_price_token_by_cmc

class RewardEstimator:
    def __init__(self, pool_state, db_reward_info=None):
        """
        Khởi tạo với trạng thái Real-time của Pool (lấy từ Module 1)
        :param pool_state: Dict trả về từ V3Scanner.scan_and_profile
        :param db_reward_info: Thông tin reward và metadata lấy từ database
        """
        # Thông số on-chain (Real-time từ Scanner)
        self.tick_current = int(pool_state.get('currentTick', 0))
        self.sqrt_price_x96 = float(pool_state.get('sqrtPriceX96', 0))
        
        # QUAN TRỌNG: Với MasterChef V3, mẫu số để tính share reward là tổng Liquidity In-range của các đối thủ
        self.l_pool_active = float(pool_state.get('totalInRangeLiquidity', 0))
        
        # Thông số Metadata (Sẽ được cập nhật từ DB sau)
        self.db_reward_info = db_reward_info or {}
        self.decimals_0 = int(self.db_reward_info.get('token0_decimals', 18))
        self.decimals_1 = int(self.db_reward_info.get('token1_decimals', 18))
        
        # Giá trị SqrtPrice thực tế (Shift 96 bits cho EVM)
        self.sqrt_price_current = self.sqrt_price_x96 / (2**96)
        
        # Kết nối DB
        self.db_connection = get_connection()

    def get_pool_state_from_db(self, chain, pool_address):
        """Lấy thông tin cấu hình và reward của pool từ database"""
        try:
            query = "SELECT * FROM pool_info WHERE pool_address = %s AND chain = %s"
            
            # Sử dụng dictionary=True để có thể truy cập qua key (result.get('...'))
            try:
                cursor = self.db_connection.cursor(dictionary=True)
            except:
                # Fallback nếu driver không hỗ trợ dictionary cursor trực tiếp
                cursor = self.db_connection.cursor()

            cursor.execute(query, (pool_address.lower(), chain.upper()))
            result = cursor.fetchone()
            
            if not result:
                return None
            
            # Xử lý trường hợp result trả về tuple thay vì dict
            if isinstance(result, tuple):
                print("⚠️ Cảnh báo: Cursor trả về Tuple. Hãy kiểm tra cấu hình DictCursor.")
                return None

            # Lấy giá token từ DB (Đã được cập nhật từ CMC)
            token0_price = get_price_tokens_coingecko(chain, result.get("token0_address"))
            token1_price = get_price_tokens_coingecko(chain, result.get("token1_address"))
            if token0_price == 0:
                token0_price = get_token_price_token_by_cmc(chain, result.get("token0_address"))
                print(f"Token 0 price from cmc: {token0_price}")
            if token1_price == 0:
                token1_price = get_token_price_token_by_cmc(chain, result.get("token1_address"))
                print(f"Token 1 price from cmc: {token1_price}")
                
            return {
                "token0_address": result.get("token0_address"),
                "token1_address": result.get("token1_address"),
                "token0_symbol": result.get("token0_symbol"),
                "token1_symbol": result.get("token1_symbol"),
                "token0_decimals": result.get("token0_decimals", 18),
                "token1_decimals": result.get("token1_decimals", 18),
                "reward_per_day": float(result.get("cake_per_day", 0)),
                "token0_price": float(token0_price) if token0_price else 0,
                "token1_price": float(token1_price) if token1_price else 0,
                "fee": result.get("fee"),
                "pid": result.get("pid"),
                "source": "db"
            }
        except Exception as e:
            print(f"❌ Lỗi truy vấn database: {e}")
            return None

    def find_best_position_to_copy(self, competitors, strategy='max_liquidity'):
        """Lọc đối thủ tối ưu để copy khoảng giá (Dữ liệu từ Scanner)"""
        if not competitors:
            return None

        temp_list = list(competitors)
            
        if strategy == 'max_liquidity':
            temp_list.sort(key=lambda x: int(x.get('liquidity', 0)), reverse=True)
        elif strategy == 'narrowest':
            temp_list.sort(key=lambda x: (int(x.get('tickUpper', 0)) - int(x.get('tickLower', 0))))

        return temp_list[0]

    def check_range_safety(self, tick_lower, tick_upper, buffer_zone=10):
        """Kiểm tra độ an toàn của khoảng giá so với giá hiện tại"""
        range_width = tick_upper - tick_lower
        if range_width <= 0: return {"is_safe": False, "msg": "Khoảng giá không hợp lệ"}

        position_percent = ((self.tick_current - tick_lower) / range_width) * 100
        
        is_safe = True
        msg = "Khoảng giá an toàn"
        warning_type = "NONE"

        if position_percent < buffer_zone:
            is_safe = False
            msg = f"Giá sát biên dưới ({position_percent:.1f}%). Rủi ro out-range nếu giá giảm."
            warning_type = "WARNING_LOW"
        elif position_percent > (100 - buffer_zone):
            is_safe = False
            msg = f"Giá sát biên trên ({position_percent:.1f}%). Rủi ro out-range nếu giá tăng."
            warning_type = "WARNING_HIGH"

        return {
            "is_safe": is_safe,
            "message": msg,
            "type": warning_type,
            "percent_in_range": round(position_percent, 2)
        }

    def estimate_by_multiplier(self, sample_position, multiplier=1.0):
        """Tính toán dự phóng dựa trên vị thế mẫu của đối thủ"""
        l_sample = float(sample_position.get('liquidity', 0))
        t_low = int(sample_position.get('tickLower', 0))
        t_up = int(sample_position.get('tickUpper', 0))

        safety = self.check_range_safety(t_low, t_up)
        l_user = l_sample * multiplier
        
        # Share = L_user / (L_đối_thủ_in_range + L_user)
        total_l = self.l_pool_active + l_user
        share_percent = (l_user / total_l) * 100 if total_l > 0 else 0
        
        reward_per_day = float(self.db_reward_info.get('reward_per_day', 0))
        daily_reward = reward_per_day * (share_percent / 100)
        
        amt0_wei, amt1_wei = self._get_amounts_for_liquidity(l_user, t_low, t_up)
        
        return {
            "multiplier": multiplier,
            "user_liquidity": l_user,
            "total_liquidity": total_l,
            "share_percent": round(share_percent, 4),
            "daily_reward": round(daily_reward, 4),
            "required_assets": {
                "token0": amt0_wei / (10**self.decimals_0),
                "token1": amt1_wei / (10**self.decimals_1)
            },
            "range_info": {
                "current_tick": self.tick_current,
                "is_active": t_low <= self.tick_current <= t_up,
                "safety": safety,
                "tick_lower": t_low,
                "tick_upper": t_up
            }
        }

    def _get_amounts_for_liquidity(self, liquidity, tick_lower, tick_upper):
        """Toán học Uniswap/Pancake V3 quy đổi L sang số lượng Token"""
        sqrt_p = self.sqrt_price_current
        sqrt_a = math.sqrt(1.0001 ** tick_lower)
        sqrt_b = math.sqrt(1.0001 ** tick_upper)

        if sqrt_a > sqrt_b: sqrt_a, sqrt_b = sqrt_b, sqrt_a

        amount0 = 0
        amount1 = 0

        if sqrt_p <= sqrt_a:
            amount0 = liquidity * (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b)
        elif sqrt_p < sqrt_b:
            amount0 = liquidity * (sqrt_b - sqrt_p) / (sqrt_p * sqrt_b)
            amount1 = liquidity * (sqrt_p - sqrt_a)
        else:
            amount1 = liquidity * (sqrt_b - sqrt_a)

        return max(0, amount0), max(0, amount1)

if __name__ == "__main__":
    chain = "BNB" 
    pool_address = "0x07e17913808ca2983b10181e589d800218d57cc7"
    
    # 1. Lấy dữ liệu Real-time từ Scanner (Module 1 mới)
    scanner = V3Scanner(chain)
    pool_state = scanner.scan_and_profile(pool_address)
    
    if not pool_state:
        print("❌ Không thể quét dữ liệu pool!")
        sys.exit()

    # 2. Khởi tạo Estimator và cập nhật Metadata từ DB
    # Truyền pool_state (từ scan_pool.py) vào constructor
    estimator = RewardEstimator(pool_state) 
    pool_db_metadata = estimator.get_pool_state_from_db(chain, pool_address)
    
    if not pool_db_metadata:
        print(f"❌ Không tìm thấy pool {pool_address} trong database!")
        sys.exit()

    # Cập nhật thông tin DB vào estimator để tính toán reward và decimals
    estimator.db_reward_info = pool_db_metadata
    
    estimator.decimals_0 = pool_db_metadata.get('token0_decimals', 18)
    estimator.decimals_1 = pool_db_metadata.get('token1_decimals', 18)
    
    symbol0 = pool_db_metadata.get('token0_symbol', "Unknow")
    symbol1 = pool_db_metadata.get('token1_symbol', "Unknow")

    # 3. Lấy đối thủ (Lấy từ pool_state do Scanner trả về)
    competitors = pool_state.get('competitors', [])
    
    if not competitors:
        print(f"⚠️ Không tìm thấy đối thủ In-range nào để phân tích.")
    else:
        # Tìm Whale để so sánh
        best_target = estimator.find_best_position_to_copy(competitors, strategy='max_liquidity')
        print(f"🎯 Đối thủ Whale: #{best_target}")
        
        if best_target:
            # Giả lập nạp vốn bằng 50% đối thủ Whale
            report = estimator.estimate_by_multiplier(best_target, multiplier=2.0)
            
            print(f"\n✅ ĐỒNG BỘ MODULE THÀNH CÔNG")
            print(f"🎯 NFT Đối thủ: #{best_target['id']}")
            print(f"📊 Range: [{report['range_info']['tick_lower']} -> {report['range_info']['tick_upper']}]")
            print(f"📊 Liquidity: {report['user_liquidity']}")
            print(f"📊 Tổng thanh khoản: {report['total_liquidity']}")
            print(f"📊 Share dự kiến: {report['share_percent']}%")
            print(f"💰 Thu nhập: {report['daily_reward']} CAKE/ngày (Dựa trên DB reward)")
            print(f"🛡️ Trạng thái an toàn: {report['range_info']['safety']['message']}")
            print(f"🧪 Tài sản cần: {report['required_assets']['token0']:.4f} {symbol0} & {report['required_assets']['token1']:.4f} {symbol1}")