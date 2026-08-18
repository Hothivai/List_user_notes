import math
import sys
import os

# Thiết lập đường dẫn hệ thống để import các module local
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
    
from services.evm.semi_auto_mint.scan_pool import V3Scanner
from services.evm.semi_auto_mint.reward_estimator import RewardEstimator

class RangeOptimizer:
    def __init__(self, pool_state, db_info):
        """
        Khởi tạo bộ tối ưu hóa khoảng giá và vốn.
        :param pool_state: Dữ liệu real-time từ V3Scanner
        :param db_info: Dữ liệu metadata từ Database
        """
        self.current_tick = int(pool_state['currentTick'])
        self.tick_spacing = int(pool_state['tickSpacing'])
        self.sqrt_price_x96 = float(pool_state['sqrtPriceX96'])
        self.sqrt_p = self.sqrt_price_x96 / (2**96)
        
        # Mẫu số: Tổng thanh khoản đang Staked và In-range hiện tại của đối thủ
        self.l_active_pool = float(pool_state.get('totalInRangeLiquidity', 0))
        
        self.reward_per_day = float(db_info.get('reward_per_day', 0))
        self.decimals_0 = int(db_info.get('token0_decimals', 18))
        self.decimals_1 = int(db_info.get('token1_decimals', 18))
        self.token0_symbol = db_info.get('token0_symbol', 'T0')
        self.token1_symbol = db_info.get('token1_symbol', 'T1')

    def calculate_liquidity_for_capital(self, usd_amount, price_0, price_1, t_low, t_up):
        """Tính toán L thô (RAW) từ số vốn USD"""
        sqrt_a, sqrt_b = math.sqrt(1.0001 ** t_low), math.sqrt(1.0001 ** t_up)
        if sqrt_a > sqrt_b: sqrt_a, sqrt_b = sqrt_b, sqrt_a

        if self.sqrt_p <= sqrt_a:
            u0, u1 = (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b), 0
        elif self.sqrt_p < sqrt_b:
            u0, u1 = (sqrt_b - self.sqrt_p) / (self.sqrt_p * sqrt_b), (self.sqrt_p - sqrt_a)
        else:
            u0, u1 = 0, (sqrt_b - sqrt_a)
            
        cost_per_l_raw = (u0 / (10**self.decimals_0) * price_0) + (u1 / (10**self.decimals_1) * price_1)
        return usd_amount / cost_per_l_raw if cost_per_l_raw > 0 else 0

    def calculate_capital_for_liquidity(self, liquidity, price_0, price_1, t_low, t_up):
        """Tính toán số vốn USD cần thiết để đạt được một lượng Liquidity (L) nhất định"""
        sqrt_a, sqrt_b = math.sqrt(1.0001 ** t_low), math.sqrt(1.0001 ** t_up)
        if sqrt_a > sqrt_b: sqrt_a, sqrt_b = sqrt_b, sqrt_a
        
        if self.sqrt_p <= sqrt_a:
            u0, u1 = liquidity * (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b), 0
        elif self.sqrt_p < sqrt_b:
            u0, u1 = liquidity * (sqrt_b - self.sqrt_p) / (self.sqrt_p * sqrt_b), liquidity * (self.sqrt_p - sqrt_a)
        else:
            u0, u1 = 0, liquidity * (sqrt_b - sqrt_a)
            
        cost_usd = (u0 / (10**self.decimals_0) * price_0) + (u1 / (10**self.decimals_1) * price_1)
        return cost_usd

    def get_token_breakdown(self, liquidity, t_low, t_up):
        """Quy đổi Liquidity sang số lượng Token thực tế"""
        sqrt_a, sqrt_b = math.sqrt(1.0001 ** t_low), math.sqrt(1.0001 ** t_up)
        if sqrt_a > sqrt_b: sqrt_a, sqrt_b = sqrt_b, sqrt_a
        
        amt0_raw, amt1_raw = 0, 0
        if self.sqrt_p <= sqrt_a:
            amt0_raw = liquidity * (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b)
        elif self.sqrt_p < sqrt_b:
            amt0_raw = liquidity * (sqrt_b - self.sqrt_p) / (self.sqrt_p * sqrt_b)
            amt1_raw = liquidity * (self.sqrt_p - sqrt_a)
        else:
            amt1_raw = liquidity * (sqrt_b - sqrt_a)
            
        return amt0_raw / (10**self.decimals_0), amt1_raw / (10**self.decimals_1)

    def get_custom_strategy(self, tick_low, tick_up, usd_capital, price_0, price_1, cake_price):
        """
        Hàm cho phép người dùng nhập Range và Vốn tùy ý.
        Tự động căn chỉnh tick theo tick_spacing của Pool.
        """
        # Căn chỉnh tick theo spacing
        t_low = round(tick_low / self.tick_spacing) * self.tick_spacing
        t_up = round(tick_up / self.tick_spacing) * self.tick_spacing
        
        if t_low == t_up: t_up += self.tick_spacing # Đảm bảo dải không rỗng

        l_user = self.calculate_liquidity_for_capital(usd_capital, price_0, price_1, t_low, t_up)
        total_liquidity = self.l_active_pool + l_user
        share = (l_user / total_liquidity) * 100 if total_liquidity > 0 else 0
        daily_reward = self.reward_per_day * (share / 100)
        
        return self._wrap_result(t_low, t_up, l_user, share, daily_reward, 0, "custom", usd_capital, cake_price)

    def get_optimized_strategy(self, usd_capital, price_0, price_1, cake_price, mode='balanced'):
        """Tự động tìm kiếm dải giá tối ưu dựa trên điểm bão hòa lợi nhuận (Dùng làm tham khảo)."""
        best_scenario = None
        prev_reward = 0
        sensitivity = 0.05 if mode == 'balanced' else 0.01
        
        for half_width in range(5000, self.tick_spacing, -self.tick_spacing):
            t_low = (self.current_tick - half_width) // self.tick_spacing * self.tick_spacing
            t_up = (self.current_tick + half_width) // self.tick_spacing * self.tick_spacing
            if t_low >= t_up: continue

            l_user = self.calculate_liquidity_for_capital(usd_capital, price_0, price_1, t_low, t_up)
            share = (l_user / (self.l_active_pool + l_user)) * 100
            daily_reward = self.reward_per_day * (share / 100)
            
            reward_gain = (daily_reward - prev_reward) / daily_reward if daily_reward > 0 else 0
            current_scenario = self._wrap_result(t_low, t_up, l_user, share, daily_reward, reward_gain, mode, usd_capital, cake_price)

            if prev_reward > 0 and reward_gain < sensitivity:
                return best_scenario
                
            prev_reward = daily_reward
            best_scenario = current_scenario
        return best_scenario

    def _wrap_result(self, t_low, t_up, l_user, share, daily_reward, gain, mode, capital, cake_price):
        """Đóng gói dữ liệu đầu ra đồng nhất cho cả Auto và Manual"""
        # Tính biên an toàn (%)
        price_impact_low = (1 - (1.0001 ** (t_low - self.current_tick))) * 100
        price_impact_high = ((1.0001 ** (t_up - self.current_tick)) - 1) * 100
        
        # APR dự kiến
        apr = (daily_reward * 365 * cake_price / capital) * 100 if capital > 0 else 0
        
        t0_amt, t1_amt = self.get_token_breakdown(l_user, t_low, t_up)

        return {
            "width_ticks": t_up - t_low,
            "range": [t_low, t_up],
            "share": share,
            "daily_reward": daily_reward,
            "estimated_apr": apr,
            "liquidity_user": l_user,
            "token_amounts": {self.token0_symbol: t0_amt, self.token1_symbol: t1_amt},
            "safety_margin_percent": min(abs(price_impact_low), abs(price_impact_high)),
            "is_active": t_low <= self.current_tick <= t_up,
            "mode": mode
        }

    def suggest_capital_by_target(self, price_0, price_1, target_percent=None, target_reward=None, whale_liquidity=None):
        """Gợi ý số vốn cần thiết dựa trên mục tiêu tài chính (Tham khảo)"""
        t_low = (self.current_tick - 2500) // self.tick_spacing * self.tick_spacing
        t_up = (self.current_tick + 2500) // self.tick_spacing * self.tick_spacing

        results = {}
        if target_percent:
            share_decimal = target_percent / 100
            l_needed = (share_decimal * self.l_active_pool) / (1 - share_decimal)
            results['by_share'] = {"capital": self.calculate_capital_for_liquidity(l_needed, price_0, price_1, t_low, t_up), "share": target_percent}
        if target_reward and self.reward_per_day > 0:
            share_needed = (target_reward / self.reward_per_day) * 100
            if share_needed < 100:
                l_needed = ((share_needed/100) * self.l_active_pool) / (1 - (share_needed/100))
                results['by_reward'] = {"capital": self.calculate_capital_for_liquidity(l_needed, price_0, price_1, t_low, t_up), "reward_goal": target_reward}
        if whale_liquidity:
            results['by_whale'] = {"capital": self.calculate_capital_for_liquidity(float(whale_liquidity), price_0, price_1, t_low, t_up), "whale_l": whale_liquidity}
        return results

    def analyze_efficiency_vs_whale(self, usd_capital, price_0, price_1, whale_pos, mode='balanced'):
        """Phân tích hiệu suất so với Whale cụ thể (Tham khảo)"""
        opt = self.get_optimized_strategy(usd_capital, price_0, price_1, 2.5, mode=mode)
        l_whale = float(whale_pos['liquidity'])
        t_low_w, t_up_w = int(whale_pos['tickLower']), int(whale_pos['tickUpper'])
        whale_cost = self.calculate_capital_for_liquidity(l_whale, price_0, price_1, t_low_w, t_up_w)
        user_efficiency = opt['liquidity_user'] / usd_capital
        whale_efficiency = l_whale / whale_cost if whale_cost > 0 else 0
        return {"optimized": opt, "whale_cost_est": whale_cost, "leverage": user_efficiency / whale_efficiency if whale_efficiency > 0 else 0}

if __name__ == "__main__":
    # --- LUỒNG PHỐI HỢP THỰC TẾ ---
    CHAIN, POOL_ADDR = "BNB", "0x07e17913808ca2983b10181e589d800218d57cc7"
    P0, P1, CAKE_P = 1.0, 0.02255, 1.5
    
    scanner = V3Scanner(CHAIN)
    pool_state = scanner.scan_and_profile(POOL_ADDR)
    if not pool_state: sys.exit()

    estimator = RewardEstimator(pool_state) 
    pool_db_info = estimator.get_pool_state_from_db(CHAIN, POOL_ADDR)
    if not pool_db_info: sys.exit()
    
    optimizer = RangeOptimizer(pool_state, pool_db_info)
    
    # 0. TARGET CAPITAL SUGGESTION 
    print("--- TỰ NHẬP TỐI ƯU ---")
    competitors = pool_state.get('competitors', [])
    if competitors:
        best_target = estimator.find_best_position_to_copy(competitors, strategy='max_liquidity')
        if best_target:
            print(f"Whale liquidity: {best_target['liquidity']}")
            cap_suggestions = optimizer.suggest_capital_by_target(P0, P1, target_percent=5, target_reward=1000, whale_liquidity=best_target['liquidity'])
    print(f"TỰ NHẬP TỐI ƯU: {cap_suggestions}")
    
    # 1. THAM KHẢO TỪ HỆ THỐNG
    print("--- THAM KHẢO CHIẾN LƯỢC TỐI ƯU ---")
    ref_res = optimizer.get_optimized_strategy(100, P0, P1, CAKE_P, mode='aggressive')
    print(f"Reference result: {ref_res}")
    print(f"📍 Gợi ý Balanced: Range {ref_res['range']} | APR: {ref_res['estimated_apr']:.1f}%")

    # 2. NGƯỜI DÙNG TỰ NHẬP (CUSTOM)
    print("\n--- CHIẾN LƯỢC TÙY CHỈNH (USER INPUT) ---")
    USER_LOW, USER_UP, USER_CAP = 35000, 40000, 100
    custom_res = optimizer.get_custom_strategy(USER_LOW, USER_UP, USER_CAP, P0, P1, CAKE_P)
    print(f"Custom result: {custom_res}")
    print(f"📍 Range tùy chỉnh: {custom_res['range']} | Vốn: ${USER_CAP}")
    print(f"🛡️ Biên an toàn: {custom_res['safety_margin_percent']:.2f}%")
    print(f"📊 Share: {custom_res['share']:.2f}% | Reward: {custom_res['daily_reward']:.4f} CAKE/ngày")
    print(f"🧪 CÔNG THỨC MINT: {custom_res['token_amounts']}")
