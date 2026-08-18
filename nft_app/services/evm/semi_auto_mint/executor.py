import sys
import os

# Thiết lập đường dẫn hệ thống để import các module local
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import time
from web3 import Web3
from eth_abi import encode as abi_encode
from services.liquidity_actions.helper import (
    get_web3_connection, get_abi, get_contract, 
    build_transaction_safely, get_target_token_address,
    NPM_ADDRESSES, MASTERCHEF_ADDRESSES, WRAPPED_TOKENS
)

class V3Executor:
    """
    Module Executor: Chuẩn bị dữ liệu giao dịch cho PancakeSwap V3 trên EVM.
    Hỗ trợ quy trình: Wrap -> Approve -> Mint Position -> Auto-Stake MasterChef.
    Lưu ý: Trả về tx_base để người dùng ký tại Client (Metamask/WalletConnect).
    """
    def __init__(self, chain_name, account_address):
        self.chain_name = chain_name.upper()
        self.account = Web3.to_checksum_address(account_address)
        self.w3 = get_web3_connection(self.chain_name)
        
        if not self.w3 or not self.w3.is_connected():
            raise ConnectionError(f"❌ Không thể kết nối RPC tới mạng {self.chain_name}")

        # Khởi tạo địa chỉ các Contract quan trọng
        self.npm_address = Web3.to_checksum_address(NPM_ADDRESSES.get(self.chain_name))
        self.masterchef_address = Web3.to_checksum_address(MASTERCHEF_ADDRESSES.get(self.chain_name))
        
        # Khởi tạo Contract Instances
        npm_abi = get_abi(self.chain_name, self.npm_address)
        self.npm_contract = self.w3.eth.contract(address=self.npm_address, abi=npm_abi)

    def prepare_wrap_tx(self, token_address, needed_amount_raw):
        """
        Chuẩn bị giao dịch Wrap (Native sang Wrapped) nếu số dư wrapped token không đủ.
        :param token_address: Địa chỉ token (phải là Wrapped Native Token của chain)
        :param needed_amount_raw: Số lượng cần thiết (wei)
        :return: dict (unsigned tx) hoặc None nếu không cần wrap
        """
        wrapped_addr = WRAPPED_TOKENS.get(self.chain_name)
        if not wrapped_addr or token_address.lower() != wrapped_addr.lower():
            return None

        wrapped_addr = Web3.to_checksum_address(wrapped_addr)
        token_abi = get_abi(self.chain_name, wrapped_addr)
        token_contract = self.w3.eth.contract(address=wrapped_addr, abi=token_abi)

        # Kiểm tra số dư Wrapped hiện tại
        current_balance = token_contract.functions.balanceOf(self.account).call()
        print(f"📡 Số dư Wrapped: {current_balance / 10**18}")
        
        if current_balance < needed_amount_raw:
            missing_amount_raw = needed_amount_raw - current_balance
            # Chuyển đổi sang đơn vị Ether để dùng với build_transaction_safely (trường value)
            missing_human = missing_amount_raw / 10**18 
            
            print(f"⚠️ Thiếu {missing_human} wrapped token. Chuẩn bị giao dịch deposit...")
            deposit_fn = token_contract.functions.deposit()
            return build_transaction_safely(self.chain_name, deposit_fn, self.account, value=missing_human)
        
        return None

    def prepare_approve_tx(self, token_address, amount_raw):
        """
        Chuẩn bị giao dịch Approve token cho NonfungiblePositionManager.
        :param token_address: Địa chỉ token cần approve.
        :param amount_raw: Số lượng thô (đã nhân decimals).
        :return: dict (unsigned tx) hoặc None nếu allowance đã đủ.
        """
        token_address = Web3.to_checksum_address(token_address)
        target_addr = get_target_token_address(self.w3, token_address)
        
        token_abi = get_abi(self.chain_name, target_addr)
        token_contract = self.w3.eth.contract(address=token_address, abi=token_abi)
        
        # Kiểm tra hạn mức chi tiêu hiện tại
        allowance = token_contract.functions.allowance(self.account, self.npm_address).call()
        print(f"📡 Hạn mức chi tiêu: {allowance}")
        print(f"📡 Số lượng cần approve: {amount_raw}")
        
        if allowance >= amount_raw:
            print(f"✅ Allowance cho {token_address} đã đủ.")
            return None
            
        approve_fn = token_contract.functions.approve(self.npm_address, amount_raw)
        return build_transaction_safely(self.chain_name, approve_fn, self.account)

    def prepare_mint_tx(self, strategy_data):
        """
        Chuẩn bị giao dịch Mint vị thế mới (NFT).
        :param strategy_data: Dữ liệu từ Range Optimizer (bao gồm range, amounts, decimals, symbols).
        :return: dict (unsigned tx)
        """
        print(f"\n🛠️ Chuẩn bị Mint NFT cho chiến lược: {strategy_data.get('mode', 'custom').upper()}")
        
        token0 = Web3.to_checksum_address(strategy_data['token0_address'])
        token1 = Web3.to_checksum_address(strategy_data['token1_address'])
        
        # Tính toán amounts theo đơn vị thô (integer)
        amount0_raw = int(strategy_data['token_amounts'][strategy_data['token0_symbol']] * (10**strategy_data['token0_decimals']))
        amount1_raw = int(strategy_data['token_amounts'][strategy_data['token1_symbol']] * (10**strategy_data['token1_decimals']))
        
        deadline = int(time.time()) + 600  # Thời hạn 10 phút
        
        mint_params = {
            "token0": token0,
            "token1": token1,
            "fee": strategy_data['fee_tier'],
            "tickLower": strategy_data['range'][0],
            "tickUpper": strategy_data['range'][1],
            "amount0Desired": amount0_raw,
            "amount1Desired": amount1_raw,
            "amount0Min": 0,  # Có thể tính toán slippage dựa trên amountDesired nếu cần
            "amount1Min": 0,
            "recipient": self.account,
            "deadline": deadline
        }

        mint_fn = self.npm_contract.functions.mint(mint_params)
        return build_transaction_safely(self.chain_name, mint_fn, self.account)

    def prepare_stake_tx(self, token_id, pool_pid):
        """
        Chuẩn bị giao dịch Stake NFT vào MasterChef V3.
        Được thực hiện bằng cách gửi NFT qua safeTransferFrom kèm theo encoded PID.
        :param token_id: ID của NFT vừa được Mint.
        :param pool_pid: PID của pool trong MasterChef.
        """
        print(f"🌾 Chuẩn bị Stake NFT #{token_id} vào MasterChef (PID: {pool_pid})")
        
        # Mã hóa PID để MasterChef nhận diện pool khi nhận NFT
        data = abi_encode(['uint256'], [pool_pid])
        
        # npm_contract.safeTransferFrom(from, to, tokenId, data)
        stake_fn = self.npm_contract.functions.safeTransferFrom(
            self.account, 
            self.masterchef_address, 
            token_id, 
            data
        )
        
        return build_transaction_safely(self.chain_name, stake_fn, self.account)

    def extract_token_id_from_receipt(self, receipt):
        """
        Trích xuất Token ID từ logs của Transaction Receipt (Sử dụng sau khi người dùng ký Mint).
        """
        # Event signature của ERC-721 Transfer(address,address,uint256)
        transfer_sig = self.w3.keccak(text="Transfer(address,address,uint256)").hex()
        zero_address_topic = "0x" + "0" * 64

        for log in receipt.get("logs", []):
            # Kiểm tra xem log có phải từ NPM contract và đúng signature Transfer không
            if log["address"].lower() == self.npm_address.lower() and log["topics"][0].hex() == transfer_sig:
                # Kiểm tra topic[1] là 0x0 (Mint) và topic[2] là ví của User
                if log["topics"][1].hex() == zero_address_topic:
                    token_id = int(log["topics"][3].hex(), 16)
                    return token_id
        return None

# --- VÍ DỤ TÍCH HỢP ---
if __name__ == "__main__":
    # Giả lập dữ liệu từ module Range Optimizer
    STRATEGY = {
        "mode": "balanced",
        "range": [35000, 40000],
        "fee_tier": 2500,
        "token0_address": "0x55d398326f99059fF775485246999027B3197955", # WBNB
        "token1_address": "0xb0b92de23bAa85fB06208277E925ceD53edab482", # TRIA
        "token0_symbol": "USDT",
        "token1_symbol": "TRIA",
        "token0_decimals": 18,
        "token1_decimals": 18,
        "token_amounts": {"USDT": 500.0, "TRIA": 400.44813307}
    }
    
    USER_ADDR = "0x9b73E95909Be63F02b06130716384c3030C74D8D"
    PID = 525 

    executor = V3Executor("BNB", USER_ADDR)
    
    # 1. Kiểm tra Wrap (Nếu token0 là WBNB)
    needed_raw_0 = int(STRATEGY['token_amounts']['USDT'] * 10**18)
    needed_raw_1 = int(STRATEGY['token_amounts']['TRIA'] * 10**18)

    wrap_tx_0 = executor.prepare_wrap_tx(STRATEGY['token0_address'], needed_raw_0)
    wrap_tx_1 = executor.prepare_wrap_tx(STRATEGY['token1_address'], needed_raw_1)

    if wrap_tx_0:
        print(f"👉 Hãy ký giao dịch Wrap: {wrap_tx_0}")
    if wrap_tx_1:
        print(f"👉 Hãy ký giao dịch Wrap: {wrap_tx_1}")

    # 2. Kiểm tra Approve
    approve_tx_0 = executor.prepare_approve_tx(STRATEGY['token0_address'], needed_raw_0)
    approve_tx_1 = executor.prepare_approve_tx(STRATEGY['token1_address'], needed_raw_1)

    if approve_tx_0:
        print(f"👉 Hãy ký giao dịch Approve: {approve_tx_0}")
    if approve_tx_1:
        print(f"👉 Hãy ký giao dịch Approve: {approve_tx_1}")
    

    # 3. Chuẩn bị Mint
    # mint_tx = executor.prepare_mint_tx(STRATEGY)
    # print(f"👉 Hãy ký giao dịch Mint: {mint_tx}")