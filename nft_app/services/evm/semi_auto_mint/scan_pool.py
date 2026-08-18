# Module 1: Scan Pool Position cho PancakeSwap V3
# Cập nhật theo Subgraph ID mới nhất từ PancakeSwap Developer Docs (7/1/25)
# Sử dụng MasterChef V3 Subgraph với cấu trúc query chuẩn xác từ người dùng

import os
import requests
from web3 import Web3
import json
import time
from w3multicall.multicall import W3Multicall
from itertools import islice

class V3Scanner:
    API_KEY_INFURA = "92ce3193f38d4592a33bba00e65fd936"
    API_KEY = "5dcbb5f56c64aa954328b8997984d0b2" 

    _scan_cache = {}
    _positions_cache = {}     # {chain: {token_id: {liquidity, tick_lower, tick_upper, pid}}}
    _last_synced_block = {}   # {chain: block_number}
    _cache_initialized = {}   # {chain: bool}

    TOPIC_DEPOSIT = "0x" + Web3.keccak(text="Deposit(address,uint256,uint256,uint256,int24,int24)").hex()
    TOPIC_WITHDRAW = "0x" + Web3.keccak(text="Withdraw(address,address,uint256,uint256)").hex()

    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    SHARED_CACHE_DIR = os.path.join(BASE_DIR, "latest_farms", "positions_cache")

    CHAIN_CONFIGS = {
        "BNB": {
            "rpc": f"https://bsc-mainnet.infura.io/v3/{API_KEY_INFURA}",
            "subgraph": f"https://gateway.thegraph.com/api/{API_KEY}/subgraphs/id/QProcZexB8KYHueG55aoLhBmwnLXExxopq7CUnFkjMv"
        },
        "BAS": {
            "rpc": f"https://base-mainnet.infura.io/v3/{API_KEY_INFURA}",
            "subgraph": f"https://gateway.thegraph.com/api/{API_KEY}/subgraphs/id/6eA56Eyh2fgoLv1uV5B7ov62mSQZPSk375xvTTYKUxRF"
        },
        "ARB": {
            "rpc": f"https://arbitrum-mainnet.infura.io/v3/{API_KEY_INFURA}",
            "subgraph": f"https://gateway.thegraph.com/api/{API_KEY}/subgraphs/id/2fq9U1dYX1bxuu6D3HuZcfyZSBxPHd8yWduJnVoxNjSP"
        },
        "ETH": {
            "rpc": f"https://mainnet.infura.io/v3/{API_KEY_INFURA}",
            "subgraph": None
        },
        "LIN": {
            "rpc": f"https://linea-mainnet.infura.io/v3/{API_KEY_INFURA}",
            "subgraph": None
        }
    }

    MASTERCHEF_ADDRESSES = {
        "BNB": "0x556B9306565093C855AEA9AE92A594704c2Cd59e",
        "BAS": "0xC6A2Db661D5a5690172d8eB0a7DEA2d3008665A3",
        "ARB": "0x5e09ACf80C0296740eC5d6F643005a4ef8DaA694",
        "ETH": "0x556B9306565093C855AEA9AE92A594704c2Cd59e",
        "LIN": "0x22E2f236065B780FA33EC8C4E58b99ebc8B55c57"
    }

    NPM_ADDRESSES = {
        "BNB": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
        "BAS": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
        "ARB": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
        "ETH": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
        "LIN": "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"
    }
    
    def __init__(self, chain_name):
        chain_name = chain_name.upper()
        if chain_name not in self.CHAIN_CONFIGS:
            raise ValueError(f"Chain {chain_name} chưa được hỗ trợ. Hãy chọn: {', '.join(self.CHAIN_CONFIGS.keys())}")
        
        config = self.CHAIN_CONFIGS[chain_name]
        self.chain_name = chain_name
        self.w3 = Web3(Web3.HTTPProvider(config["rpc"]))
        
        # Chỉ áp dụng POA Middleware cho BNB (vì extraData block của BNB lên tới 280 bytes)
        # Các Layer 2 như BASE, ARB sẽ bị lỗi nếu gắn nhầm Middleware này
        if chain_name == "BNB":
            try:
                from web3.middleware import ExtraDataToPOAMiddleware
                self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            except ImportError:
                # Trong trường hợp Web3.py bản cũ (v5)
                from web3.middleware import geth_poa_middleware
                self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            
        self.subgraph_url = config.get("subgraph")
        self.masterchef_addresses = self.MASTERCHEF_ADDRESSES
        self.npm_addresses = self.NPM_ADDRESSES
        
        # ABI chuẩn cho PancakeSwap V3 (feeProtocol: uint32)
        self.POOL_ABI = [
            {
                "inputs": [],
                "name": "slot0",
                "outputs": [
                    {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
                    {"internalType": "int24", "name": "tick", "type": "int24"},
                    {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
                    {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
                    {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
                    {"internalType": "uint32", "name": "feeProtocol", "type": "uint32"},
                    {"internalType": "bool", "name": "unlocked", "type": "bool"}
                ],
                "stateMutability": "view",
                "type": "function"
            },
            {"inputs":[],"name":"liquidity","outputs":[{"internalType":"uint128","name":"","type":"uint128"}],"stateMutability":"view","type":"function"},
            {"inputs":[],"name":"tickSpacing","outputs":[{"internalType":"int24","name":"","type":"int24"}],"stateMutability":"view","type":"function"},
            {"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
            {"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}
        ]

    # ─────────────────────────────────────────────────────────────
    # Delta Sweep Methods (for chains without subgraph, e.g. ETH)
    # ─────────────────────────────────────────────────────────────

    def _load_shared_positions_cache(self):
        """Load shared positions cache maintained by update_staked_tvl.py cronjob."""
        chain = self.chain_name
        if self.__class__._cache_initialized.get(chain):
            return

        cache_file = os.path.join(self.SHARED_CACHE_DIR, f"positions_cache_{chain}.json")
        try:
            if os.path.exists(cache_file):
                with open(cache_file, "r") as f:
                    data = json.load(f)
                positions = {int(k): v for k, v in data.get("positions", {}).items()}
                self.__class__._positions_cache[chain] = positions
                self.__class__._last_synced_block[chain] = data.get("last_synced_block", 0)
                self.__class__._positions_cache[f"_bootstrapped_{chain}"] = data.get("bootstrapped_pids", [])
                print(f"✅ Shared cache loaded: {len(positions)} positions, last block {self.__class__._last_synced_block[chain]}")
            else:
                print(f"⚠️ No shared cache found: {cache_file}")
                self.__class__._positions_cache[chain] = {}
                self.__class__._last_synced_block[chain] = 0
                self.__class__._positions_cache[f"_bootstrapped_{chain}"] = []
        except Exception as e:
            print(f"⚠️ Failed to load shared cache for {chain}: {e}")
            self.__class__._positions_cache[chain] = {}
            self.__class__._last_synced_block[chain] = 0
            self.__class__._positions_cache[f"_bootstrapped_{chain}"] = []

        self.__class__._cache_initialized[chain] = True

    def _get_pid_for_pool(self, pool_address):
        """Query MasterChef to find the pid that corresponds to pool_address."""
        chain = self.chain_name
        masterchef_addr = self.MASTERCHEF_ADDRESSES.get(chain)
        if not masterchef_addr:
            return None

        checksum_mc = Web3.to_checksum_address(masterchef_addr)
        target = pool_address.lower()

        _POOL_LENGTH_ABI = [{
            "inputs": [], "name": "poolLength",
            "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
            "stateMutability": "view", "type": "function"
        }]

        def _scan_pids(pids):
            """Batch-multicall poolInfo for a list of pids and return matching pid."""
            try:
                for batch in self.batch_iterable(pids, 50):
                    mc = W3Multicall(self.w3)
                    for pid in batch:
                        mc.add(W3Multicall.Call(
                            checksum_mc,
                            "poolInfo(uint256)(uint256,address,address,address,uint24,uint256,uint256)",
                            pid
                        ))
                    results = mc.call()
                    for i, result in enumerate(results):
                        if result and result[1].lower() == target:
                            return batch[i]
            except Exception as e:
                print(f"⚠️ _get_pid_for_pool multicall error: {e}")
            return None

        # First: try bootstrapped pids (fast path)
        bootstrapped_pids = self.__class__._positions_cache.get(f"_bootstrapped_{chain}", [])
        if bootstrapped_pids:
            pid = _scan_pids(bootstrapped_pids)
            if pid is not None:
                return pid

        # Fallback: call poolLength() directly, then scan all pids
        print(f"⚠️ [{chain}] PID not in bootstrapped list. Scanning all MasterChef pools...")
        try:
            mc_contract = self.w3.eth.contract(address=checksum_mc, abi=_POOL_LENGTH_ABI)
            pool_length = int(mc_contract.functions.poolLength().call())
            print(f"[{chain}] MasterChef poolLength = {pool_length}")
        except Exception as e:
            print(f"⚠️ Cannot get poolLength from MasterChef: {e}")
            return None

        pid = _scan_pids(list(range(pool_length)))
        if pid is None:
            print(f"⚠️ [{chain}] Pool {pool_address} is not registered in MasterChef (scanned {pool_length} pids).")
        return pid

    def _delta_sweep_masterchef(self, from_block, to_block):
        """
        Sweep MasterChef Deposit/Withdraw events in block range.
        Returns (new_stake_ids, unstake_ids) sets of token_ids.
        Returns (None, None) on failure.
        """
        chain = self.chain_name
        masterchef_addr = self.MASTERCHEF_ADDRESSES.get(chain)
        if not masterchef_addr:
            return set(), set()

        checksum_mc = Web3.to_checksum_address(masterchef_addr)
        new_stake_ids = set()
        unstake_ids = set()
        CHUNK_SIZE = 1000

        if to_block - from_block > 100000:
            print(f"⚠️ [{chain}] Delta range too large ({to_block - from_block} blocks). Skipping.")
            return None, None

        current = from_block
        while current <= to_block:
            end = min(current + CHUNK_SIZE - 1, to_block)
            success = False
            for attempt in range(3):
                try:
                    logs = self.w3.eth.get_logs({
                        "fromBlock": current,
                        "toBlock": end,
                        "address": checksum_mc,
                        "topics": [[self.TOPIC_DEPOSIT, self.TOPIC_WITHDRAW]]
                    })
                    for log_ev in logs:
                        topic0 = Web3.to_hex(log_ev["topics"][0]).lower()
                        # Deposit: topics[3] = tokenId (indexed); Withdraw: topics[3] = tokenId (indexed)
                        token_id = int.from_bytes(log_ev["topics"][3], "big")
                        if topic0 == self.TOPIC_DEPOSIT.lower():
                            new_stake_ids.add(token_id)
                        elif topic0 == self.TOPIC_WITHDRAW.lower():
                            unstake_ids.add(token_id)
                    success = True
                    break
                except Exception as e:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                    else:
                        print(f"⚠️ Delta sweep chunk {current}-{end} failed after 3 attempts: {e}")
                        return None, None
            if not success:
                return None, None
            current = end + 1

        print(f"✅ [{chain}] Delta sweep: +{len(new_stake_ids)} deposits, -{len(unstake_ids)} withdraws")
        return new_stake_ids, unstake_ids

    def _refresh_positions_multicall(self, token_ids):
        """Fetch/update position data from MasterChef via multicall and write to positions_cache."""
        chain = self.chain_name
        masterchef_addr = self.MASTERCHEF_ADDRESSES.get(chain)
        if not masterchef_addr or not token_ids:
            return

        checksum_mc = Web3.to_checksum_address(masterchef_addr)
        positions = self.__class__._positions_cache.get(chain, {})

        for batch in self.batch_iterable(sorted(token_ids), 100):
            mc = W3Multicall(self.w3)
            for token_id in batch:
                mc.add(W3Multicall.Call(
                    checksum_mc,
                    "userPositionInfos(uint256)(uint128,uint128,int24,int24,uint256,uint256,address,uint256,uint256)",
                    token_id
                ))
            try:
                results = mc.call()
                for i, data in enumerate(results):
                    if not data:
                        continue
                    token_id = batch[i]
                    liquidity, _, tick_lower, tick_upper, _, _, user, pos_pid, _ = data
                    positions[token_id] = {
                        "liquidity": liquidity,
                        "tick_lower": tick_lower,
                        "tick_upper": tick_upper,
                        "user": user,
                        "pid": int(pos_pid)
                    }
            except Exception as e:
                print(f"⚠️ _refresh_positions_multicall batch error: {e}")

        self.__class__._positions_cache[chain] = positions

    def _fetch_positions_via_delta_sweep(self, pool_address, is_masterchef=True):
        """
        Main orchestrator for chains without subgraph (e.g. ETH).
        Loads shared cache → delta sweep for new events → filters by pid → returns position list.
        """
        chain = self.chain_name

        # 1. Load the shared positions cache (one-time per process)
        self._load_shared_positions_cache()

        # 2. Delta sweep from last synced block to current block
        try:
            current_block = self.w3.eth.get_block("latest")["number"]
        except Exception as e:
            print(f"❌ Cannot get latest block: {e}")
            return []

        last_block = self.__class__._last_synced_block.get(chain, 0)
        if last_block > 0 and last_block < current_block:
            result = self._delta_sweep_masterchef(last_block + 1, current_block)
            if result != (None, None):
                new_stake_ids, unstake_ids = result
                # Remove unstaked positions
                positions = self.__class__._positions_cache.get(chain, {})
                for tid in unstake_ids:
                    positions.pop(tid, None)
                self.__class__._positions_cache[chain] = positions
                # Refresh newly staked positions via multicall
                if new_stake_ids:
                    self._refresh_positions_multicall(new_stake_ids)
                self.__class__._last_synced_block[chain] = current_block

        # 3. Find which pid corresponds to this pool_address
        pid = self._get_pid_for_pool(pool_address)
        if pid is None:
            print(f"⚠️ Không tìm được PID cho pool {pool_address} trên {chain}")
            return []

        # 4. Filter positions by pid, include only active (liquidity > 0)
        positions = self.__class__._positions_cache.get(chain, {})
        pid_tokens = {
            tid: info for tid, info in positions.items()
            if info.get("pid") == pid and info.get("liquidity", 0) > 0
        }

        # Fetch user address for positions loaded from shared cache (missing "user" field)
        missing_user = {tid for tid, info in pid_tokens.items() if "user" not in info}
        if missing_user:
            print(f"🔄 Fetching user address for {len(missing_user)} positions missing owner...")
            self._refresh_positions_multicall(missing_user)
            # Re-read updated entries from cache
            updated = self.__class__._positions_cache.get(chain, {})
            pid_tokens = {tid: updated.get(tid, info) for tid, info in pid_tokens.items()}

        result = []
        for token_id, info in pid_tokens.items():
            raw_user = info.get("user", "")
            user_id = raw_user.lower() if isinstance(raw_user, str) and raw_user else "unknown"
            result.append({
                "id": str(token_id),
                "liquidity": str(info.get("liquidity", 0)),
                "tickLower": str(info.get("tick_lower", 0)),
                "tickUpper": str(info.get("tick_upper", 0)),
                "user": {"id": user_id}
            })

        print(f"✅ Delta sweep found {len(result)} active positions for pool {pool_address} (pid={pid})")
        return result

    def get_realtime_pool_state(self, pool_address):
        """Lấy giá và thanh khoản thực tế từ Smart Contract (Ground Truth)"""
        try:
            checksum_address = Web3.to_checksum_address(pool_address)
            pool_contract = self.w3.eth.contract(address=checksum_address, abi=self.POOL_ABI)
            
            slot0 = pool_contract.functions.slot0().call()
            active_liquidity = pool_contract.functions.liquidity().call()
            tick_spacing = pool_contract.functions.tickSpacing().call()
            token0_address = pool_contract.functions.token0().call()
            token1_address = pool_contract.functions.token1().call()
            
            return {
                "token0": token0_address,
                "token1": token1_address,
                "currentTick": slot0[1],
                "sqrtPriceX96": slot0[0],
                "activeLiquidity": active_liquidity,
                "tickSpacing": tick_spacing
            }
        except Exception as e:
            print(f"❌ Lỗi RPC: {e}")
            return None
    
    def fetch_all_positions(self, pool_address, is_masterchef=True):
        """
        Truy vấn danh sách Position từ Decentralized Subgraph.
        Sử dụng cấu trúc lọc pool_: {v3Pool: "..."} như mẫu thực tế.
        """
        
        if is_masterchef:
            # Dispatch to delta sweep if this chain has no subgraph
            if self.subgraph_url is None:
                return self._fetch_positions_via_delta_sweep(pool_address, is_masterchef)

            # Query MasterChef V3 dựa trên mẫu mô phỏng đúng của người dùng
            query = """
            {
              userPositions(
                first: 1000,
                orderBy: liquidity, 
                orderDirection: desc, 
                where: { pool_: { v3Pool: "%s" }, liquidity_gt: "0" }
              ) {
                id
                liquidity
                tickLower
                tickUpper
                user { id }
              }
            }
            """ % pool_address.lower()
        else:
            # Query Exchange V3 (Dùng pool_ để đồng bộ cấu trúc nếu cần)
            query = """
            {
              positions(
                where: { pool: "%s", liquidity_gt: "0" }, 
                orderBy: liquidity, 
                orderDirection: desc, 
                first: 1000
              ) {
                id
                owner
                liquidity
                tickLower
                tickUpper
              }
            }
            """ % pool_address.lower()
        
        # Cấu hình Retry
        max_retries = 5
        retry_delay = 2  # Bắt đầu với 2 giây

        for attempt in range(max_retries):
            try:
                response = requests.post(self.subgraph_url, json={'query': query}, timeout=15)
                response.raise_for_status()
                data = response.json()
                
                if 'errors' in data:
                    print(f"⚠️ Lỗi Schema Subgraph: {data['errors']}")
                    return []
                
                field_name = 'userPositions' if is_masterchef else 'positions'
                positions = data.get('data', {}).get(field_name, [])
                return positions

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                wait_time = retry_delay * (2 ** attempt) # Exponential backoff: 2s, 4s, 8s, 16s...
                print(f"⚠️ Lỗi kết nối (Lần thử {attempt + 1}/{max_retries}). Thử lại sau {wait_time}s...")
                time.sleep(wait_time)
            except Exception as e:
                print(f"⚠️ Lỗi không xác định: {e}")
                break
        
        print("❌ Đã hết lượt thử lại nhưng vẫn không kết nối được tới Subgraph.")
        return []
    
    def batch_iterable(self, iterable, batch_size):
        it = iter(iterable)
        while True:
            batch = list(islice(it, batch_size))
            if not batch:
                break
            yield batch

    def get_position_info_with_multicall(self, w3, chain, masterchef_addresses, token_ids, batch_size=100):
        contract_address = masterchef_addresses.get(chain)
        if not contract_address:
            return {}
        position_infos = {}

        for batch_num, token_batch in enumerate(self.batch_iterable(token_ids, batch_size), start=1):
            mc = W3Multicall(w3)
            for token_id in token_batch:
                mc.add(
                    W3Multicall.Call(
                        contract_address,
                        "userPositionInfos(uint256)(uint128,uint128,int24,int24,uint256,uint256,address,uint256,uint256)",
                        token_id
                    )
                )

            try:
                position_results = mc.call()
            except Exception as e:
                print(f"⚠️ Multicall batch {batch_num} lỗi: {e}")
                continue

            for idx, data in enumerate(position_results):
                token_id = token_batch[idx]
                if not data:
                    continue

                try:
                    liquidity, boost_liquidity, tick_lower, tick_upper, reward_growth_inside, reward, user, position_pid, boost_multiplier = data
                    position_infos[token_id] = {
                        "liquidity": liquidity,
                        "boost_liquidity": boost_liquidity,
                        "tick_lower": tick_lower,
                        "tick_upper": tick_upper,
                        "reward_growth_inside": reward_growth_inside,
                        "reward": reward,
                        "user": user,
                        "position_pid": position_pid,
                        "boost_multiplier": boost_multiplier
                    }
                except Exception as e:
                    print(f"⚠️ Error unpacking token_id={token_id}: {e}")
                    continue

            print(f"✅ Done batch {batch_num}: {len(token_batch)} token_ids processed")

        return position_infos
    
    def get_recent_token_ids_from_rpc(self, w3, pool_address, masterchef_address, current_block) -> list:
        # Lấy logs từ 100 blocks gần nhất (~5 phút trên BSC/ARB) để tránh lỗi limit exceeded của public RPC
        from_block = max(0, current_block - 100)
        recent_token_ids = set()

        pool_checksum = Web3.to_checksum_address(pool_address)
        npm_address = Web3.to_checksum_address(self.npm_addresses.get(self.chain_name, "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"))
        
        # 1. Quét tx trên Pool (Đại diện cho Mint/IncreaseLiquidity)
        try:
            pool_logs = w3.eth.get_logs({
                'fromBlock': from_block,
                'toBlock': 'latest',
                'address': pool_checksum
            })
            print(f"Pool logs: {len(pool_logs)}")
            
            tx_hashes_pool = set(log['transactionHash'].hex() for log in pool_logs)

            # 2. Xóa bỏ quét tx mở rộng trên MasterChef do quá tải Node (Timeout).
            # Bất cứ giao dịch Mint (IncreaseLiquidity) nào cũng BẮT BUỘC phải tương tác với Pool (phát ra event Mint).
            # Do đó, chỉ cần lấy các tx tương tác với Pool là ĐỦ để móc ra toàn bộ event của NPM.
            all_tx_hashes = tx_hashes_pool
            
            # 3. Phân tích các MẠNG (Receipts) từ tx_hashes để lấy ra token_id từ NPM
            # Target topic
            transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
            increase_liq_topic = "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
            
            for tx_hash in all_tx_hashes:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                for log in receipt['logs']:
                    # Chỉ quan tâm log do NonfungiblePositionManager phát ra
                    if log['address'].lower() == npm_address.lower():
                        topics = log.get('topics', [])
                        if not topics:
                            continue
                        
                        # Sử dụng Web3.to_hex() để đảm bảo luôn có tiền tố '0x' khớp với chuỗi
                        topic_0 = Web3.to_hex(topics[0]).lower()
                        
                        # Bắt sự kiện Transfer(from, to, tokenId) hoặc IncreaseLiquidity
                        if topic_0 == transfer_topic.lower() and len(topics) == 4:
                            token_id = int.from_bytes(topics[3], "big")
                            recent_token_ids.add(token_id)
                        elif topic_0 == increase_liq_topic.lower() and len(topics) >= 2:
                            token_id = int.from_bytes(topics[1], "big")
                            recent_token_ids.add(token_id)

        except Exception as e:
            print(f"⚠️ Cannot extract recent tokens from RPC: {e}")

        # Về cơ bản, list này chứa các recent token_ids liên quan tới tx của Pool và MC 
        # Sự thanh lọc (filter) token_id có thuộc về Pool hiện tại hay không sẽ do `get_position_info_with_multicall` tự loại bỏ (ví dụ: tick=0, liquidity=0)
        return list(recent_token_ids)

    def scan_and_profile(self, pool_address, is_masterchef=True, force_refresh=False):
        """Phân tích và lọc các Position đang In-range"""

        cache_key = f"{self.chain_name}_{pool_address}_{is_masterchef}"
        current_time = time.time()
        
        if not force_refresh and cache_key in self.__class__._scan_cache:
            cached_data = self.__class__._scan_cache[cache_key]
            if current_time - cached_data['timestamp'] < 300:  # Cache sống trong 5 phút
                print(f"⚡ [SCAN CACHE HIT] Trả về dữ liệu pool {pool_address} từ memory.")
                return cached_data['data']
                
        print(f"🔄 [SCAN CACHE MISS] Fetch dữ liệu on-chain mới cho {pool_address}.")

        state = self.get_realtime_pool_state(pool_address)
        if not state: return None
        
        current_tick = state['currentTick']
        sqrt_price_x96 = state['sqrtPriceX96']
        l_pool_active = state['activeLiquidity']
        tick_spacing = state['tickSpacing']
        token0_address = state['token0']
        token1_address = state['token1']
        
        raw_positions = self.fetch_all_positions(pool_address, is_masterchef)
        print(f"Raw positions size: {len(raw_positions)}")

        token_ids_from_graph = [int(pos['id']) for pos in raw_positions]
        
        # Mở rộng danh sách bằng cách quét RPC bù đắp độ trễ
        try:
            current_rpc_block = self.w3.eth.get_block('latest')['number']
            masterchef_address = self.masterchef_addresses.get(self.chain_name) if is_masterchef else None
            recent_tokens = self.get_recent_token_ids_from_rpc(self.w3, pool_address, masterchef_address, current_rpc_block)
            
            print(f"🔍 Found {len(recent_tokens)} recent tokens from RPC.")
            
            # Add into graph result
            for t_id in recent_tokens:
                if t_id not in token_ids_from_graph:
                    token_ids_from_graph.append(t_id)
                    raw_positions.append({
                        "id": str(t_id),
                        "liquidity": 0,
                        "tickLower": 0,
                        "tickUpper": 0,
                        "user": {"id": "rpc_caught"}
                    })
        except Exception as e:
            print(f"⚠️ RPC Event Catch-up failed: {e}")

        position_infos = self.get_position_info_with_multicall(self.w3, self.chain_name, self.masterchef_addresses, token_ids_from_graph, batch_size=100)
        # print(f"Position infos: {position_infos}")
        
        # Deleted check for raw_positions to allow scanning new pools without positions

        in_range = []
        total_active_l_from_positions = 0

        for pos in raw_positions:
            try:
                pos_id = int(pos['id'])
                if pos_id not in position_infos:
                    continue

                real_liquidity = int(position_infos[pos_id].get('liquidity', 0))
                pos['liquidity'] = real_liquidity

                t_low = int(position_infos[pos_id].get('tick_lower', pos.get('tickLower', 0)))
                t_up = int(position_infos[pos_id].get('tick_upper', pos.get('tickUpper', 0)))
                pos['tickLower'] = str(t_low)
                pos['tickUpper'] = str(t_up)
                
                # Chỉ lọc những vị thế có chứa tick hiện tại
                if t_low <= current_tick <= t_up:
                    pos['share_percent'] = 0 
                    in_range.append(pos)
                    total_active_l_from_positions += real_liquidity
            except (KeyError, TypeError, ValueError):
                continue

        # Tính toán % chiếm hữu reward dựa trên tổng thanh khoản đang active
        for pos in in_range:
            if total_active_l_from_positions > 0:
                pos['share_percent'] = (int(pos['liquidity']) / total_active_l_from_positions) * 100
            else:
                pos['share_percent'] = 0

        in_range.sort(key=lambda x: int(x['liquidity']), reverse=True)

        result = {
            "pool": pool_address,
            "token0": token0_address,
            "token1": token1_address,
            "sqrtPriceX96": sqrt_price_x96,
            "tickSpacing": tick_spacing,
            "currentTick": current_tick,
            "rpcActiveLiquidity": l_pool_active,
            "totalInRangeLiquidity": total_active_l_from_positions,
            "inRangeCount": len(in_range),
            "competitors": in_range
        }
        
        # --- 3. LƯU VÀO CACHE TRƯỚC KHI TRẢ VỀ ---
        self.__class__._scan_cache[cache_key] = {
            'data': result,
            'timestamp': current_time
        }
        
        return result

if __name__ == "__main__":
    
    try:
        MY_CHAIN = "ETH" 
        MY_POOL = "0x6ca298d2983ab03aa1da7679389d955a4efee15c"
        
        scanner = V3Scanner(MY_CHAIN)
        data = scanner.scan_and_profile(MY_POOL)
        
        if data:
            print(f"\n✅ PHÂN TÍCH THÀNH CÔNG TRÊN CHAIN {MY_CHAIN}")
            print(f"📍 Pool Address: {data['pool']}")
            print(f"📍 Tick hiện tại: {data['currentTick']}")
            print(f"🔥 Số lượng đối thủ đang In-range (Active): {data['inRangeCount']}")
            print(f"📊 Tổng thanh khoản Staked đang tranh chấp: {data['totalInRangeLiquidity']}")
            
            if data['competitors']:
                print("\n--- TOP 10 ĐỐI THỦ ĐANG CHIẾM REWARD CAO NHẤT ---")
                for i, p in enumerate(data['competitors'][:10]):
                    owner = p.get('user', {}).get('id', 'Unknown')
                    print(f"{i+1}. NFT ID: {p['id']} | Share: {p['share_percent']:.2f}% | Range: [{p['tickLower']} : {p['tickUpper']}] | Owner: {owner} | Active Liquidity: {p['liquidity']}")
                    
    except Exception as e:
        print(f"❌ Lỗi khởi tạo: {e}")