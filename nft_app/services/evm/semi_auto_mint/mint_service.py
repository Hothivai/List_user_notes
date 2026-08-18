import math
import time
from web3 import Web3
from services.db_connect import get_connection
from services.evm.semi_auto_mint.scan_pool import V3Scanner
from services.evm.semi_auto_mint.reward_estimator import RewardEstimator
# Giả sử file range_optimizer.py nằm cùng thư mục
from services.evm.semi_auto_mint.range_optimizer import RangeOptimizer
from services.evm.semi_auto_mint.swapper import V3Swapper
# Giả sử file v3_executor.py nằm cùng thư mục
from services.evm.semi_auto_mint.executor import V3Executor
from services.pancake_api import get_cake_price_usd, get_price_tokens_coingecko, get_token_price_from_dexscreener, get_token_price_token_by_cmc
from config import AERODROME_NPM_FACTORY_ADDRESSES, NPM_ADDRESSES

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
]

MASTERCHEF_ADDRESSES = {
    "BNB": "0x556B9306565093C855AEA9AE92A594704c2Cd59e",
    "BAS": "0xC6A2Db661D5a5690172d8eB0a7DEA2d3008665A3",
    "ETH": "0x556B9306565093C855AEA9AE92A594704c2Cd59e",
    "ARB": "0x5e09ACf80C0296740eC5d6F643005a4ef8DaA694",
    "LIN": "0x22E2f236065B780FA33EC8C4E58b99ebc8B55c57",
    "POL": "0xe9c7f3196ab8c09f6616365e8873daeb207c0391"
}

PANCAKE_V3 = "pancake_v3"
AERODROME_V3 = "aerodrome_v3"

AERODROME_POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24", "name": "tick", "type": "int24"},
            {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
            {"internalType": "bool", "name": "unlocked", "type": "bool"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {"inputs": [], "name": "liquidity", "outputs": [{"internalType": "uint128", "name": "", "type": "uint128"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "tickSpacing", "outputs": [{"internalType": "int24", "name": "", "type": "int24"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"}
]

def _normalize_dex_type(dex_type):
    value = (dex_type or "").strip().lower()
    if value in ("aerodrome", "aerodrome_v3", "aerodrome_gauge"):
        return AERODROME_V3
    if value in ("", "pancake", "pancakeswap", "pancake_v3", "pancake_v3_masterchef"):
        return PANCAKE_V3
    return value

def _fetch_token_price(chain, token_address):
    price = get_price_tokens_coingecko(chain, token_address)
    if not price:
        price = get_token_price_token_by_cmc(chain, token_address)
    if not price:
        price = get_token_price_from_dexscreener(token_address)
    return float(price) if price else 0.0

class PancakeV3Adapter:
    dex_type = PANCAKE_V3

    def __init__(self, aggregator):
        self.aggregator = aggregator

    def get_context(self, pool_address, force_refresh=False):
        return self.aggregator._get_pancake_execution_context(pool_address, force_refresh=force_refresh)

    def get_ui_metadata(self, pool_address, force_refresh=False):
        return self.aggregator._get_pancake_ui_metadata(pool_address, force_refresh=force_refresh)

class AerodromeV3Adapter:
    dex_type = AERODROME_V3

    def __init__(self, aggregator):
        self.aggregator = aggregator

    def get_context(self, pool_address, force_refresh=False):
        return self.aggregator._get_aerodrome_execution_context(pool_address, force_refresh=force_refresh)

    def get_ui_metadata(self, pool_address, force_refresh=False):
        context = self.get_context(pool_address, force_refresh=force_refresh)
        if not context:
            return {"error": "POOL_CONTEXT_UNAVAILABLE", "msg": "Unable to load Aerodrome pool context."}

        pool_state = context["pool_state"]
        db_info = context["db_info"]
        current_price, market_price, price_deviation_pct, market_price_status = self.aggregator._build_price_view(pool_state, db_info)
        spacing = int(pool_state.get("tickSpacing") or db_info.get("tick_spacing") or 1)
        current_tick = int(pool_state["currentTick"])
        default_manual_low = (current_tick // spacing - 10) * spacing
        default_manual_up = (current_tick // spacing + 10) * spacing
        protocol_meta = self.aggregator._base_protocol_metadata(db_info)

        return {
            "pool_meta": {
                **protocol_meta,
                "dex_type": AERODROME_V3,
                "pair": f"{db_info['token1_symbol']} / {db_info['token0_symbol']}",
                "token0": {"symbol": db_info["token0_symbol"], "address": db_info["token0_address"]},
                "token1": {"symbol": db_info["token1_symbol"], "address": db_info["token1_address"]},
                "token0_price": round(db_info.get("token0_price", 0), 6),
                "token1_price": round(db_info.get("token1_price", 0), 6),
                "market_price": round(market_price, 6) if market_price is not None else None,
                "price_deviation_pct": round(price_deviation_pct, 4) if price_deviation_pct is not None else None,
                "market_price_status": market_price_status,
                "fee_tier": db_info.get("fee", 0),
                "current_price": round(current_price, 6),
                "current_tick": current_tick,
                "tick_spacing": spacing,
                "total_active_l": str(pool_state.get("totalInRangeLiquidity", 0)),
                "competitors": [],
                "manual_required": True,
                "default_manual_range": [default_manual_low, default_manual_up],
                "npm_address": db_info.get("npm_address"),
                "staking_address": db_info.get("staking_address")
            },
            "strategies": {
                "manual": {
                    "apr": 0,
                    "safety": 0,
                    "range": [default_manual_low, default_manual_up],
                    "description": "Manual range required for Aerodrome V3 pools."
                }
            }
        }

class V3ApiAggregator:
    """
    Module Aggregator: Trung tâm điều phối dữ liệu cho UI Semi-Auto Mint.
    Nhiệm vụ: Tổng hợp thông tin từ Scanner, Optimizer, Swapper và Executor thành 
    bộ dữ liệu chuẩn hóa để Frontend hiển thị và thực thi.
    """
    _context_cache = {}
    _db_info_cache = {}
    _balance_cache = {}
    _cake_price_cache = {"value": None, "timestamp": 0}
    CONTEXT_TTL_SECONDS = 45
    DB_INFO_TTL_SECONDS = 600
    BALANCE_TTL_SECONDS = 10
    CAKE_PRICE_TTL_SECONDS = 60

    def __init__(self, chain_name, rpc_url, dex_type=None):
        self.chain_name = chain_name.upper()
        self.rpc_url = rpc_url
        self.dex_type = _normalize_dex_type(dex_type) if dex_type else None
        self.scanner = V3Scanner(self.chain_name)
        print(f"Chain name: {self.chain_name}")
        
        # Mapping giá token chính để tính toán APR (Thực tế nên lấy từ Oracle hoặc Price Feed)
        # Ở đây dùng tạm cấu hình giá tham chiếu
        self.market_prices = {
            "CAKE": 1.5,
            "BNB": 360.0,
            "ETH": 2450.0,
            "USDT": 1.0,
            "USDC": 1.0
        }

        self.cake_price = self._get_cached_cake_price()
        self.masterchef_address = MASTERCHEF_ADDRESSES.get(self.chain_name, "")

    def _adapter_for_pool(self, pool_address, dex_type=None):
        explicit_dex_type = dex_type if str(dex_type or "").strip() else self.dex_type
        if explicit_dex_type:
            requested = _normalize_dex_type(explicit_dex_type)
            print(f"[DEX ADAPTER] chain={self.chain_name} pool={pool_address} explicit_dex_type={requested}")
        else:
            requested = self._resolve_dex_type(pool_address)
            print(f"[DEX ADAPTER] chain={self.chain_name} pool={pool_address} resolved_dex_type={requested}")
        if requested == AERODROME_V3:
            return AerodromeV3Adapter(self)
        return PancakeV3Adapter(self)

    def _resolve_dex_type(self, pool_address):
        conn = None
        cursor = None
        try:
            conn = get_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1
                        FROM aerodrome_pool_info
                        WHERE chain = %s AND LOWER(pool_address) = LOWER(%s)
                        LIMIT 1
                    ) AS is_aerodrome,
                    EXISTS(
                        SELECT 1
                        FROM pool_info
                        WHERE chain = %s AND LOWER(pool_address) = LOWER(%s)
                        LIMIT 1
                    ) AS is_pancake
                """,
                (self.chain_name, pool_address, self.chain_name, pool_address),
            )
            row = cursor.fetchone() or {}
            is_aerodrome = bool(row.get("is_aerodrome"))
            is_pancake = bool(row.get("is_pancake"))
            print(
                f"[DEX RESOLVE] chain={self.chain_name} pool={pool_address} "
                f"is_aerodrome={is_aerodrome} is_pancake={is_pancake}"
            )
            if is_aerodrome:
                return AERODROME_V3
            if is_pancake:
                return PANCAKE_V3
        except Exception as exc:
            print(f"⚠️ Dex type resolve failed for {pool_address}: {exc}")
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
        return PANCAKE_V3

    def _base_protocol_metadata(self, db_info):
        dex_type = db_info.get("dex_type", PANCAKE_V3)
        if dex_type == AERODROME_V3:
            return {
                "dex_type": AERODROME_V3,
                "npm_address": db_info.get("npm_address", ""),
                "staking_address": db_info.get("staking_address", ""),
                "gauge_address": db_info.get("gauge_address", ""),
                "stake_method": "aerodrome_gauge_deposit",
                "mint_param_schema": "aerodrome_tick_spacing"
            }
        return {
            "dex_type": PANCAKE_V3,
            "npm_address": str(NPM_ADDRESSES.get(self.chain_name, "")),
            "staking_address": self.masterchef_address,
            "masterchef_address": self.masterchef_address,
            "stake_method": "pancake_masterchef_transfer",
            "mint_param_schema": "pancake_fee"
        }

    def _resolve_aerodrome_npm(self, factory_address):
        if not factory_address:
            return ""
        factory_lc = str(factory_address).lower()
        for npm_address, configured_factory in AERODROME_NPM_FACTORY_ADDRESSES.get(self.chain_name, {}).items():
            if str(configured_factory).lower() == factory_lc:
                return str(npm_address)
        return ""

    def _get_aerodrome_db_info(self, pool_address):
        conn = None
        cursor = None
        try:
            conn = get_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT
                    p.pool_address,
                    p.chain,
                    p.factory_address,
                    p.token0_address,
                    p.token1_address,
                    p.token0_symbol,
                    p.token1_symbol,
                    p.token0_decimals,
                    p.token1_decimals,
                    p.fee,
                    p.tick_spacing,
                    e.gauge_address,
                    e.reward_per_day,
                    e.reward_decimals
                FROM aerodrome_pool_info p
                LEFT JOIN aerodrome_pool_epoch_state e
                    ON p.chain = e.chain AND LOWER(p.pool_address) = LOWER(e.pool_address)
                WHERE p.chain = %s AND LOWER(p.pool_address) = LOWER(%s)
                ORDER BY e.update_at DESC
                LIMIT 1
                """,
                (self.chain_name, pool_address),
            )
            row = cursor.fetchone()
            if not row:
                return None
            npm_address = self._resolve_aerodrome_npm(row.get("factory_address"))
            return {
                "token0_address": row.get("token0_address"),
                "token1_address": row.get("token1_address"),
                "token0_symbol": row.get("token0_symbol"),
                "token1_symbol": row.get("token1_symbol"),
                "token0_decimals": int(row.get("token0_decimals") or 18),
                "token1_decimals": int(row.get("token1_decimals") or 18),
                "reward_per_day": 0.0,
                "token0_price": _fetch_token_price(self.chain_name, row.get("token0_address")),
                "token1_price": _fetch_token_price(self.chain_name, row.get("token1_address")),
                "fee": int(row.get("fee") or 0),
                "tick_spacing": int(row.get("tick_spacing") or 0),
                "pid": None,
                "source": "aerodrome_db",
                "dex_type": AERODROME_V3,
                "factory_address": row.get("factory_address"),
                "npm_address": npm_address,
                "staking_address": row.get("gauge_address"),
                "gauge_address": row.get("gauge_address"),
                "stake_method": "aerodrome_gauge_deposit",
                "mint_param_schema": "aerodrome_tick_spacing"
            }
        except Exception as exc:
            print(f"❌ Aerodrome DB metadata failed: {exc}")
            return None
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def _w3(self):
        if not hasattr(self, "_rpc_w3"):
            self._rpc_w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        return self._rpc_w3

    def _get_aerodrome_pool_state(self, pool_address):
        try:
            checksum_address = Web3.to_checksum_address(pool_address)
            pool_contract = self._w3().eth.contract(address=checksum_address, abi=AERODROME_POOL_ABI)
            slot0 = pool_contract.functions.slot0().call()
            active_liquidity = pool_contract.functions.liquidity().call()
            tick_spacing = pool_contract.functions.tickSpacing().call()
            token0_address = pool_contract.functions.token0().call()
            token1_address = pool_contract.functions.token1().call()
            return {
                "pool": pool_address,
                "token0": token0_address,
                "token1": token1_address,
                "sqrtPriceX96": slot0[0],
                "currentTick": slot0[1],
                "tickSpacing": tick_spacing,
                "rpcActiveLiquidity": active_liquidity,
                "totalInRangeLiquidity": 0,
                "inRangeCount": 0,
                "competitors": []
            }
        except Exception as exc:
            print(f"❌ Aerodrome pool scan failed: {exc}")
            return None

    @classmethod
    def _get_cached_cake_price(cls):
        now = time.time()
        cached = cls._cake_price_cache
        if cached["value"] is not None and now - cached["timestamp"] < cls.CAKE_PRICE_TTL_SECONDS:
            return cached["value"]
        price = get_cake_price_usd()
        cls._cake_price_cache = {"value": price, "timestamp": now}
        return price

    @staticmethod
    def _cache_get(cache, key, ttl_seconds):
        item = cache.get(key)
        if not item:
            return None
        if time.time() - item["timestamp"] >= ttl_seconds:
            cache.pop(key, None)
            return None
        return item["value"]

    @staticmethod
    def _cache_set(cache, key, value):
        cache[key] = {"value": value, "timestamp": time.time()}
        return value

    def _get_cached_db_info(self, pool_state, pool_address, force_refresh=False):
        cache_key = (self.chain_name, PANCAKE_V3, pool_address.lower())
        if not force_refresh:
            cached = self._cache_get(self.__class__._db_info_cache, cache_key, self.DB_INFO_TTL_SECONDS)
            if cached:
                print(f"⚡ [DB INFO CACHE HIT] {self.chain_name} {pool_address}")
                return cached

        estimator = RewardEstimator(pool_state)
        db_info = estimator.get_pool_state_from_db(self.chain_name, pool_address)
        if not db_info:
            return None
        return self._cache_set(self.__class__._db_info_cache, cache_key, db_info)

    def _get_pancake_execution_context(self, pool_address, force_refresh=False):
        cache_key = (self.chain_name, PANCAKE_V3, pool_address.lower())
        if not force_refresh:
            cached = self._cache_get(self.__class__._context_cache, cache_key, self.CONTEXT_TTL_SECONDS)
            if cached:
                print(f"⚡ [PLAN CONTEXT CACHE HIT] {self.chain_name} {pool_address}")
                return cached

        pool_state = self.scanner.scan_and_profile(pool_address, force_refresh=force_refresh)
        if not pool_state:
            return None
        db_info = self._get_cached_db_info(pool_state, pool_address, force_refresh=force_refresh)
        if not db_info:
            return None
        db_info.update(self._base_protocol_metadata(db_info))
        context = {"pool_state": pool_state, "db_info": db_info, "cake_price": self.cake_price, "dex_type": PANCAKE_V3}
        return self._cache_set(self.__class__._context_cache, cache_key, context)

    def _get_aerodrome_execution_context(self, pool_address, force_refresh=False):
        cache_key = (self.chain_name, AERODROME_V3, pool_address.lower())
        if not force_refresh:
            cached = self._cache_get(self.__class__._context_cache, cache_key, self.CONTEXT_TTL_SECONDS)
            if cached:
                print(f"⚡ [AERODROME PLAN CONTEXT CACHE HIT] {self.chain_name} {pool_address}")
                return cached

        pool_state = self._get_aerodrome_pool_state(pool_address)
        if not pool_state:
            return None
        db_info = self._get_aerodrome_db_info(pool_address)
        if not db_info:
            return None

        db_tick_spacing = int(db_info.get("tick_spacing") or 0)
        if db_tick_spacing > 0:
            pool_state["tickSpacing"] = db_tick_spacing
        context = {"pool_state": pool_state, "db_info": db_info, "cake_price": self.cake_price, "dex_type": AERODROME_V3}
        return self._cache_set(self.__class__._context_cache, cache_key, context)

    def _get_wallet_token_balance(self, swapper, user_checksum, token_addr, wrapped_native):
        cache_key = (self.chain_name, user_checksum.lower(), token_addr.lower())
        cached = self._cache_get(self.__class__._balance_cache, cache_key, self.BALANCE_TTL_SECONDS)
        if cached is not None:
            return cached

        addr = Web3.to_checksum_address(token_addr)
        contract = swapper.w3.eth.contract(address=addr, abi=ERC20_ABI)
        try:
            bal = contract.functions.balanceOf(user_checksum).call()
        except Exception:
            bal = 0
        if token_addr.lower() in wrapped_native:
            try:
                bal += swapper.w3.eth.get_balance(user_checksum)
            except Exception:
                pass
        return self._cache_set(self.__class__._balance_cache, cache_key, bal)

    def _build_price_view(self, pool_state, db_info):
        price_raw = (pool_state.get('sqrtPriceX96', 0) / (2**96)) ** 2
        scale = 10**db_info['token0_decimals'] / 10**db_info['token1_decimals']
        current_price = price_raw * scale
        p0_price = db_info.get('token0_price', 0)
        p1_price = db_info.get('token1_price', 0)
        market_price = None
        price_deviation_pct = None
        market_price_status = "missing"
        try:
            p0_price_float = float(p0_price or 0)
            p1_price_float = float(p1_price or 0)
            current_price_float = float(current_price or 0)
            if p0_price_float > 0 and p1_price_float > 0 and current_price_float > 0:
                market_price = p0_price_float / p1_price_float
                if market_price > 0:
                    price_deviation_pct = ((current_price_float - market_price) / market_price) * 100
                    market_price_status = "available"
        except (TypeError, ValueError, ZeroDivisionError):
            market_price = None
            price_deviation_pct = None
            market_price_status = "missing"
        return current_price, market_price, price_deviation_pct, market_price_status

    def get_ui_metadata(self, pool_address, force_refresh=False, dex_type=None):
        adapter = self._adapter_for_pool(pool_address, dex_type=dex_type)
        return adapter.get_ui_metadata(pool_address, force_refresh=force_refresh)

    def _get_pancake_ui_metadata(self, pool_address, force_refresh=False):
        """
        Giai đoạn 1: Lấy dữ liệu khởi tạo khi người dùng vừa vào trang.
        Cung cấp thông tin Pool và phân tích 'xem trước' cho các thẻ chiến lược.
        """
        # 1. Quét trạng thái Pool thực tế từ Smart Contract
        pool_state = self.scanner.scan_and_profile(pool_address, force_refresh=force_refresh)
        print(f"Pool state: {pool_state}")
        if not pool_state:
            return {"error": "POOL_SCAN_FAILED", "msg": "Can not scan pool data from blockchain."}

        # 2. Lấy Metadata từ DB (PID, Reward rate, Token Decimals)
        db_info = self._get_cached_db_info(pool_state, pool_address, force_refresh=force_refresh)
        if not db_info:
            return {"error": "DB_METADATA_MISSING", "msg": "Can not get pool metadata from database."}
        db_info.update(self._base_protocol_metadata(db_info))

        # 3. Tính toán giá Token 1 dựa trên Token 0 (thường là Stablecoin)
        price_raw = (pool_state.get('sqrtPriceX96', 0) / (2**96)) ** 2
        scale = 10**db_info['token0_decimals'] / 10**db_info['token1_decimals']
        current_price = price_raw * scale
        print(f"Current Price: {current_price}")

        # 4. Phân tích các chiến lược (vốn mẫu $100) để hiển thị lên Strategy Cards
        optimizer = RangeOptimizer(pool_state, db_info)
        cake_price = self.cake_price
        sample_cap = 100.0

        # THÊM MỚI: Tính toán giá trị USD cho từng Competitor
        competitors = pool_state.get('competitors', [])[:10]
        p0_price = db_info.get('token0_price', 0)
        p1_price = db_info.get('token1_price', 0)
        market_price = None
        price_deviation_pct = None
        market_price_status = "missing"
        try:
            p0_price_float = float(p0_price or 0)
            p1_price_float = float(p1_price or 0)
            current_price_float = float(current_price or 0)
            if p0_price_float > 0 and p1_price_float > 0 and current_price_float > 0:
                market_price = p0_price_float / p1_price_float
                if market_price > 0:
                    price_deviation_pct = ((current_price_float - market_price) / market_price) * 100
                    market_price_status = "available"
        except (TypeError, ValueError, ZeroDivisionError):
            market_price = None
            price_deviation_pct = None
            market_price_status = "missing"

        for comp in competitors:
            try:
                l_val = float(comp.get('liquidity', 0))
                t_low = int(comp.get('tickLower', 0))
                t_up = int(comp.get('tickUpper', 0))
                
                # Sử dụng công thức toán học V3 để dịch từ L thô sang USD
                usd_val = optimizer.calculate_capital_for_liquidity(l_val, p0_price, p1_price, t_low, t_up)
                comp['liquidity_usd'] = round(usd_val, 2)
            except Exception as e:
                comp['liquidity_usd'] = 0.0

        balanced_analysis = optimizer.get_optimized_strategy(sample_cap, 1.0, 0.02216, cake_price,mode='balanced')
        aggressive_analysis = optimizer.get_optimized_strategy(sample_cap, 1.0, 0.02216, cake_price, mode='aggressive')
        
        # Tạo gợi ý dải giá mặc định cho chế độ Manual (ví dụ: +/- 10 tick spacing)
        spacing = pool_state['tickSpacing']
        current_tick = pool_state['currentTick']
        default_manual_low = (current_tick // spacing - 10) * spacing
        default_manual_up = (current_tick // spacing + 10) * spacing
        manual_preview = optimizer.get_custom_strategy(default_manual_low, default_manual_up, sample_cap, 1.0, 0.02216, cake_price)

        # Đóng gói dữ liệu cho UI
        return {
            "pool_meta": {
                "dex_type": PANCAKE_V3,
                "pair": f"{db_info['token1_symbol']} / {db_info['token0_symbol']}",
                "token0": {"symbol": db_info['token0_symbol'], "address": db_info['token0_address']},
                "token1": {"symbol": db_info['token1_symbol'], "address": db_info['token1_address']},
                "token0_price": round(p0_price, 6),
                "token1_price": round(p1_price, 6),
                "market_price": round(market_price, 6) if market_price is not None else None,
                "price_deviation_pct": round(price_deviation_pct, 4) if price_deviation_pct is not None else None,
                "market_price_status": market_price_status,
                "fee_tier": db_info.get('fee', 0),
                "current_price": round(current_price, 6),
                "current_tick": pool_state['currentTick'],
                "tick_spacing": spacing,
                "total_active_l": str(pool_state['totalInRangeLiquidity']),
                "competitors": competitors,
                "npm_address": db_info.get("npm_address"),
                "staking_address": db_info.get("staking_address"),
                "masterchef_address": db_info.get("masterchef_address"),
                "stake_method": db_info.get("stake_method"),
                "mint_param_schema": db_info.get("mint_param_schema")
            },
            "strategies": {
                "balanced": {
                    "apr": round((balanced_analysis['daily_reward'] * 365 * cake_price / sample_cap) * 100, 2),
                    "safety": round(balanced_analysis['safety_margin_percent'], 2),
                    "range": balanced_analysis['range'],
                    "description": "Balanced between risk and reward. Wide range helps maintain farm position longer when market volatility."
                },
                "aggressive": {
                    "apr": round((aggressive_analysis['daily_reward'] * 365 * cake_price / sample_cap) * 100, 2),
                    "safety": round(aggressive_analysis['safety_margin_percent'], 2),
                    "range": aggressive_analysis['range'],
                    "description": "Focus capital on narrow range around current price to capture maximum reward share. High out-range risk."
                },
                "manual": {
                    "apr": round((manual_preview['daily_reward'] * 365 * cake_price / sample_cap) * 100, 2),
                    "safety": round(manual_preview['safety_margin_percent'], 2),
                    "range": manual_preview['range'],
                    "description": "Free to set your own price range. Suitable for strategies to catch bottom or expect profit."
                }
            }
        }

    def get_execution_pipeline(self, pool_address, user_address, capital_usd, mode='balanced', custom_range=None, slippage_bps=10):
        """
        Giai đoạn 2: Tính toán chi tiết khi người dùng bấm 'Start'.
        Trả về phân tích vốn thực tế và danh sách các giao dịch (Pipeline) để Client ký.
        """
        # 1. Thu thập lại dữ liệu mới nhất (chống trượt giá)
        pool_state = self.scanner.scan_and_profile(pool_address, force_refresh=True)
        estimator = RewardEstimator(pool_state)
        db_info = estimator.get_pool_state_from_db(self.chain_name, pool_address)
        optimizer = RangeOptimizer(pool_state, db_info)
        swapper = V3Swapper(self.chain_name, self.rpc_url)
        executor = V3Executor(self.chain_name, user_address)

        # 2. Tính toán Price 1 hiện tại
        price_raw = (pool_state.get('sqrtPriceX96', 0) / (2**96)) ** 2
        price_1 = price_raw * (10**db_info['token0_decimals'] / 10**db_info['token1_decimals'])
        cake_price = self.market_prices.get("CAKE", 1.5)

        # 3. Lấy Plan chi tiết dựa trên Mode (Hỗ trợ Custom Range)
        if mode == 'manual' and custom_range:
            # Lấy tick_low và tick_up từ mảng custom_range [low, up]
            plan = optimizer.get_custom_strategy(
                custom_range[0], 
                custom_range[1], 
                capital_usd, 
                1.0, 
                0.02216, 
                cake_price
            )
        else:
            plan = optimizer.get_optimized_strategy(capital_usd, 1.0, 0.02216, cake_price, mode=mode)

        # 4. Tính toán Zap Swap (Số lượng Token 0 cần đổi sang Token 1)
        price_0_in_1 = price_1 / 1.0
        swap_amount_human = swapper.calculate_optimal_swap_v3(
            capital_usd, price_0_in_1, pool_state['currentTick'], plan['range'][0], plan['range'][1]
        )

        pipeline = {
            "strategy_analysis": plan,
            "steps": []
        }

        # BƯỚC 1: WRAP (Nếu nạp bằng Native Token)
        wrap_tx = executor.prepare_wrap_tx(db_info['token0_address'], int(capital_usd * 10**db_info['token0_decimals']))
        if wrap_tx and "error" not in wrap_tx:
            pipeline["steps"].append({
                "id": "step_wrap",
                "action": "WRAP",
                "description": f"Wrap Native to {db_info['token0_symbol']}",
                "tx": wrap_tx
            })

        # BƯỚC 2: SWAP (Nếu cần cân bằng tỷ lệ)
        if swap_amount_human > (capital_usd * 0.005): # Giảm ngưỡng swap xuống 0.5% để chính xác hơn
            amt_wei = int(swap_amount_human * (10 ** db_info['token0_decimals']))
            route = swapper.get_kyber_route(db_info['token0_address'], db_info['token1_address'], amt_wei)
            if route:
                swap_tx = swapper.build_kyber_swap_data(route, user_address, slippage_bps=slippage_bps)
                pipeline["steps"].append({
                    "id": "step_swap",
                    "action": "SWAP",
                    "description": f"Đổi {swap_amount_human:.4f} {db_info['token0_symbol']} sang {db_info['token1_symbol']}",
                    "tx": swap_tx
                })

        # BƯỚC 3: APPROVE
        for token_key in [db_info['token0_symbol'], db_info['token1_symbol']]:
            is_t0 = (token_key == db_info['token0_symbol'])
            addr = db_info['token0_address'] if is_t0 else db_info['token1_address']
            token_price_raw = db_info['token0_price'] * (10**db_info['token0_decimals']) if is_t0 else db_info['token1_price'] * (10**db_info['token1_decimals'])
            app_tx = executor.prepare_approve_tx(addr, int(token_price_raw))
            if app_tx and "error" not in app_tx:
                pipeline["steps"].append({
                    "id": f"step_approve_{token_key}",
                    "action": "APPROVE",
                    "description": f"Approve {token_key} to spend",
                    "tx": app_tx
                })
        
        print(f"Pipeline: {pipeline}")

        # BƯỚC 4: MINT & STAKE
        plan.update({
            "token0_address": db_info['token0_address'],
            "token1_address": db_info['token1_address'],
            "token0_decimals": db_info['token0_decimals'],
            "token1_decimals": db_info['token1_decimals'],
            "token0_symbol": db_info['token0_symbol'],
            "token1_symbol": db_info['token1_symbol'],
            "fee_tier": db_info['fee']
        })
        mint_tx = executor.prepare_mint_tx(plan)
        if "error" not in mint_tx:
            pipeline["steps"].append({
                "id": "step_mint",
                "action": "MINT",
                "description": f"Mint NFT position V3 ({plan['range'][0]} - {plan['range'][1]}) & Auto-Stake (PID: {db_info['pid']})",
                "tx": mint_tx,
                "pid": db_info['pid']
            })

        return pipeline

    def calculate_impact(self,route_data):
        summary = route_data.get('routeSummary', {})
        in_usd = float(summary.get('amountInUsd', 0))
        out_usd = float(summary.get('amountOutUsd', 0))
        
        if in_usd <= 0:
            return 0.0
        
        # Tính phần trăm hao hụt
        impact = ((in_usd - out_usd) / in_usd) * 100
        
        # Nếu impact âm (do arbitrage hoặc sai lệch oracle), trả về 0
        return max(0.0, impact)

    def get_execution_plan(self, pool_address, user_address, capital_usd, mode='manual', custom_range=None, slippage_bps=10, force_refresh=False, quote_mode='full', dex_type=None, **kwargs):
        """Tính toán các thông số nạp tiền để Client tự build TX (Logic Auto-balance giống Solana)"""
        
        quote_mode = (quote_mode or "full").lower()
        if quote_mode not in ("full", "preview", "quote_preview"):
            quote_mode = "full"

        adapter = self._adapter_for_pool(pool_address, dex_type=dex_type)
        context = adapter.get_context(pool_address, force_refresh=force_refresh)
        if not context:
            return {"error": {"code": "POOL_CONTEXT_UNAVAILABLE", "description": "Unable to load pool context."}}
        pool_state = context["pool_state"]
        db_info = context["db_info"]
        resolved_dex_type = context.get("dex_type") or db_info.get("dex_type") or PANCAKE_V3
        
        optimizer = RangeOptimizer(pool_state, db_info)
        swapper = V3Swapper(self.chain_name, self.rpc_url)

        # 1. Định giá Token
        price_raw = (pool_state.get('sqrtPriceX96', 0) / (2**96)) ** 2
        ratio_t1_t0 = price_raw * (10**db_info['token0_decimals'] / 10**db_info['token1_decimals'])
        
        p0_usd = db_info.get("token0_price", 0)
        p1_usd = db_info.get("token1_price", 0)

        # 2. Logic suy luận giá (Inference)
        if p0_usd > 0 and p1_usd == 0:
            p1_usd = p0_usd / ratio_t1_t0 if ratio_t1_t0 > 0 else 0
        elif p1_usd > 0 and p0_usd == 0:
            p0_usd = p1_usd * ratio_t1_t0

        cake_price_usd = 0 if resolved_dex_type == AERODROME_V3 else self.cake_price
        print("p0_usd", p0_usd)
        print("p1_usd", p1_usd)
        print("cake_price_usd", cake_price_usd)

        # 2. Tạo Plan
        if mode == 'manual' and custom_range:
            plan = optimizer.get_custom_strategy(int(custom_range[0]), int(custom_range[1]), float(capital_usd), p0_usd, p1_usd, cake_price_usd)
        else:
            plan = optimizer.get_optimized_strategy(float(capital_usd), p0_usd, p1_usd, cake_price_usd, mode=mode)

        amt0_needed = plan['token_amounts'][db_info['token0_symbol']]
        amt1_needed = plan['token_amounts'][db_info['token1_symbol']]
        
        req0_raw = int(amt0_needed * (10**db_info['token0_decimals']))
        req1_raw = int(amt1_needed * (10**db_info['token1_decimals']))

        protocol_metadata = self._base_protocol_metadata(db_info)
        response = {
            "strategy_analysis": {
                "range": plan['range'],
                "amount0_raw": str(req0_raw),
                "amount1_raw": str(req1_raw),
                "estimated_apr": 0 if resolved_dex_type == AERODROME_V3 else plan['estimated_apr'],
                "share": 0 if resolved_dex_type == AERODROME_V3 else plan['share'],
                "safety_margin": plan['safety_margin_percent'],
                "liquidity_user": plan['liquidity_user']
            },
            "metadata": {
                **protocol_metadata,
                "token0_address": db_info['token0_address'],
                "token1_address": db_info['token1_address'],
                "token0_symbol": db_info['token0_symbol'],
                "token1_symbol": db_info['token1_symbol'],
                "token0_decimals": db_info['token0_decimals'],
                "token1_decimals": db_info['token1_decimals'],
                "fee_tier": db_info['fee'],
                "tick_spacing": db_info.get("tick_spacing") or pool_state.get("tickSpacing"),
                "pid": db_info.get('pid'),
                "token0_price": p0_usd,
                "token1_price": p1_usd,
                "masterchef_address": protocol_metadata.get(
                    "masterchef_address",
                    self.masterchef_address if resolved_dex_type == PANCAKE_V3 else ""
                )
            },
            "swap_step": None,
            "swap_intent": None,
            "swap_quote_preview": None,
            "quote_warning": None,
            "quote_mode": quote_mode
        }

        # ---------------------------------------------------------
        # 3. LOGIC AUTO-BALANCE TƯƠNG TỰ SOLANA
        # ---------------------------------------------------------
        user_checksum = Web3.to_checksum_address(user_address)
        
        # Hàm fetch balance linh hoạt (Gộp Native và ERC20 nếu là Wrapped token)
        WRAPPED_NATIVE = [
            "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", 
            "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "0x4200000000000000000000000000000000000006", 
            "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"
        ]
        def get_available_balance(token_addr):
            return self._get_wallet_token_balance(swapper, user_checksum, token_addr, WRAPPED_NATIVE)

        # Fetch on-chain balances
        bal0_raw = get_available_balance(db_info['token0_address'])
        bal1_raw = get_available_balance(db_info['token1_address'])

        missing0_raw = max(0, req0_raw - bal0_raw)
        missing1_raw = max(0, req1_raw - bal1_raw)

        print("missing0_raw", missing0_raw)
        print("missing1_raw", missing1_raw)
        print("req0_raw", req0_raw)
        print("req1_raw", req1_raw)
        print("bal0_raw", bal0_raw)
        print("bal1_raw", bal1_raw)

        # Trạng thái 1: Thiếu cả 2 Token (Số vốn nạp slider lớn hơn tài sản thực có trong ví)
        if missing0_raw > 0 and missing1_raw > 0:
            m0_human = missing0_raw / (10**db_info['token0_decimals'])
            m1_human = missing1_raw / (10**db_info['token1_decimals'])
            response["error"] = {
                "code": "INSUFFICIENT_FUNDS_BOTH",
                "description": f"Insufficient funds for both tokens. Need to deposit at least {m0_human:.4f} {db_info['token0_symbol']} and {m1_human:.4f} {db_info['token1_symbol']}."
            }
            return response

        # Trạng thái 2: Dư Token 0, Thiếu Token 1
        if missing1_raw > 0:
            missing1_usd = (missing1_raw / (10**db_info['token1_decimals'])) * p1_usd
            
            # Chỉ tiến hành Swap nếu lượng thiếu lớn hơn 0.5% vốn (chống bụi gas/dust)
            if missing1_usd > (float(capital_usd) * 0.005):
                excess0_raw = bal0_raw - req0_raw
                
                # Trừ buffer gas nếu Token0 là WBNB/WETH
                if db_info['token0_address'].lower() in WRAPPED_NATIVE:
                    excess0_raw -= int(0.005 * 10**db_info['token0_decimals'])
                
                if excess0_raw <= 0:
                    response["error"] = {"code": "INSUFFICIENT_FUNDS_TOKEN0", "description": f"Not enough {db_info['token0_symbol']} (after gas buffer) to auto-swap for missing amount."}
                    return response
                
                # Tính lượng Token 0 cần bán (Kèm 2% Buffer chống Slippage)
                amt_in_usd = missing1_usd * 1.02
                amt_in_raw = int((amt_in_usd / p0_usd) * 10**db_info['token0_decimals'])
                print(f"[DEBUG] amt_in_raw: {amt_in_raw}, excess0_raw: {excess0_raw}")
                
                if amt_in_raw > excess0_raw:
                    response["error"] = {"code": "INSUFFICIENT_SWAP_BALANCE", "description": f"Not enough {db_info['token0_symbol']} to swap. Need {(amt_in_raw / 10**db_info['token0_decimals']):.4f} but only {(excess0_raw / 10**db_info['token0_decimals']):.4f} available."}
                    return response

                if quote_mode == "preview":
                    response["swap_intent"] = {
                        "action": "SWAP_AUTO_BALANCE",
                        "description": f"Route pending: Bán {(amt_in_raw / 10**db_info['token0_decimals']):.4f} {db_info['token0_symbol']} ➡️ Mua {(missing1_raw / 10**db_info['token1_decimals']):.4f} {db_info['token1_symbol']}",
                        "token_in_address": db_info['token0_address'],
                        "token_out_address": db_info['token1_address'],
                        "sell_amount_raw": str(amt_in_raw),
                        "missing_amount_raw": str(missing1_raw),
                        "route_display": "Route pending",
                        "price_impact": None
                    }
                    return response
                
                if quote_mode == "quote_preview":
                    response["swap_intent"] = {
                        "action": "SWAP_AUTO_BALANCE",
                        "description": f"Route pending: BÃ¡n {(amt_in_raw / 10**db_info['token0_decimals']):.4f} {db_info['token0_symbol']} âž¡ï¸ Mua {(missing1_raw / 10**db_info['token1_decimals']):.4f} {db_info['token1_symbol']}",
                        "token_in_address": db_info['token0_address'],
                        "token_out_address": db_info['token1_address'],
                        "sell_amount_raw": str(amt_in_raw),
                        "missing_amount_raw": str(missing1_raw),
                        "route_display": "Route pending",
                        "price_impact": None
                    }

                # swap_tx_data = swapper.get_0x_swap_quote(db_info['token0_address'], db_info['token1_address'], amt_in_raw, user_address, int(slippage_bps))
                # if swap_tx_data:
                #     response["swap_step"] = {
                #         "action": "SWAP_0_TO_1",
                #         "description": f"Auto-Balance: Bán {(amt_in_raw / 10**db_info['token0_decimals']):.4f} {db_info['token0_symbol']} ➡️ Mua {(missing1_raw / 10**db_info['token1_decimals']):.4f} {db_info['token1_symbol']}",
                #         "token_in_address": db_info['token0_address'],
                #         "sell_amount_raw": str(amt_in_raw),
                #         "tx": {"to": Web3.to_checksum_address(swap_tx_data['to']), "data": swap_tx_data['data'], "value": Web3.to_hex(int(swap_tx_data['value']))}
                #     }

                # Sử dụng bộ so sánh Route tốt nhất
                best_route = swapper.get_best_swap_route(
                    db_info['token0_address'],
                    db_info['token1_address'],
                    amt_in_raw,
                    user_address,
                    slippage_bps=int(slippage_bps),
                    quote_context="preview" if quote_mode == "quote_preview" else "execute"
                )

                if best_route:
                    quoted_at_ms = int(time.time() * 1000)
                    if quote_mode == "quote_preview":
                        response["swap_quote_preview"] = {
                            "provider": best_route["provider"],
                            "route_display": best_route.get("route_display"),
                            "price_impact": best_route.get("price_impact", 0),
                            "buy_amount_raw": str(best_route.get("buyAmount", "0")),
                            "sell_amount_raw": str(amt_in_raw),
                            "token_in_address": db_info["token0_address"],
                            "token_out_address": db_info["token1_address"],
                            "quoted_at_ms": quoted_at_ms
                        }
                        return response
                    response["swap_step"] = {
                        "action": "SWAP_AUTO_BALANCE",
                        "description": f"{best_route['provider']} Swap: Bán {(amt_in_raw / 10**db_info['token0_decimals']):.4f} {db_info['token0_symbol']} ➡️ Mua {(missing1_raw / 10**db_info['token1_decimals']):.4f} {db_info['token1_symbol']}",
                        "token_in_address": db_info['token0_address'],
                        "token_out_address": db_info['token1_address'],
                        "sell_amount_raw": str(amt_in_raw),
                        "buy_amount_raw": str(best_route.get("buyAmount", "0")),
                        "quoted_at_ms": quoted_at_ms,
                        "allowanceTarget": best_route.get("allowanceTarget"),
                        "route_display": best_route.get("route_display"),
                        "price_impact": best_route.get("price_impact", 0),
                        "provider": best_route['provider'],
                        "tx": {
                            "to": Web3.to_checksum_address(best_route['to']),
                            "data": best_route['data'],
                            "value": Web3.to_hex(int(best_route.get('value', 0)))
                        }
                    }

        # Trạng thái 3: Dư Token 1, Thiếu Token 0
                elif quote_mode == "quote_preview":
                    response["quote_warning"] = {"code": "ROUTE_UNAVAILABLE", "description": "Unable to fetch indicative route."}
                    return response

        elif missing0_raw > 0:
            missing0_usd = (missing0_raw / (10**db_info['token0_decimals'])) * p0_usd
            
            if missing0_usd > (float(capital_usd) * 0.005):
                excess1_raw = bal1_raw - req1_raw
                
                if db_info['token1_address'].lower() in WRAPPED_NATIVE:
                    excess1_raw -= int(0.005 * 10**db_info['token1_decimals'])
                
                if excess1_raw <= 0:
                    response["error"] = {"code": "INSUFFICIENT_FUNDS_TOKEN1", "description": f"Not enough {db_info['token1_symbol']} to auto-swap for missing amount."}
                    return response
                
                amt_in_usd = missing0_usd * 1.02
                amt_in_raw = int((amt_in_usd / p1_usd) * 10**db_info['token1_decimals'])
                print(f"[DEBUG] amt_in_raw: {amt_in_raw}, excess1_raw: {excess1_raw}")
                
                if amt_in_raw > excess1_raw:
                    response["error"] = {"code": "INSUFFICIENT_SWAP_BALANCE", "description": f"Not enough {db_info['token1_symbol']} to swap. Need {(amt_in_raw / 10**db_info['token1_decimals']):.4f} but only {(excess1_raw / 10**db_info['token1_decimals']):.4f} available."}
                    return response

                if quote_mode == "preview":
                    response["swap_intent"] = {
                        "action": "SWAP_AUTO_BALANCE",
                        "description": f"Route pending: Bán {(amt_in_raw / 10**db_info['token1_decimals']):.4f} {db_info['token1_symbol']} ➡️ Mua {(missing0_raw / 10**db_info['token0_decimals']):.4f} {db_info['token0_symbol']}",
                        "token_in_address": db_info['token1_address'],
                        "token_out_address": db_info['token0_address'],
                        "sell_amount_raw": str(amt_in_raw),
                        "missing_amount_raw": str(missing0_raw),
                        "route_display": "Route pending",
                        "price_impact": None
                    }
                    return response
                
                if quote_mode == "quote_preview":
                    response["swap_intent"] = {
                        "action": "SWAP_AUTO_BALANCE",
                        "description": f"Route pending: BÃ¡n {(amt_in_raw / 10**db_info['token1_decimals']):.4f} {db_info['token1_symbol']} âž¡ï¸ Mua {(missing0_raw / 10**db_info['token0_decimals']):.4f} {db_info['token0_symbol']}",
                        "token_in_address": db_info['token1_address'],
                        "token_out_address": db_info['token0_address'],
                        "sell_amount_raw": str(amt_in_raw),
                        "missing_amount_raw": str(missing0_raw),
                        "route_display": "Route pending",
                        "price_impact": None
                    }

                # swap_tx_data = swapper.get_0x_swap_quote(db_info['token1_address'], db_info['token0_address'], amt_in_raw, user_address, int(slippage_bps))
                # if swap_tx_data:
                #     response["swap_step"] = {
                #         "action": "SWAP_1_TO_0",
                #         "description": f"Auto-Balance: Bán {(amt_in_raw / 10**db_info['token1_decimals']):.4f} {db_info['token1_symbol']} ➡️ Mua {(missing0_raw / 10**db_info['token0_decimals']):.4f} {db_info['token0_symbol']}",
                #         "token_in_address": db_info['token1_address'],
                #         "sell_amount_raw": str(amt_in_raw),
                #         "tx": {"to": Web3.to_checksum_address(swap_tx_data['to']), "data": swap_tx_data['data'], "value": Web3.to_hex(int(swap_tx_data['value']))}
                #     }

                # Sử dụng bộ so sánh Route tốt nhất
                best_route = swapper.get_best_swap_route(
                    db_info['token1_address'],
                    db_info['token0_address'],
                    amt_in_raw,
                    user_address,
                    slippage_bps=int(slippage_bps),
                    quote_context="preview" if quote_mode == "quote_preview" else "execute"
                )

                if best_route:
                    quoted_at_ms = int(time.time() * 1000)
                    if quote_mode == "quote_preview":
                        response["swap_quote_preview"] = {
                            "provider": best_route["provider"],
                            "route_display": best_route.get("route_display"),
                            "price_impact": best_route.get("price_impact", 0),
                            "buy_amount_raw": str(best_route.get("buyAmount", "0")),
                            "sell_amount_raw": str(amt_in_raw),
                            "token_in_address": db_info["token1_address"],
                            "token_out_address": db_info["token0_address"],
                            "quoted_at_ms": quoted_at_ms
                        }
                        return response
                    response["swap_step"] = {
                        "action": "SWAP_AUTO_BALANCE",
                        "description": f"{best_route['provider']} Swap: Bán {(amt_in_raw / 10**db_info['token1_decimals']):.4f} {db_info['token1_symbol']} ➡️ Mua {(missing0_raw / 10**db_info['token0_decimals']):.4f} {db_info['token0_symbol']}",
                        "token_in_address": db_info['token1_address'],
                        "token_out_address": db_info['token0_address'],
                        "sell_amount_raw": str(amt_in_raw),
                        "buy_amount_raw": str(best_route.get("buyAmount", "0")),
                        "quoted_at_ms": quoted_at_ms,
                        "allowanceTarget": best_route.get("allowanceTarget"),
                        "route_display": best_route.get("route_display"),
                        "price_impact": best_route.get("price_impact", 0),
                        "provider": best_route['provider'],
                        "tx": {
                            "to": Web3.to_checksum_address(best_route['to']),
                            "data": best_route['data'],
                            "value": Web3.to_hex(int(best_route.get('value', 0)))
                        }
                    }

                elif quote_mode == "quote_preview":
                    response["quote_warning"] = {"code": "ROUTE_UNAVAILABLE", "description": "Unable to fetch indicative route."}
                    return response

        return response
