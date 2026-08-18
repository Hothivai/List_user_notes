import os
import sys
import time

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from web3 import Web3

from config import (
    AERODROME_NPM_FACTORY_ADDRESSES,
    MASTERCHEF_ADDRESSES,
    NPM_ADDRESSES,
)
from logging_setup import system_logger as log
from services.liquidity_actions.helper import (
    get_abi,
    get_token_info,
    get_web3_connection,
)
from services.update_query import get_aerodrome_pool_info


MAX_UINT128 = 2**128 - 1


class CollectHarvestService:
    """
    Build unsigned wallet transactions for V3 position actions.

    PancakeSwap:
    - Collect uses NPM for unstaked positions and MasterChef for staked positions.
    - Harvest uses MasterChef.harvest(tokenId, recipient).
    - Withdraw preserves the existing current behavior: remove 100%, collect, then
      MasterChef.withdraw when staked. It does not burn or harvest.

    Aerodrome:
    - Uses the position's configured NPM and resolves its Gauge from DB pool metadata.
    - Collect is only allowed while the NFT is not staked in the Gauge.
    - Harvest uses Gauge.getReward(tokenId).
    - Withdraw returns one or two tx steps. Staked positions first call Gauge.withdraw,
      then close the returned NFT with NPM multicall(decreaseLiquidity, collect, burn).
    """

    def __init__(self, chain_name: str, dex: str = "pancakeswap", npm_address: str = None):
        self.chain = chain_name.upper()
        self.dex = self._normalize_dex(dex)
        self.w3 = get_web3_connection(self.chain)

        if not self.w3 or not self.w3.is_connected():
            raise ConnectionError(f"Cannot connect to RPC for chain {self.chain}")

        if self.dex == "aerodrome":
            self._init_aerodrome(npm_address)
        else:
            self._init_pancake()

    @staticmethod
    def _normalize_dex(dex: str) -> str:
        value = (dex or "pancakeswap").strip().lower()
        if value in ("aerodrome", "aerodrome_v3", "aerodrome_gauge"):
            return "aerodrome"
        return "pancakeswap"

    @staticmethod
    def _tx_step(label: str, to: str, data: str, sub_calls=None) -> dict:
        return {
            "label": label,
            "to": to,
            "data": data,
            "value": "0x0",
            "sub_calls": sub_calls or [],
        }

    @staticmethod
    def _with_single_step(payload: dict, step: dict) -> dict:
        payload.update({
            "to": step["to"],
            "data": step["data"],
            "value": step["value"],
            "steps": [step],
        })
        return payload

    def _init_pancake(self):
        self.npm_address = Web3.to_checksum_address(NPM_ADDRESSES[self.chain])
        npm_abi = get_abi(self.chain, self.npm_address)
        self.npm_contract = self.w3.eth.contract(address=self.npm_address, abi=npm_abi)

        self.mc_address = Web3.to_checksum_address(MASTERCHEF_ADDRESSES[self.chain])
        mc_abi = get_abi(self.chain, self.mc_address)
        self.mc_contract = self.w3.eth.contract(address=self.mc_address, abi=mc_abi)

    def _init_aerodrome(self, npm_address: str):
        if self.chain not in AERODROME_NPM_FACTORY_ADDRESSES:
            raise ValueError(f"Aerodrome actions are not configured for chain {self.chain}")
        if not npm_address:
            raise ValueError("npm_address is required for Aerodrome actions")

        self.npm_address = Web3.to_checksum_address(npm_address)
        npm_factory_map = AERODROME_NPM_FACTORY_ADDRESSES[self.chain]
        self.factory_address = npm_factory_map.get(self.npm_address)
        if not self.factory_address:
            raise ValueError(f"Unknown Aerodrome NPM for {self.chain}: {self.npm_address}")

        npm_abi = get_abi(self.chain, self.npm_address)
        self.npm_contract = self.w3.eth.contract(address=self.npm_address, abi=npm_abi)
        self.mc_address = None
        self.mc_contract = None

    def _pancake_is_staked(self, nft_id: int) -> bool:
        try:
            user_pos = self.mc_contract.functions.userPositionInfos(nft_id).call()
            return user_pos[0] > 0
        except Exception as exc:
            log.debug(f"Position #{nft_id} not found in MasterChef: {exc}")
            return False

    def _build_npm_close_multicall(self, nft_id: int, liquidity: int, user: str, deadline: int) -> str:
        decrease_params = (nft_id, liquidity, 0, 0, deadline)
        collect_params = (nft_id, user, MAX_UINT128, MAX_UINT128)

        decrease_calldata = self.npm_contract.functions.decreaseLiquidity(
            decrease_params
        )._encode_transaction_data()
        collect_calldata = self.npm_contract.functions.collect(
            collect_params
        )._encode_transaction_data()
        burn_calldata = self.npm_contract.functions.burn(nft_id)._encode_transaction_data()

        multicall_data = [
            Web3.to_bytes(hexstr=decrease_calldata),
            Web3.to_bytes(hexstr=collect_calldata),
            Web3.to_bytes(hexstr=burn_calldata),
        ]
        return self.npm_contract.functions.multicall(multicall_data)._encode_transaction_data()

    def _get_aerodrome_context(self, nft_id: int, user_address: str = None) -> dict:
        user = Web3.to_checksum_address(user_address) if user_address else None
        position = self.npm_contract.functions.positions(nft_id).call()

        token0_addr = Web3.to_checksum_address(position[2])
        token1_addr = Web3.to_checksum_address(position[3])
        tick_spacing = position[4]
        liquidity = position[7]
        tokens_owed0 = position[10]
        tokens_owed1 = position[11]

        pool_info = get_aerodrome_pool_info(
            self.chain,
            token0_addr,
            token1_addr,
            tick_spacing,
            factory_address=self.factory_address,
        )
        if not pool_info:
            raise ValueError(
                f"Cannot resolve Aerodrome pool metadata for NFT #{nft_id} "
                f"on NPM {self.npm_address}"
            )

        gauge_address = pool_info.get("gauge_address")
        if not gauge_address:
            raise ValueError(f"Aerodrome pool for NFT #{nft_id} has no gauge_address")

        gauge_address = Web3.to_checksum_address(gauge_address)
        gauge_abi = get_abi(self.chain, gauge_address)
        gauge_contract = self.w3.eth.contract(address=gauge_address, abi=gauge_abi)

        is_staked = False
        pending_reward = 0
        if user:
            try:
                is_staked = bool(gauge_contract.functions.stakedContains(user, nft_id).call())
            except Exception as exc:
                log.warning(f"Failed stakedContains({user}, {nft_id}) on {gauge_address}: {exc}")
            try:
                pending_reward = gauge_contract.functions.earned(user, nft_id).call()
            except Exception as exc:
                log.warning(f"Failed earned({user}, {nft_id}) on {gauge_address}: {exc}")

        return {
            "position": position,
            "token0_addr": token0_addr,
            "token1_addr": token1_addr,
            "liquidity": liquidity,
            "tokens_owed0": tokens_owed0,
            "tokens_owed1": tokens_owed1,
            "gauge_address": gauge_address,
            "gauge_contract": gauge_contract,
            "is_staked": is_staked,
            "pending_reward": pending_reward,
        }

    def get_claimable_info(self, nft_id: int, user_address: str = None) -> dict:
        if self.dex == "aerodrome":
            return self._get_aerodrome_claimable_info(nft_id, user_address)
        return self._get_pancake_claimable_info(nft_id)

    def _get_pancake_claimable_info(self, nft_id: int) -> dict:
        is_staked = self._pancake_is_staked(nft_id)
        pending_reward = 0
        if is_staked:
            pending_reward = self.mc_contract.functions.pendingCake(nft_id).call()

        try:
            position = self.npm_contract.functions.positions(nft_id).call()
            token0_address = position[2]
            token1_address = position[3]
            tokens_owed0 = position[10]
            tokens_owed1 = position[11]

            token0_symbol, token0_decimals = get_token_info(self.w3, self.chain, token0_address)
            token1_symbol, token1_decimals = get_token_info(self.w3, self.chain, token1_address)
        except Exception as exc:
            log.error(f"Failed to read Pancake position #{nft_id}: {exc}")
            return {"error": "READ_FAILED", "message": f"Failed to read position: {exc}"}

        return {
            "dex": self.dex,
            "nft_id": nft_id,
            "is_staked": is_staked,
            "pending_fee_token0_raw": str(tokens_owed0),
            "pending_fee_token1_raw": str(tokens_owed1),
            "pending_fee_token0_human": tokens_owed0 / (10 ** token0_decimals),
            "pending_fee_token1_human": tokens_owed1 / (10 ** token1_decimals),
            "token0_symbol": token0_symbol,
            "token1_symbol": token1_symbol,
            "pending_reward_raw": str(pending_reward),
            "pending_reward_human": pending_reward / (10 ** 18) if pending_reward else 0,
            "reward_symbol": "CAKE",
            "collect_disabled": False,
        }

    def _get_aerodrome_claimable_info(self, nft_id: int, user_address: str = None) -> dict:
        try:
            ctx = self._get_aerodrome_context(nft_id, user_address)
            token0_symbol, token0_decimals = get_token_info(self.w3, self.chain, ctx["token0_addr"])
            token1_symbol, token1_decimals = get_token_info(self.w3, self.chain, ctx["token1_addr"])
        except Exception as exc:
            log.error(f"Failed to read Aerodrome position #{nft_id}: {exc}")
            return {"error": "READ_FAILED", "message": f"Failed to read position: {exc}"}

        return {
            "dex": self.dex,
            "nft_id": nft_id,
            "npm_address": self.npm_address,
            "gauge_address": ctx["gauge_address"],
            "is_staked": ctx["is_staked"],
            "pending_fee_token0_raw": str(ctx["tokens_owed0"]),
            "pending_fee_token1_raw": str(ctx["tokens_owed1"]),
            "pending_fee_token0_human": ctx["tokens_owed0"] / (10 ** token0_decimals),
            "pending_fee_token1_human": ctx["tokens_owed1"] / (10 ** token1_decimals),
            "token0_symbol": token0_symbol,
            "token1_symbol": token1_symbol,
            "pending_reward_raw": str(ctx["pending_reward"]),
            "pending_reward_human": ctx["pending_reward"] / (10 ** 18) if ctx["pending_reward"] else 0,
            "reward_symbol": "AERO",
            "collect_disabled": ctx["is_staked"],
            "collect_disabled_reason": (
                "Collect is disabled while this Aerodrome position is staked in Gauge."
                if ctx["is_staked"]
                else ""
            ),
        }

    def build_collect_fee_tx(self, nft_id: int, user_address: str) -> dict:
        if self.dex == "aerodrome":
            return self._build_aerodrome_collect_fee_tx(nft_id, user_address)
        return self._build_pancake_collect_fee_tx(nft_id, user_address)

    def _build_pancake_collect_fee_tx(self, nft_id: int, user_address: str) -> dict:
        user = Web3.to_checksum_address(user_address)
        collect_params = (nft_id, user, MAX_UINT128, MAX_UINT128)

        is_staked = self._pancake_is_staked(nft_id)
        if is_staked:
            log.info(f"Position #{nft_id} is staked; building Pancake collect via MasterChef")
            collect_fn = self.mc_contract.functions.collect(collect_params)
            target_contract = self.mc_address
        else:
            log.info(f"Position #{nft_id} is not staked; building Pancake collect via NPM")
            collect_fn = self.npm_contract.functions.collect(collect_params)
            target_contract = self.npm_address

        step = self._tx_step(
            "Collect fees",
            target_contract,
            collect_fn._encode_transaction_data(),
            ["collect"],
        )
        return self._with_single_step({
            "action": "COLLECT_FEE",
            "dex": self.dex,
            "is_staked": is_staked,
        }, step)

    def _build_aerodrome_collect_fee_tx(self, nft_id: int, user_address: str) -> dict:
        user = Web3.to_checksum_address(user_address)
        try:
            ctx = self._get_aerodrome_context(nft_id, user)
        except Exception as exc:
            return {"error": "READ_FAILED", "message": str(exc)}

        if ctx["is_staked"]:
            return {
                "error": "POSITION_STAKED_COLLECT_DISABLED",
                "message": (
                    f"Aerodrome position #{nft_id} is staked in Gauge. "
                    "Collect is disabled to avoid unstaking the NFT."
                ),
            }

        try:
            owner = self.npm_contract.functions.ownerOf(nft_id).call()
        except Exception as exc:
            return {"error": "READ_FAILED", "message": f"Failed to read NFT owner: {exc}"}
        if owner.lower() != user.lower():
            return {
                "error": "NOT_POSITION_OWNER",
                "message": f"Connected wallet is not the owner of Aerodrome NFT #{nft_id}.",
            }

        collect_params = (nft_id, user, MAX_UINT128, MAX_UINT128)
        step = self._tx_step(
            "Collect fees",
            self.npm_address,
            self.npm_contract.functions.collect(collect_params)._encode_transaction_data(),
            ["collect"],
        )
        return self._with_single_step({
            "action": "COLLECT_FEE",
            "dex": self.dex,
            "npm_address": self.npm_address,
            "gauge_address": ctx["gauge_address"],
            "is_staked": False,
        }, step)

    def build_harvest_reward_tx(self, nft_id: int, user_address: str) -> dict:
        if self.dex == "aerodrome":
            return self._build_aerodrome_harvest_reward_tx(nft_id, user_address)
        return self._build_pancake_harvest_reward_tx(nft_id, user_address)

    def _build_pancake_harvest_reward_tx(self, nft_id: int, user_address: str) -> dict:
        user = Web3.to_checksum_address(user_address)

        try:
            user_pos = self.mc_contract.functions.userPositionInfos(nft_id).call()
            if user_pos[0] == 0:
                return {
                    "error": "POSITION_NOT_STAKED",
                    "message": f"Position #{nft_id} is not staked in MasterChef. Cannot harvest.",
                }
        except Exception as exc:
            return {
                "error": "READ_FAILED",
                "message": f"Failed to check position stake status: {exc}",
            }

        try:
            pending_reward = self.mc_contract.functions.pendingCake(nft_id).call()
            if pending_reward == 0:
                return {
                    "error": "NO_PENDING_REWARD",
                    "message": f"Position #{nft_id} has no pending CAKE reward to harvest.",
                }
        except Exception as exc:
            log.warning(f"Failed to check pendingCake for #{nft_id}: {exc}")
            pending_reward = 0

        step = self._tx_step(
            "Harvest rewards",
            self.mc_address,
            self.mc_contract.functions.harvest(nft_id, user)._encode_transaction_data(),
            ["harvest"],
        )
        return self._with_single_step({
            "action": "HARVEST_REWARD",
            "dex": self.dex,
            "pending_reward_raw": str(pending_reward),
            "pending_reward_human": pending_reward / (10 ** 18) if pending_reward else 0,
            "reward_symbol": "CAKE",
        }, step)

    def _build_aerodrome_harvest_reward_tx(self, nft_id: int, user_address: str) -> dict:
        user = Web3.to_checksum_address(user_address)
        try:
            ctx = self._get_aerodrome_context(nft_id, user)
        except Exception as exc:
            return {"error": "READ_FAILED", "message": str(exc)}

        if not ctx["is_staked"]:
            return {
                "error": "POSITION_NOT_STAKED",
                "message": f"Aerodrome position #{nft_id} is not staked in Gauge. Cannot harvest.",
            }
        if ctx["pending_reward"] == 0:
            return {
                "error": "NO_PENDING_REWARD",
                "message": f"Aerodrome position #{nft_id} has no pending AERO reward to harvest.",
            }

        step = self._tx_step(
            "Harvest rewards",
            ctx["gauge_address"],
            ctx["gauge_contract"].functions.getReward(nft_id)._encode_transaction_data(),
            ["getReward"],
        )
        return self._with_single_step({
            "action": "HARVEST_REWARD",
            "dex": self.dex,
            "npm_address": self.npm_address,
            "gauge_address": ctx["gauge_address"],
            "pending_reward_raw": str(ctx["pending_reward"]),
            "pending_reward_human": ctx["pending_reward"] / (10 ** 18),
            "reward_symbol": "AERO",
        }, step)

    def build_withdraw_tx(self, nft_id: int, user_address: str, deadline: int = None) -> dict:
        if self.dex == "aerodrome":
            return self._build_aerodrome_withdraw_tx(nft_id, user_address, deadline)
        return self._build_pancake_withdraw_tx(nft_id, user_address, deadline)

    def _build_pancake_withdraw_tx(self, nft_id: int, user_address: str, deadline: int = None) -> dict:
        user = Web3.to_checksum_address(user_address)
        deadline = deadline or int(time.time()) + 300

        try:
            position = self.npm_contract.functions.positions(nft_id).call()
            liquidity = position[7]
            token0_addr = position[2]
            token1_addr = position[3]
        except Exception as exc:
            log.error(f"Failed to read Pancake position #{nft_id} for withdraw: {exc}")
            return {"error": "READ_FAILED", "message": f"Could not read position info: {exc}"}

        if liquidity == 0:
            return {"error": "NO_LIQUIDITY", "message": f"Position #{nft_id} has no liquidity to withdraw."}

        is_staked = self._pancake_is_staked(nft_id)
        contract = self.mc_contract if is_staked else self.npm_contract
        target_addr = self.mc_address if is_staked else self.npm_address

        decrease_params = (nft_id, liquidity, 0, 0, deadline)
        collect_params = (nft_id, user, MAX_UINT128, MAX_UINT128)

        multicall_data = [
            Web3.to_bytes(hexstr=contract.functions.decreaseLiquidity(decrease_params)._encode_transaction_data()),
            Web3.to_bytes(hexstr=contract.functions.collect(collect_params)._encode_transaction_data()),
        ]
        sub_calls = ["decreaseLiquidity", "collect"]

        if is_staked:
            multicall_data.append(
                Web3.to_bytes(hexstr=contract.functions.withdraw(nft_id, user)._encode_transaction_data())
            )
            sub_calls.append("withdraw")

        step = self._tx_step(
            "Withdraw position",
            target_addr,
            contract.functions.multicall(multicall_data)._encode_transaction_data(),
            sub_calls,
        )

        token0_symbol, _ = get_token_info(self.w3, self.chain, token0_addr)
        token1_symbol, _ = get_token_info(self.w3, self.chain, token1_addr)

        return self._with_single_step({
            "action": "WITHDRAW_POSITION",
            "dex": self.dex,
            "is_staked": is_staked,
            "liquidity": str(liquidity),
            "token0_symbol": token0_symbol,
            "token1_symbol": token1_symbol,
            "nft_id": nft_id,
            "sub_calls": sub_calls,
        }, step)

    def _build_aerodrome_withdraw_tx(self, nft_id: int, user_address: str, deadline: int = None) -> dict:
        user = Web3.to_checksum_address(user_address)
        deadline = deadline or int(time.time()) + 300

        try:
            ctx = self._get_aerodrome_context(nft_id, user)
        except Exception as exc:
            return {"error": "READ_FAILED", "message": str(exc)}

        liquidity = ctx["liquidity"]
        if liquidity == 0:
            return {"error": "NO_LIQUIDITY", "message": f"Aerodrome position #{nft_id} has no liquidity to withdraw."}

        if not ctx["is_staked"]:
            try:
                owner = self.npm_contract.functions.ownerOf(nft_id).call()
            except Exception as exc:
                return {"error": "READ_FAILED", "message": f"Failed to read NFT owner: {exc}"}
            if owner.lower() != user.lower():
                return {
                    "error": "NOT_POSITION_OWNER",
                    "message": f"Connected wallet is not the owner of Aerodrome NFT #{nft_id}.",
                }

        close_step = self._tx_step(
            "Close position",
            self.npm_address,
            self._build_npm_close_multicall(nft_id, liquidity, user, deadline),
            ["decreaseLiquidity", "collect", "burn"],
        )
        steps = [close_step]
        sub_calls = list(close_step["sub_calls"])

        if ctx["is_staked"]:
            unstake_step = self._tx_step(
                "Unstake from Gauge",
                ctx["gauge_address"],
                ctx["gauge_contract"].functions.withdraw(nft_id)._encode_transaction_data(),
                ["withdraw"],
            )
            steps = [unstake_step, close_step]
            sub_calls = ["gauge.withdraw"] + sub_calls

        token0_symbol, _ = get_token_info(self.w3, self.chain, ctx["token0_addr"])
        token1_symbol, _ = get_token_info(self.w3, self.chain, ctx["token1_addr"])

        payload = {
            "action": "WITHDRAW_POSITION",
            "dex": self.dex,
            "npm_address": self.npm_address,
            "gauge_address": ctx["gauge_address"],
            "is_staked": ctx["is_staked"],
            "liquidity": str(liquidity),
            "token0_symbol": token0_symbol,
            "token1_symbol": token1_symbol,
            "nft_id": nft_id,
            "sub_calls": sub_calls,
            "steps": steps,
        }
        payload.update({
            "to": steps[0]["to"],
            "data": steps[0]["data"],
            "value": steps[0]["value"],
        })
        return payload
