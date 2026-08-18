# import sys
# import os

# # Đường dẫn đến thư mục flask_app (chứa thư mục services)
# BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../'))

# # Thêm vào sys.path
# if BASE_DIR not in sys.path:
#     sys.path.insert(0, BASE_DIR)

import math
from services.solana.semi_auto_mint.scan_pool import analyze_pool_ticks
from solana.rpc.api import Client
from solders.pubkey import Pubkey

class RewardEstimator:
    def __init__(self, pool_info):
        """
        Khởi tạo với thông tin snapshot của Pool (lấy từ Module 1)
        """
        # Parse an toàn, đảm bảo kiểu dữ liệu
        self.l_pool_global = float(pool_info.get('liquidity', 0))
        self.sqrt_price_x64 = float(pool_info.get('sqrt_price_x64', 0))
        self.tick_current = int(pool_info.get('tick_current', 0))
        self.decimals_0 = int(pool_info.get('mint_decimals_0', 0))
        self.decimals_1 = int(pool_info.get('mint_decimals_1', 0))
        
        # Giá trị SqrtPrice thực tế (đã chia 2^64)
        if self.sqrt_price_x64 > 0:
            self.sqrt_price_current = self.sqrt_price_x64 / (2**64)
        else:
            # Fallback nếu thiếu data, tính từ tick
            self.sqrt_price_current = math.sqrt(1.0001 ** self.tick_current)

    def find_best_position_to_copy(self, position_list, strategy='max_liquidity'):
        """
        Lọc và chọn ra position tốt nhất từ danh sách để copy.
        
        :param position_list: List các dict position lấy từ Module 1.
        :param strategy: Chiến lược chọn ('max_liquidity' | 'narrowest' | 'widest')
        :return: Dict position tốt nhất hoặc None nếu không có cái nào active.
        """
        active_positions = []
        
        # 1. Lọc các Position đang Active
        for pos in position_list:
            tick_lower = pos.get('tick_low') or pos.get('tick_lower_index')
            tick_upper = pos.get('tick_up') or pos.get('tick_upper_index')
            
            if tick_lower is None or tick_upper is None:
                continue
                
            # Check Active: Tick hiện tại phải nằm trong Range
            if tick_lower <= self.tick_current < tick_upper:
                # Chuẩn hóa keys để xử lý thống nhất sau này
                pos_normalized = pos.copy()
                pos_normalized['tick_low'] = tick_lower
                pos_normalized['tick_up'] = tick_upper
                pos_normalized['liquidity'] = float(pos.get('liquidity', 0))
                active_positions.append(pos_normalized)

        if not active_positions:
            print("Warning: No active positions found in the provided list.")
            return None

        # 2. Sắp xếp theo chiến lược
        if strategy == 'max_liquidity':
            # Ưu tiên thanh khoản cao nhất (An toàn, theo đám đông)
            # Sort giảm dần theo liquidity
            active_positions.sort(key=lambda x: x['liquidity'], reverse=True)
            
        elif strategy == 'narrowest':
            # Ưu tiên range hẹp nhất (High Risk, High Return)
            # Sort tăng dần theo độ rộng (tick_upper - tick_lower)
            active_positions.sort(key=lambda x: (x['tick_up'] - x['tick_low']))
            
        elif strategy == 'widest':
            # Ưu tiên range rộng nhất (Full range, An toàn, Low Return)
            active_positions.sort(key=lambda x: (x['tick_up'] - x['tick_low']), reverse=True)

        # Trả về ứng viên số 1
        return active_positions[0]
    
    def check_range_safety(self, tick_lower, tick_upper, safety_threshold_percent=2.0):
        """
        Kiểm tra xem giá hiện tại có quá sát biên không.
        :param safety_threshold_percent: Ngưỡng an toàn (ví dụ 2% khoảng cách tới biên)
        :return: (is_safe, message, warning_type)
        """
        # Chuyển đổi tick sang giá để dễ hình dung % (hoặc tính % trên tick cũng được)
        # Ở đây dùng khoảng cách tick cho đơn giản và chính xác với cơ chế V3
        
        # Khoảng cách từ current tới 2 biên
        dist_to_lower = self.tick_current - tick_lower
        dist_to_upper = tick_upper - self.tick_current
        
        # Tổng độ rộng range
        range_width = tick_upper - tick_lower
        if range_width == 0: return False, "Range invalid (width 0)", "ERROR"

        # Tính % vị trí của giá trong range (0% = sát lower, 100% = sát upper)
        position_percent = (dist_to_lower / range_width) * 100
        print(f"DEBUG: Position percent in range: {position_percent:.2f}%")
        
        # Kiểm tra ngưỡng (Buffer)
        # Ví dụ: Nếu giá nằm trong 5% biên dưới hoặc 5% biên trên -> Nguy hiểm
        buffer_zone = 10 # 5% vùng biên
        
        is_safe = True
        msg = "Safe range"
        warning_type = "NONE"

        if position_percent < buffer_zone:
            is_safe = False
            msg = f"The price is too close to the lower limit. ({position_percent:.1f}%). Easily out-range when prices fall."
            warning_type = "WARNING_LOW"
        elif position_percent > (100 - buffer_zone):
            is_safe = False
            msg = f"The price is too close to the upper limit. ({position_percent:.1f}%). Easily out-range when prices rise."
            warning_type = "WARNING_HIGH"
            
        return {
            "is_safe": is_safe,
            "message": msg,
            "type": warning_type,
            "dist_lower": dist_to_lower,
            "dist_upper": dist_to_upper
        }
    
    def estimate_by_multiplier(self, sample_position, multiplier):
        """Tính toán Reward Share và Vốn cần thiết."""
        target_pos = sample_position
        if isinstance(sample_position, list):
            if not sample_position: raise ValueError("sample_position list is empty")
            target_pos = sample_position[0]

        l_sample = float(target_pos.get('liquidity', 0))
        tick_lower = target_pos.get('tick_low') or target_pos.get('tick_lower_index')
        tick_upper = target_pos.get('tick_up') or target_pos.get('tick_upper_index')
            
        if tick_lower is None or tick_upper is None:
            raise ValueError(f"Could not find ticks")

        # --- LOGIC MỚI: Check Safety ---
        safety_status = self.check_range_safety(tick_lower, tick_upper)
        print(f"Safety status: {safety_status}")

        l_user = l_sample * multiplier
        l_new_total = self.l_pool_global + l_user
        share_percent = (l_user / l_new_total * 100) if l_new_total > 0 else 0

        amount0_wei, amount1_wei = self._get_amounts_for_liquidity(l_user, tick_lower, tick_upper)
        amount0_human = amount0_wei / (10 ** self.decimals_0)
        amount1_human = amount1_wei / (10 ** self.decimals_1)

        return {
            "input_multiplier": multiplier,
            "estimated_liquidity": l_user,
            "reward_share_percent": round(share_percent, 6),
            "required_assets": {
                "token0_amount": amount0_human,
                "token1_amount": amount1_human,
            },
            "range_info": {
                "tick_lower": tick_lower,
                "tick_upper": tick_upper,
                "current_tick": self.tick_current,
                "is_active": tick_lower <= self.tick_current < tick_upper,
                "safety": safety_status # Trả về trạng thái an toàn
            }
        }

    def _get_amounts_for_liquidity(self, liquidity, tick_lower, tick_upper):
        sqrt_price_current = self.sqrt_price_current
        
        # Tính căn bậc 2 giá tại Tick Lower/Upper
        sqrt_price_a = math.sqrt(1.0001 ** tick_lower)
        sqrt_price_b = math.sqrt(1.0001 ** tick_upper)

        # Ensure A < B
        if sqrt_price_a > sqrt_price_b:
            sqrt_price_a, sqrt_price_b = sqrt_price_b, sqrt_price_a

        amount0 = 0.0
        amount1 = 0.0

        # Case 1: Giá hiện tại < Range (Out Left) -> Cần 100% Token X (Token 0)
        if sqrt_price_current <= sqrt_price_a:
            amount0 = liquidity * (sqrt_price_b - sqrt_price_a) / (sqrt_price_a * sqrt_price_b)
            amount1 = 0.0

        # Case 2: Giá hiện tại > Range (Out Right) -> Cần 100% Token Y (Token 1)
        elif sqrt_price_current >= sqrt_price_b:
            amount0 = 0.0
            amount1 = liquidity * (sqrt_price_b - sqrt_price_a) # Logic chuẩn V3 cho Amount1

        # Case 3: In Range (Active) -> Cần cả hai
        else:
            # Token 0 needed: to cover range [Current, Upper]
            amount0 = liquidity * (sqrt_price_b - sqrt_price_current) / (sqrt_price_current * sqrt_price_b)
            
            # Token 1 needed: to cover range [Lower, Current]
            amount1 = liquidity * (sqrt_price_current - sqrt_price_a)

        # Sanity check: Không bao giờ được trả về số âm
        return max(0.0, amount0), max(0.0, amount1)
    
# if __name__ == "__main__":
#     client = Client("https://shy-spring-card.solana-mainnet.quiknode.pro/6a97979ed162924bd71e878f5517215efab54766")
#     program_id = Pubkey.from_string("HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq")
#     pool_id = Pubkey.from_string("4FSrFjSMePHfRZiNaT9WxrRV8wqLcNnevjruG4zJWbpQ")
    
#     pool_ranges = analyze_pool_ticks(client, program_id, pool_id)
    
#     mock_pool_info = pool_ranges.get("pool_info", {})
    
#     mock_sample_position_list = pool_ranges.get("personal_price_ranges", [])
    
#     estimator = RewardEstimator(mock_pool_info)
#     user_multiplier = 2.0

#     print(f"DEBUG: SqrtPriceCurrent ≈ {estimator.sqrt_price_current}")
    
#     best_position = estimator.find_best_position_to_copy(mock_sample_position_list, strategy='max_liquidity')
    
#     if best_position:
#         print(f"DEBUG: Selected Best Position Range: [{best_position['tick_low']} - {best_position['tick_up']}]")
        
#         # Bước 2: Tính toán dựa trên Best Position
#         result = estimator.estimate_by_multiplier(best_position, user_multiplier)

#         print("--- KẾT QUẢ MODULE 2 (Fixed & Optimized) ---")
#         print(f"Hệ số nhân: x{result['input_multiplier']}")
#         print(f"Reward Share: {result['reward_share_percent']}%")
#         print(f"Token 0 (SOL): {result['required_assets']['token0_amount']:.6f}")
#         print(f"Token 1 (USDC): {result['required_assets']['token1_amount']:.6f}")
#         print(f"Range Active: {result['range_info']['is_active']}")
#     else:
#         print("Không tìm thấy position active nào phù hợp!")
    